"""The dry run: what an update would do, from the site's own list. Changes nothing on the site."""

import csv
import datetime
import json
import os

from . import pins

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
UNTESTED_TOOL = 'untested-tool'
UNKNOWN_REF = 'unknown-ref'
NO_ARCHIVE = 'no-archive'
NOT_FOUND = 'not-found'
AMBIGUOUS = 'ambiguous'


def read_project_file(path):
    """A TSV with `project_name` and/or `latest_id`, and optional `ref`, `extra_ac_flags` and
    `metadata_sheet`, a sheet to send with the upload, its path relative to this file.

    Lines starting with # are comments. An id from an older version still resolves.
    """
    def sheet(row):
        value = (row.get('metadata_sheet') or '').strip()
        return os.path.join(os.path.dirname(os.path.abspath(path)), value) if value else ''

    with open(path, newline='') as handle:
        rows = csv.DictReader((line for line in handle if not line.startswith('#')),
                              delimiter='\t')
        return [{'name': (row.get('project_name') or '').strip(),
                 'id': (row.get('latest_id') or '').strip(),
                 'ref': (row.get('ref') or '').strip(),
                 'extra_ac_flags': (row.get('extra_ac_flags') or '').strip(),
                 'metadata_sheet': sheet(row)}
                for row in rows]


def wanted_from_argument(value):
    """--project takes an id or an exact name."""
    is_id = len(value) == 24 and all(c in '0123456789abcdefABCDEF' for c in value)
    return {'name': '' if is_id else value, 'id': value if is_id else '',
            'ref': '', 'extra_ac_flags': ''}


def _index(projects):
    heads = [p for p in projects if p.get('is_latest_version', True)]
    by_id, by_name = {}, {}
    for head in heads:
        by_id[head['id']] = head
        for older in head.get('previous_versions') or []:
            by_id[older['linkid']] = head
        by_name.setdefault(head['project_name'], []).append(head)
    return by_id, by_name


def _is_coral(project):
    tools = project.get('reconstruction_tools') or ''
    version = project.get('CoRAL_version') or ''
    return 'coral' in tools.lower() or version not in ('', 'NA', 'None')


def build_entry(wanted, by_id, by_name, site, target_version):
    label = wanted['name'] or wanted['id']
    head = by_id.get(wanted['id'])
    if head is None:
        matches = by_name.get(wanted['name'], [])
        if len(matches) > 1:
            return {'name': label, 'id': None, 'status': AMBIGUOUS,
                    'note': '%d projects have this name; give the id' % len(matches)}
        if not matches:
            return {'name': label, 'id': None, 'status': NOT_FOUND,
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
             'metadata_sheet': wanted.get('metadata_sheet', ''), 'archive_bytes': None, 'note': ''}
    if head.get('AC_version') == target_version:
        entry['status'] = CURRENT
    elif _is_coral(head):
        entry['status'] = UNTESTED_TOOL
        entry['note'] = 'CoRAL results have not been through this workflow'
    elif not ac_ref:
        entry['status'] = UNKNOWN_REF
        entry['note'] = 'no AC reference for %r; add a ref column' % reference
    else:
        entry['status'] = UPDATE
    if entry['status'] in (UPDATE, UNTESTED_TOOL):
        entry['archive_bytes'] = site.archive_size(head['id'])
        if entry['archive_bytes'] is None:
            entry['status'] = NO_ARCHIVE
            entry['note'] = 'the site has no stored archive to download for this version'
    return entry


def build_plan(wanted, site, target_version=pins.AC_VERSION):
    by_id, by_name = _index(site.list_projects())
    entries = [build_entry(w, by_id, by_name, site, target_version) for w in wanted]
    entries.sort(key=lambda e: (e['status'] != UPDATE, e.get('archive_bytes') or 0))
    return {'site': site.name, 'target_ac': target_version,
            'created': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'projects': entries}


def predicted_disk(entries):
    return sum((e.get('archive_bytes') or 0) * DISK_FACTOR for e in entries if e['status'] == UPDATE)


def format_plan(plan):
    lines = ['Plan for %s, to AC %s, made %s' % (plan['site'], plan['target_ac'], plan['created']), '']
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
