"""The dry run: what an update would do. Changes nothing on the site.

The projects come from the site's keep-current list, the ones whose owners ticked "Keep this
project's classifications up to date", unless they're given on the command line.
"""

import csv
import datetime
import hashlib
import json
import os

from . import pins
from .site_api import SiteError

# AC's --ref value for each reference name the site uses.
REF_MAP = {
    'hg19': 'hg19', 'GRCh37': 'GRCh37',
    'hg38': 'GRCh38', 'GRCh38': 'GRCh38',
    'GRCh38_viral': 'GRCh38_viral',
    'mm10': 'mm10', 'GRCm38': 'mm10',
}

# Measured 2026-11-06 on TCGA (101 samples) and Tang (one project each): a finished work
# directory was 5.8 and 5.7 times the archive, and the rebuilt archive is about 1 more.
DISK_FACTOR = 7
# TCGA took 32 minutes with --jobs 16 --bfb_threads 2 on 2026-11-06. One project, so this
# gives the scale of a run, not a schedule.
SECONDS_PER_SAMPLE = 19

UPDATE = 'update'
CURRENT = 'current'
UNKNOWN_REF = 'unknown-ref'
NO_ARCHIVE = 'no-archive'
NOT_FOUND = 'not-found'
NO_ACCESS = 'no-access'
AMBIGUOUS = 'ambiguous'

# Where a project in the plan came from.
FROM_SITE = 'site list'
INCLUDED = 'included'
GIVEN = 'given'


def read_project_file(path):
    """A TSV with `project_name` and/or `latest_id`, and optional `ref`, `extra_ac_flags`,
    `metadata_sheet`, a sheet to send with the upload, and `archive`, a local archive to use
    instead of the site's. Both paths are relative to this file.

    Lines starting with # are comments. An id from an older version still resolves.
    """
    def relative(row, column):
        value = (row.get(column) or '').strip()
        return os.path.join(os.path.dirname(os.path.abspath(path)), value) if value else ''

    with open(path, newline='') as handle:
        rows = csv.DictReader((line for line in handle if not line.startswith('#')),
                              delimiter='\t')
        return [{'name': (row.get('project_name') or '').strip(),
                 'id': (row.get('latest_id') or '').strip(),
                 'ref': (row.get('ref') or '').strip(),
                 'extra_ac_flags': (row.get('extra_ac_flags') or '').strip(),
                 'metadata_sheet': relative(row, 'metadata_sheet'),
                 'archive': relative(row, 'archive')}
                for row in rows]


def _is_id(value):
    return len(value) == 24 and all(c in '0123456789abcdefABCDEF' for c in value)


def wanted_from_argument(value, source=GIVEN):
    """--project and --include take an id or an exact name."""
    is_id = _is_id(value)
    return {'name': '' if is_id else value, 'id': value if is_id else '',
            'ref': '', 'extra_ac_flags': '', 'archive': '', 'source': source}


def wanted_from_site(rows):
    """The site's keep-current list: the projects whose owners ticked the box."""
    return [{'name': row['project_name'], 'id': row['id'], 'ref': '', 'extra_ac_flags': '',
             'archive': '', 'source': FROM_SITE} for row in rows]


def _index(projects):
    heads = [p for p in projects if p.get('is_latest_version', True)]
    by_id, by_name = {}, {}
    for head in heads:
        by_id[head['id']] = head
        for older in head.get('previous_versions') or []:
            by_id[older['linkid']] = head
        by_name.setdefault(head['project_name'], []).append(head)
    return by_id, by_name


def build_entry(wanted, by_id, by_name, site, target_version):
    label = wanted['name'] or wanted['id']
    head = by_id.get(wanted['id'])
    if head is None:
        matches = by_name.get(wanted['name'], [])
        if len(matches) > 1:
            return {'name': label, 'id': None, 'status': AMBIGUOUS,
                    'note': '%d projects have this name; give the id' % len(matches)}
        if not matches and wanted.get('source') == FROM_SITE:
            # On the site's list, so it exists, and the site gave this token no way to read it.
            return {'name': label, 'id': wanted['id'], 'status': NO_ACCESS,
                    'source': FROM_SITE,
                    'note': 'on the site\'s list, but this token can\'t read it. Is it an '
                            'admin\'s token?'}
        if not matches:
            return {'name': label, 'id': None, 'status': NOT_FOUND,
                    'source': wanted.get('source', GIVEN),
                    'note': 'not visible on %s with this token' % site.name}
        head = matches[0]
    reference = head.get('reference_genome') or ''
    ac_ref = wanted['ref'] or REF_MAP.get(reference)
    entry = {'name': head['project_name'], 'id': head['id'], 'reference': reference,
             'ac_ref': ac_ref, 'ref_override': wanted['ref'], 'samples': head.get('sample_count') or 0,
             'visibility': head.get('visibility'), 'current_ac': head.get('AC_version'),
             'aggregator': head.get('aggregator_version'),
             'tools': head.get('reconstruction_tools'),
             'extra_ac_flags': wanted['extra_ac_flags'],
             'metadata_sheet': wanted.get('metadata_sheet', ''), 'archive_bytes': None, 'note': '',
             'source': wanted.get('source', GIVEN)}
    if head.get('AC_version') == target_version:
        entry['status'] = CURRENT
    elif not ac_ref:
        entry['status'] = UNKNOWN_REF
        entry['note'] = 'no AC reference for %r; give it a ref in a --projects file' % reference
    else:
        entry['status'] = UPDATE
    if wanted.get('archive') and entry['status'] == UPDATE:
        # One campaign's substitute for the site's archive, such as a cleaned copy. Once it is
        # uploaded it is the site's archive, so later campaigns drop this column.
        local = wanted['archive']
        if not os.path.isfile(local):
            entry['status'] = NO_ARCHIVE
            entry['note'] = 'the local archive %s is not there' % local
        else:
            entry['local_archive'] = {'path': os.path.abspath(local), 'bytes': os.path.getsize(local),
                                      'sha256': sha256(local)}
            entry['archive_bytes'] = entry['local_archive']['bytes']
            entry['note'] = 'uses the local archive %s, not the site\'s' % local
    elif entry['status'] == UPDATE:
        entry['archive_bytes'] = site.archive_size(head['id'])
        if entry['archive_bytes'] is None:
            entry['status'] = NO_ARCHIVE
            entry['note'] = 'the site has no stored archive to download for this version'
    return entry


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _exclusions(values, by_id, by_name):
    """Each --exclude value, and the ids and names it stands for."""
    found = []
    for value in values:
        keys = {value}
        head = by_id.get(value)
        if head is not None:
            keys |= {head['id'], head['project_name']}
        for match in by_name.get(value, []):
            keys.add(match['id'])
        found.append((value, keys))
    return found


def _add_unlisted_heads(wanted, by_id, by_name, site):
    """Hidden-public projects aren't in the project listing unless the caller is a member, but
    an admin can read them by id. Ask for each one on the site's list that's missing."""
    for w in wanted:
        if w.get('source') != FROM_SITE or not w['id'] or w['id'] in by_id:
            continue
        try:
            head = site.project(w['id'])
        except SiteError:
            continue
        if head.get('is_latest_version', True):
            extra_id, extra_name = _index([head])
            by_id.update(extra_id)
            by_name.setdefault(head['project_name'], []).append(head)


def is_head(site, heads, project_id):
    """Whether *project_id* is still a project's current version, asking by id when the
    listing doesn't show it."""
    if project_id in heads:
        return True
    try:
        return bool(site.project(project_id).get('is_latest_version', True))
    except SiteError:
        return False


def build_plan(wanted, site, target_version=pins.AC_VERSION, exclude=(), listed=None):
    """*listed* is how many came from the site's list, or None when the list was given."""
    by_id, by_name = _index(site.list_projects())
    _add_unlisted_heads(wanted, by_id, by_name, site)
    exclusions = _exclusions(exclude, by_id, by_name)
    entries, seen, excluded = [], set(), {value: [] for value in exclude}
    for w in wanted:
        entry = build_entry(w, by_id, by_name, site, target_version)
        keys = {entry.get('id'), entry['name']} - {None, ''}
        hits = [value for value, ex_keys in exclusions if keys & ex_keys]
        if hits:
            for value in hits:
                excluded[value].append(entry['name'])
            continue
        if entry.get('id') and entry['id'] in seen:
            continue
        seen.add(entry.get('id'))
        entries.append(entry)
    entries.sort(key=lambda e: (e['status'] != UPDATE, e.get('archive_bytes') or 0))
    return {'site': site.name, 'target_ac': target_version,
            'created': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'list': {'from_site': listed is not None, 'site_count': listed,
                     'included': [e['name'] for e in entries if e.get('source') == INCLUDED],
                     'excluded': [{'value': value, 'matched': names}
                                  for value, names in excluded.items()]},
            'projects': entries}


def describe_list(plan):
    """Where the plan's projects came from, and the exceptions made at run time."""
    listing = plan.get('list') or {}
    if listing.get('from_site'):
        lines = ['Projects: %d on %s\'s keep-current list' % (listing['site_count'], plan['site'])]
    else:
        lines = ['Projects: given on the command line, not %s\'s keep-current list' % plan['site']]
    for name in listing.get('included') or []:
        lines.append('  included by hand: %s' % name)
    for row in listing.get('excluded') or []:
        if row['matched']:
            lines.append('  excluded by hand: %s' % ', '.join(row['matched']))
        else:
            lines.append('  --exclude %s matched nothing in the list' % row['value'])
    return lines


def predicted_disk(entries):
    return sum((e.get('archive_bytes') or 0) * DISK_FACTOR for e in entries if e['status'] == UPDATE)


def format_plan(plan):
    lines = ['Plan for %s, to AC %s, made %s' % (plan['site'], plan['target_ac'], plan['created'])]
    lines += describe_list(plan) + ['']
    lines.append('%-13s %8s %9s %-13s %-9s %-24s %s'
                 % ('status', 'samples', 'archive', 'reference', 'AC now', 'latest id', 'project'))
    for e in plan['projects']:
        size = '%.0f MB' % (e['archive_bytes'] / 1e6) if e.get('archive_bytes') else '-'
        lines.append('%-13s %8s %9s %-13s %-9s %-24s %s'
                     % (e['status'], e.get('samples', '-'), size, e.get('reference', '-'),
                        e.get('current_ac', '-'), e.get('id') or '-', e['name']))
        if e.get('note'):
            lines.append('%13s   %s' % ('', e['note']))
    updates = [e for e in plan['projects'] if e['status'] == UPDATE]
    samples = sum(e['samples'] for e in updates)
    lines += ['',
              '%d to update, %d samples. About %.1f hours at TCGA\'s rate, which varies a lot '
              'by project.' % (len(updates), samples, samples * SECONDS_PER_SAMPLE / 3600),
              'About %.1f GB of disk while running.' % (predicted_disk(updates) / 1e9),
              'References needed: %s' % (', '.join(sorted({e['ac_ref'] for e in updates})) or 'none'),
              'AC settings come from each archive, so they show in the change summary after `run`.']
    return '\n'.join(lines)


def save_plan(plan, path):
    with open(path, 'w') as handle:
        json.dump(plan, handle, indent=2)
        handle.write('\n')


def load_plan(path):
    with open(path) as handle:
        return json.load(handle)
