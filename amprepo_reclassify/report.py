"""Comparing old and new calls, for one project and for a whole run."""

import os
import re
from collections import defaultdict

from .archive import ProjectError

# The site's record limit is 16 MiB (16.78 MB). On 2026-11-06 prod's documents were 0.62 (Tang)
# and 0.69 (TCGA) of the local run.json. Metadata adds to that, and a local run can't see it, so
# this flags a project for the site's own check well before the limit.
RECORD_WARNING_BYTES = 12_000_000


def canon_class(label):
    """One name per class, because labels changed across AC versions (for example
    `Linear amplification` became `Linear`), and a rename isn't a change."""
    if label is None:
        return 'No amp'
    key = re.sub(r'[\s_\-]+', '', str(label).strip().lower())
    if key in ('', 'noamp', 'noamplification', 'none', 'na'):
        return 'No amp'
    if key == 'ecdna':
        return 'ecDNA'
    if key == 'bfb':
        return 'BFB'
    if key.startswith('linear'):
        return 'Linear'
    if key.startswith(('complexnoncyclic', 'heavilyrearranged')) or key == 'complex':
        return 'Complex-non-cyclic'
    if key.startswith('cyclic'):
        return 'Cyclic'
    if key == 'virus':
        return 'Virus'
    if key == 'unknown':
        return 'unknown'
    return str(label).strip()


def load_result_table(path):
    """{(sample, amplicon number): classification} and the AC version, if the table has one."""
    calls, version = {}, None
    with open(path) as handle:
        header = handle.readline().rstrip('\n').split('\t')
        columns = {name: i for i, name in enumerate(header)}
        try:
            sample, amplicon, classification = (columns['Sample name'], columns['AA amplicon number'],
                                                columns['Classification'])
        except KeyError:
            raise ProjectError('unexpected result table columns in %s: %s' % (path, header))
        version_column = columns.get('AC version')
        for line in handle:
            row = line.rstrip('\n').split('\t')
            if len(row) <= max(sample, amplicon, classification):
                continue
            calls[(row[sample], row[amplicon])] = row[classification]
            if (version_column is not None and len(row) > version_column
                    and row[version_column] not in ('', 'NA', 'None')):
                version = row[version_column]
    return calls, version


def load_old_calls(directories):
    """Every result table in the old AC directories. One may hold several single-sample tables."""
    merged = {}
    for directory in directories:
        for name in sorted(os.listdir(directory)):
            if name.endswith('_result_table.tsv'):
                calls, _ = load_result_table(os.path.join(directory, name))
                for key, value in calls.items():
                    merged.setdefault(key, value)
    return merged


def add_no_amp_rows(table_path, samples, reference):
    """Add a row for samples AC never saw (a CNV but no AA results), or the aggregator drops
    them from the project. Same form as AC's own: amplicon `NA`, Feature ID `<sample>_NA`."""
    with open(table_path) as handle:
        header = handle.readline().rstrip('\n').split('\t')
    with open(table_path, 'a') as handle:
        for sample in samples:
            row = []
            for column in header:
                if column == 'Sample name':
                    row.append(sample)
                elif column == 'Feature ID':
                    row.append(sample + '_NA')
                elif column == 'Reference version':
                    row.append(reference)
                elif re.search(r'Location|Oncogenes|genes', column, re.I):
                    row.append('[]')
                else:
                    row.append('NA')
            handle.write('\t'.join(row) + '\n')


def _sample_status(calls):
    per_sample = defaultdict(set)
    for (sample, _), label in calls.items():
        per_sample[sample].add(canon_class(label))
    return per_sample


def compare_calls(old_calls, new_calls):
    """The numbers a person reviews. Everything here is about classifications only."""
    old, new = _sample_status(old_calls), _sample_status(new_calls)
    samples = sorted(set(old) | set(new))
    changes = []
    for key in sorted(set(old_calls) | set(new_calls)):
        before, after = canon_class(old_calls.get(key)), canon_class(new_calls.get(key))
        if before != after:
            changes.append([key[0], key[1], before, after])

    def flips(label, source, destination):
        return [s for s in samples if label in destination.get(s, ()) and label not in source.get(s, ())]

    return {'samples': len(samples), 'old_samples': len(old), 'new_samples': len(new),
            'ecdna_old': sum('ecDNA' in c for c in old.values()),
            'ecdna_new': sum('ecDNA' in c for c in new.values()),
            'bfb_old': sum('BFB' in c for c in old.values()),
            'bfb_new': sum('BFB' in c for c in new.values()),
            'ecdna_gained': flips('ecDNA', old, new), 'ecdna_lost': flips('ecDNA', new, old),
            'bfb_gained': flips('BFB', old, new), 'bfb_lost': flips('BFB', new, old),
            'amplicon_changes': changes,
            'changed_samples': len({change[0] for change in changes})}


def concerns(result):
    """What a person must look at before this project is uploaded."""
    found = list(result['settings']['notes'])
    if result.get('site_ref') and result['ref'] != result['site_ref']:
        found.append('AC ran with reference %s, and the site says %s' % (result['ref'], result['site_ref']))
    if result['settings']['flags'] or result.get('extra_ac_flags'):
        found.append('Non-default AC flags: %s' % ' '.join(result['flags']))
    for key, label in (('control_dropped', 'The control rebuild dropped plots the original had'),
                       ('final_dropped', 'The rebuilt archive dropped files')):
        if result.get(key):
            found.append('%s: %d, first %s' % (label, len(result[key]), result[key][0]))
    if result['expected_samples'] != result['archive_samples']:
        found.append('The site has %d samples and the rebuilt archive has %d'
                     % (result['expected_samples'], result['archive_samples']))
    if result['cnv_beds'] != result['discovered_samples']:
        found.append('CNV BEDs for %d of %d samples. Check whether the original had them.'
                     % (result['cnv_beds'], result['discovered_samples']))
    if result['run_json_bytes'] > RECORD_WARNING_BYTES:
        found.append('run.json is %.1f MB, so check this against the site\'s 16 MiB record limit'
                     % (result['run_json_bytes'] / 1e6))
    return found


def project_report(result):
    calls = result['calls']
    lines = ['# %s' % result['name'], '',
             '- Project: %s' % result['url'],
             '- Version updated from: `%s`' % result['id'],
             '- Reference: `%s`' % result['ref'],
             '- AC: `%s` on the site, `%s` here' % (result['current_ac'], result['new_ac']),
             '- Built with: %s' % result['tools'],
             '- AC flags: `%s` (recovered from: %s, which ran AC %s)'
             % (' '.join(result['flags']) or 'defaults', result['settings']['source'],
                result['settings']['settings_ac_version'] or 'of unknown version'),
             *['- Note: %s' % item for item in result['settings'].get('info', [])],
             '- Samples: %d on the site, %d in the rebuilt archive'
             % (result['expected_samples'], result['archive_samples']),
             '- Archive: %.1f MB, of which %.1f MB of files aren\'t in the old archive'
             % (result['archive_bytes'] / 1e6, result['new_content_bytes'] / 1e6), '']
    found = concerns(result)
    if found:
        lines.append('## Check before uploading')
        lines += ['- %s' % item for item in found]
        lines.append('')
    lines += ['## Calls', '', '| | Old | New |', '| --- | --- | --- |',
              '| ecDNA+ samples | %d | %d |' % (calls['ecdna_old'], calls['ecdna_new']),
              '| BFB+ samples | %d | %d |' % (calls['bfb_old'], calls['bfb_new']),
              '', '%d amplicon(s) in %d sample(s) changed class.'
              % (len(calls['amplicon_changes']), calls['changed_samples']), '']
    for key, label in (('ecdna_gained', 'ecDNA gained'), ('ecdna_lost', 'ecDNA lost'),
                       ('bfb_gained', 'BFB gained'), ('bfb_lost', 'BFB lost')):
        if calls[key]:
            lines.append('- %s (%d): %s' % (label, len(calls[key]), ', '.join(calls[key])))
    if calls['amplicon_changes']:
        lines += ['', '| Sample | Amplicon | Old | New |', '| --- | --- | --- | --- |']
        lines += ['| %s | %s | %s | %s |' % tuple(change) for change in calls['amplicon_changes']]
    return '\n'.join(lines) + '\n'


def combined_summary(plan, states):
    """One table across a run, then everything that needs a look."""
    lines = ['# Change summary to AC %s' % plan['target_ac'], '',
             'Site: %s. Plan made %s.' % (plan['site'], plan['created']), '',
             '| Project | State | Samples | ecDNA+ | BFB+ | Amplicons changed | Archive MB | Files not in old archive MB |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    total_archive = total_new = 0
    attention = []
    for entry in plan['projects']:
        state = states.get(entry['id']) or {}
        result = state.get('result')
        if result:
            calls = result['calls']
            total_archive += result['archive_bytes']
            total_new += result['new_content_bytes']
            lines.append('| %s | built | %d | %d to %d | %d to %d | %d | %.1f | %.1f |' % (
                entry['name'], result['archive_samples'], calls['ecdna_old'], calls['ecdna_new'],
                calls['bfb_old'], calls['bfb_new'], len(calls['amplicon_changes']),
                result['archive_bytes'] / 1e6, result['new_content_bytes'] / 1e6))
            attention += ['%s: %s' % (entry['name'], item) for item in concerns(result)]
        else:
            label = 'failed' if state.get('failed') else 'unfinished' if state else entry['status']
            lines.append('| %s | %s | %s | | | | | |' % (entry['name'], label, entry.get('samples', '')))
            if state.get('failed'):
                attention.append('%s: failed: %s' % (entry['name'], state['failed']))
    lines += ['', 'Rebuilt archives come to %.0f MB, and files not in the old archives to %.0f MB. '
              'That isn\'t what the site will store. `upload --dry-run` gives those numbers.'
              % (total_archive / 1e6, total_new / 1e6), '']
    lines.append('## Check before uploading')
    lines += ['- %s' % item for item in attention] or ['Nothing flagged.']
    return '\n'.join(lines) + '\n'
