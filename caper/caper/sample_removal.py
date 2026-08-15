"""Removing samples from an aggregated project archive.

When a sample is removed from a project, it has to leave the archive as well as
the database.  The archive is what people download and what the aggregator is
handed when the project is re-aggregated, so a sample left in it comes back --
and, until it does, ships to anyone who downloads the project after being told
it was removed.

The layout this works on is AmpliconSuiteAggregator's::

    results/
        run.json
        aggregated_results.csv
        samples/<sample>/...
        consolidated_classification/
            <project>_amplicon_classification_profiles.tsv
            <project>_result_table.tsv
            <project>_gene_list.tsv                     (and other tables)
            <project>_classification_bed_files/<sample>_amplicon1_ecDNA_1_intervals.bed
            <project>_SV_summaries/<sample>_amplicon1_SV_summary.tsv
            <project>_annotated_cycles_files/...

Three shapes of thing mention a sample, and each is handled by the property that
identifies it rather than by a list of filenames, so a table added to a future
aggregator release is filtered too:

  * a directory named for the sample, under ``samples/``;
  * a file named ``<sample>_amplicon<N>_...``, in the per-feature directories;
  * a row in a delimited table, found either by a column holding sample names or
    by a column holding feature IDs, which begin ``<sample>_amplicon``.

The ``_amplicon`` boundary matters: a project may hold both ``HCC827`` and
``HCC827_res``, and a bare ``HCC827_`` prefix would take the second one with the
first.

Anything not recognised is left alone.  The aggregator rewrites the whole
consolidated_classification directory on the next run anyway; the job here is to
leave behind an archive that no longer describes the sample, not to produce
byte-perfect tables.
"""

import csv
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# Headers used for the sample name across the aggregator's tables.  Compared
# case-insensitively with surrounding punctuation stripped, so 'Sample name',
# 'sample_name', 'sample' and '#sample' all match.
_SAMPLE_HEADERS = {'sample', 'sample name', 'sample_name', 'sname'}

# Headers holding a feature ID, which begins with the sample name.  Used for
# tables that identify a row only by its feature, such as
# <project>_feature_basic_properties.tsv.
_FEATURE_HEADERS = {'feature id', 'feature_id', 'featureid'}


def _normalize(header):
    return header.strip().lstrip('#').strip().lower()


def _belongs_to(value, samples):
    """True when a sample-name cell names one of the samples being removed."""
    return value.strip() in samples


def _feature_belongs_to(value, samples):
    """True when a feature ID belongs to one of the samples being removed."""
    feature = value.strip()
    return any(feature.startswith(f'{sample}_amplicon') for sample in samples)


def _filter_table(path, samples, delimiter):
    """Drop the removed samples' rows from one delimited table, in place.

    Returns the number of rows dropped, or None when the table has no column
    that identifies a sample and so is left untouched.
    """
    with open(path, newline='', encoding='utf-8') as handle:
        rows = list(csv.reader(handle, delimiter=delimiter))

    if not rows:
        return None

    header = [_normalize(cell) for cell in rows[0]]
    sample_column = next((i for i, name in enumerate(header)
                          if name in _SAMPLE_HEADERS), None)
    feature_column = next((i for i, name in enumerate(header)
                           if name in _FEATURE_HEADERS), None)

    if sample_column is not None:
        column, matches = sample_column, _belongs_to
    elif feature_column is not None:
        column, matches = feature_column, _feature_belongs_to
    else:
        logger.warning('No sample column in %s; leaving it as it is', path)
        return None

    kept = [rows[0]] + [row for row in rows[1:]
                        if len(row) <= column or not matches(row[column], samples)]
    dropped = len(rows) - len(kept)

    if dropped:
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            csv.writer(handle, delimiter=delimiter).writerows(kept)

    return dropped


def _filter_run_json(path, samples):
    """Drop the removed samples' features from run.json.

    run.json is keyed by run ('sample_1', 'sample_2', ...) rather than by sample
    name, so the sample is found on the features inside; a run left with no
    features goes with them.
    """
    with open(path, encoding='utf-8') as handle:
        document = json.load(handle)

    runs = document.get('runs')
    if not isinstance(runs, dict):
        return 0

    dropped = 0
    surviving = {}
    for run_key, features in runs.items():
        if not isinstance(features, list):
            surviving[run_key] = features
            continue
        kept = [feature for feature in features
                if str(feature.get('Sample name', '')).strip() not in samples]
        dropped += len(features) - len(kept)
        if kept:
            surviving[run_key] = kept

    if dropped:
        document['runs'] = surviving
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(document, handle, indent=2)

    return dropped


def remove_samples_from_results(results_dir, samples_to_remove):
    """Remove samples from an extracted ``results/`` directory, in place.

    Returns a summary dict for the log: which sample directories were removed,
    how many per-feature files were deleted, and how many table rows were
    dropped.
    """
    samples = {sample.strip() for sample in samples_to_remove if sample and sample.strip()}
    summary = {'directories': [], 'files': 0, 'rows': 0}
    if not samples:
        return summary

    # 1. The sample's own directory.  The last three are where archives written
    # before the samples/ layout kept per-sample data; projects uploaded then
    # still have those archives, and are still editable.
    for sample in samples:
        for relative in (os.path.join('samples', sample),
                         os.path.join('other_files', f'{sample}_classification'),
                         os.path.join('AA_outputs', f'{sample}_AA_results'),
                         os.path.join('AA_outputs', 'extracted_from_zips',
                                      f'{sample}_AA_results')):
            sample_dir = os.path.join(results_dir, relative)
            if os.path.isdir(sample_dir):
                shutil.rmtree(sample_dir)
                summary['directories'].append(relative)

    # 2. Per-feature files, which are named for the sample, and 3. the tables.
    classification_dir = os.path.join(results_dir, 'consolidated_classification')
    for root, _dirs, files in os.walk(classification_dir):
        for name in files:
            path = os.path.join(root, name)
            if _feature_belongs_to(name, samples):
                os.remove(path)
                summary['files'] += 1
            elif name.endswith(('.tsv', '.csv')):
                dropped = _filter_table(path, samples,
                                        '\t' if name.endswith('.tsv') else ',')
                summary['rows'] += dropped or 0

    for name, delimiter in (('aggregated_results.csv', ','),
                            ('aggregated_results.tsv', '\t')):
        path = os.path.join(results_dir, name)
        if os.path.exists(path):
            summary['rows'] += _filter_table(path, samples, delimiter) or 0

    run_json = os.path.join(results_dir, 'run.json')
    if os.path.exists(run_json):
        summary['rows'] += _filter_run_json(run_json, samples)

    return summary
