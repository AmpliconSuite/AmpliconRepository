"""
A removed sample has to leave the archive, not just the database.

The archive is what people download and what the aggregator is handed when the
edit is re-aggregated into the new version, so a sample left in it comes back --
and, until it does, ships to anyone who downloads the project after being told
the sample was removed.

This was broken in a way that only showed up end to end: the stripper deleted
`results/other_files/<sample>_classification` and two AA_outputs directories,
which is where AmpliconSuiteAggregator kept per-sample data several major
versions ago.  Current archives keep it under `results/samples/<sample>/` with
the tables in `results/consolidated_classification/`, so nothing matched, the
old archive went back into aggregation intact, and the new version had every
sample the old one did.
"""

import json
import os

import pytest

from caper.sample_removal import remove_samples_from_results

pytestmark = pytest.mark.integration


PROJECT = '6a7fcb597b2b634d24e7f0d8'


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


@pytest.fixture
def results_dir(tmp_path):
    """A miniature of a real aggregated archive, with two samples in it.

    Layouts and headers are copied from an archive the site produced with
    AmpliconSuiteAggregator 8.2: the sample column is spelled four different
    ways across the tables, and one table identifies rows only by feature ID.
    """
    root = str(tmp_path / 'results')
    classification = os.path.join(root, 'consolidated_classification')

    for sample in ('COLO320DM_LC', 'COLO320DM_hg19'):
        _write(os.path.join(root, 'samples', sample, f'{sample}_CNV_CALLS.bed'),
               'chr8\t127000000\t129000000\n')

    _write(os.path.join(root, 'run.json'), json.dumps({'runs': {
        'sample_1': [{'Sample name': 'COLO320DM_LC',
                      'Feature ID': 'COLO320DM_LC_amplicon1_ecDNA_1'}],
        'sample_2': [{'Sample name': 'COLO320DM_hg19',
                      'Feature ID': 'COLO320DM_hg19_amplicon1_ecDNA_1'}],
    }}))

    _write(os.path.join(root, 'aggregated_results.csv'),
           'Sample name,AA amplicon number,Feature ID\n'
           'COLO320DM_LC,1,COLO320DM_LC_amplicon1_ecDNA_1\n'
           'COLO320DM_hg19,1,COLO320DM_hg19_amplicon1_ecDNA_1\n')

    _write(os.path.join(classification, f'{PROJECT}_amplicon_classification_profiles.tsv'),
           'sample_name\tamplicon_number\tamplicon_decomposition_class\n'
           'COLO320DM_LC\tamplicon1\tCyclic\n'
           'COLO320DM_hg19\tamplicon1\tCyclic\n')
    _write(os.path.join(classification, f'{PROJECT}_ecDNA_counts.tsv'),
           '#sample\tecDNA_count\nCOLO320DM_LC\t1\nCOLO320DM_hg19\t1\n')
    _write(os.path.join(classification, f'{PROJECT}_feature_entropy.tsv'),
           'sample\tamplicon\tfeature\nCOLO320DM_LC\tamplicon1\tecDNA_1\n'
           'COLO320DM_hg19\tamplicon1\tecDNA_1\n')
    _write(os.path.join(classification, f'{PROJECT}_result_table.tsv'),
           'Sample name\tAA amplicon number\tFeature ID\n'
           'COLO320DM_LC\t1\tCOLO320DM_LC_amplicon1_ecDNA_1\n'
           'COLO320DM_hg19\t1\tCOLO320DM_hg19_amplicon1_ecDNA_1\n')
    # Identified only by feature ID -- no sample column at all.
    _write(os.path.join(classification, f'{PROJECT}_feature_basic_properties.tsv'),
           'feature_ID\tcaptured_region_size_bp\n'
           'COLO320DM_LC_amplicon1_ecDNA_1\t1580609\n'
           'COLO320DM_hg19_amplicon1_ecDNA_1\t980000\n')

    for sample in ('COLO320DM_LC', 'COLO320DM_hg19'):
        _write(os.path.join(classification, f'{PROJECT}_classification_bed_files',
                            f'{sample}_amplicon1_ecDNA_1_intervals.bed'), 'chr8\t1\t2\n')
        _write(os.path.join(classification, f'{PROJECT}_SV_summaries',
                            f'{sample}_amplicon1_SV_summary.tsv'), 'sv\tcount\n')

    return root


def _mentions(root, sample):
    """Every path under root whose name or contents still names the sample."""
    found = []
    for directory, _subdirs, files in os.walk(root):
        if sample in os.path.basename(directory):
            found.append(directory)
        for name in files:
            path = os.path.join(directory, name)
            if sample in name:
                found.append(path)
                continue
            with open(path, encoding='utf-8') as handle:
                if sample in handle.read():
                    found.append(path)
    return found


def test_the_removed_sample_is_nowhere_in_the_archive(results_dir):
    remove_samples_from_results(results_dir, ['COLO320DM_LC'])

    assert _mentions(results_dir, 'COLO320DM_LC') == []


def test_the_surviving_sample_is_untouched(results_dir):
    """The failure that matters more than leftovers: taking the wrong rows out."""
    remove_samples_from_results(results_dir, ['COLO320DM_LC'])

    survivor = 'COLO320DM_hg19'
    assert os.path.isdir(os.path.join(results_dir, 'samples', survivor))

    with open(os.path.join(results_dir, 'run.json'), encoding='utf-8') as handle:
        runs = json.load(handle)['runs']
    assert [feature['Sample name']
            for features in runs.values()
            for feature in features] == [survivor]

    classification = os.path.join(results_dir, 'consolidated_classification')
    for name in sorted(os.listdir(classification)):
        path = os.path.join(classification, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        assert len(lines) == 2, \
            f'{name} should have kept its header and one row, has {len(lines)}'
        assert survivor in lines[1]


def test_a_sample_whose_name_prefixes_another_takes_only_its_own(tmp_path):
    """HCC827 and HCC827_res are a plausible pair, and a bare prefix match on
    'HCC827_' would take the second one out with the first."""
    root = str(tmp_path / 'results')
    classification = os.path.join(root, 'consolidated_classification')

    for sample in ('HCC827', 'HCC827_res'):
        _write(os.path.join(root, 'samples', sample, f'{sample}_CNV_CALLS.bed'), 'x\n')
        _write(os.path.join(classification, 'p_classification_bed_files',
                            f'{sample}_amplicon1_ecDNA_1_intervals.bed'), 'x\n')
    _write(os.path.join(classification, 'p_result_table.tsv'),
           'Sample name\tFeature ID\n'
           'HCC827\tHCC827_amplicon1_ecDNA_1\n'
           'HCC827_res\tHCC827_res_amplicon1_ecDNA_1\n')

    remove_samples_from_results(root, ['HCC827'])

    assert os.path.isdir(os.path.join(root, 'samples', 'HCC827_res'))
    assert not os.path.isdir(os.path.join(root, 'samples', 'HCC827'))
    assert os.path.exists(os.path.join(
        classification, 'p_classification_bed_files',
        'HCC827_res_amplicon1_ecDNA_1_intervals.bed'))
    with open(os.path.join(classification, 'p_result_table.tsv'),
              encoding='utf-8') as handle:
        rows = [line for line in handle.read().splitlines() if line.strip()]
    assert len(rows) == 2 and rows[1].startswith('HCC827_res')


def test_older_archives_are_still_stripped(tmp_path):
    """Projects uploaded before the samples/ layout still have those archives,
    and are still editable."""
    root = str(tmp_path / 'results')
    _write(os.path.join(root, 'other_files', 'OLD_SAMPLE_classification',
                        'result.tsv'), 'x\n')
    _write(os.path.join(root, 'AA_outputs', 'OLD_SAMPLE_AA_results',
                        'graph.txt'), 'x\n')
    _write(os.path.join(root, 'AA_outputs', 'extracted_from_zips',
                        'OLD_SAMPLE_AA_results', 'graph.txt'), 'x\n')
    _write(os.path.join(root, 'other_files', 'KEEP_ME_classification',
                        'result.tsv'), 'x\n')

    remove_samples_from_results(root, ['OLD_SAMPLE'])

    assert _mentions(root, 'OLD_SAMPLE') == []
    assert os.path.isdir(os.path.join(root, 'other_files', 'KEEP_ME_classification'))


def test_removing_nothing_changes_nothing(results_dir):
    before = sorted(
        os.path.join(directory, name)
        for directory, _subdirs, files in os.walk(results_dir)
        for name in files
    )

    summary = remove_samples_from_results(results_dir, [])

    after = sorted(
        os.path.join(directory, name)
        for directory, _subdirs, files in os.walk(results_dir)
        for name in files
    )
    assert before == after
    assert summary == {'directories': [], 'files': 0, 'rows': 0}
