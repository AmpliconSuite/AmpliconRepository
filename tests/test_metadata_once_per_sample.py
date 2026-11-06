"""A sample's uploaded metadata is stored on one feature row, and old versions hold it on all.

Everything that reads metadata has to give the same answer for both forms, because
old versions stay as they were written.
"""

import copy

import bson
import pandas as pd
import pytest

from caper.extra_metadata import (
    _apply_metadata_to_runs,
    _build_metadata_lookup_from_dataframe,
    get_extra_metadata_from_project,
    has_sample_metadata,
    metadata_coverage,
    summarize_project_metadata,
)
from caper.sample_metadata_rows import (
    METADATA_KEY,
    sample_metadata_of,
    with_metadata_on_every_row,
)

LUNG = {'cancer_type': 'Lung', 'cancer_subtype': 'Lung adenocarcinoma'}
BREAST = {'cancer_type': 'Breast', 'cancer_subtype': 'Breast carcinoma'}


def _rows(name, count):
    return [{'Sample_name': name, 'Feature_ID': f'{name}_{i}', 'Classification': 'ecDNA',
             'All_genes': ['EGFR'], 'Oncogenes': ['EGFR'], 'Sample_type': '', 'Cancer_type': '',
             'Tissue_of_origin': ''} for i in range(count)]


def _sheet(rows):
    return _build_metadata_lookup_from_dataframe(pd.DataFrame(rows))


def _new_form():
    runs = {'sample_1': _rows('S1', 3), 'sample_2': _rows('S2', 2), 'sample_3': _rows('S3', 1)}
    _apply_metadata_to_runs(runs, _sheet([dict(sample_name='S1', **LUNG),
                                          dict(sample_name='S2', **BREAST)]))
    return runs


def _old_form():
    """The same project as a version written before storing once per sample."""
    runs = _new_form()
    for rows in runs.values():
        metadata = sample_metadata_of(rows)
        if metadata:
            for row in rows:
                row[METADATA_KEY] = dict(metadata)
    return runs


def _project(runs):
    return {'_id': bson.ObjectId(), 'project_name': 'P', 'private': 'public',
            'project_members': [], 'runs': runs}


# --- what gets stored ------------------------------------------------------

def test_upload_stores_metadata_on_one_row_per_sample():
    runs = _new_form()
    assert [METADATA_KEY in row for row in runs['sample_1']] == [True, False, False]
    assert [METADATA_KEY in row for row in runs['sample_2']] == [True, False]
    assert METADATA_KEY not in runs['sample_3'][0]
    assert runs['sample_1'][0][METADATA_KEY]['cancer_subtype'] == 'Lung adenocarcinoma'
    assert runs['sample_1'][0][METADATA_KEY]['original_sample_name'] == 'S1'


def test_the_lifted_fields_stay_on_every_row():
    """Search and the sample tables read these per row."""
    assert {row['Cancer_type'] for row in _new_form()['sample_1']} == {'Lung'}


def test_it_makes_the_document_smaller():
    old, new = len(bson.encode({'r': _old_form()})), len(bson.encode({'r': _new_form()}))
    assert new < old


def test_applying_to_an_old_form_version_converts_it_and_new_values_win():
    runs = {'sample_1': _rows('S1', 3)}
    for row in runs['sample_1']:
        row[METADATA_KEY] = {'cancer_type': 'Old', 'kept': 'yes'}
    runs['sample_1'][2][METADATA_KEY]['only_on_last_row'] = 'x'

    _apply_metadata_to_runs(runs, _sheet([{'sample_name': 'S1', 'cancer_type': 'New'}]))

    assert [METADATA_KEY in row for row in runs['sample_1']] == [True, False, False]
    assert runs['sample_1'][0][METADATA_KEY] == {
        'cancer_type': 'New', 'kept': 'yes', 'only_on_last_row': 'x', 'original_sample_name': 'S1'}


def test_a_sample_left_out_of_the_sheet_keeps_what_it_had():
    runs = {'sample_1': _rows('S1', 2)}
    for row in runs['sample_1']:
        row[METADATA_KEY] = {'cancer_type': 'Lung'}
    _apply_metadata_to_runs(runs, _sheet([{'sample_name': 'other', 'cancer_type': 'New'}]))
    assert [row[METADATA_KEY] for row in runs['sample_1']] == [{'cancer_type': 'Lung'}] * 2


def test_a_blank_cell_stores_nothing_and_clears_an_older_value():
    runs = {'sample_1': _rows('S1', 1)}
    runs['sample_1'][0][METADATA_KEY] = {'stale': 'old', 'kept': 'yes'}
    sheet = pd.DataFrame([{'sample_name': 'S1', 'cancer_type': 'Lung', 'stale': float('nan'), 'empty': '  '}])
    _apply_metadata_to_runs(runs, _build_metadata_lookup_from_dataframe(sheet))
    assert runs['sample_1'][0][METADATA_KEY] == {
        'cancer_type': 'Lung', 'kept': 'yes', 'original_sample_name': 'S1'}


def test_retained_metadata_survives_a_new_version():
    first = _new_form()
    retained = get_extra_metadata_from_project({'runs': first})
    fresh = {'sample_1': _rows('S1', 3), 'sample_2': _rows('S2', 2)}
    _apply_metadata_to_runs(fresh, _sheet([]) or {'S1': retained['S1'], 'S2': retained['S2']},
                            old_extra_metadata=retained)
    assert [METADATA_KEY in row for row in fresh['sample_1']] == [True, False, False]
    assert fresh['sample_1'][0][METADATA_KEY]['cancer_subtype'] == 'Lung adenocarcinoma'


def test_remap_renames_every_row_and_stores_metadata_once():
    runs = {'sample_1': _rows('S1', 2)}
    _apply_metadata_to_runs(
        runs, _sheet([{'sample_name': 'S1', 'sample_name_alias': 'Renamed', 'cancer_type': 'Lung'}]),
        remap_name_to_alias=True)
    assert [row['Sample_name'] for row in runs['sample_1']] == ['Renamed', 'Renamed']
    assert [METADATA_KEY in row for row in runs['sample_1']] == [True, False]
    assert get_extra_metadata_from_project({'runs': runs})['Renamed']['original_sample_name'] == 'S1'


# --- the helpers ------------------------------------------------------------

def test_filling_rows_does_not_touch_what_is_stored():
    runs = _new_form()
    before = copy.deepcopy(runs)
    filled = with_metadata_on_every_row(runs)
    assert runs == before
    assert all(row.get(METADATA_KEY) for row in filled['sample_1'])
    assert METADATA_KEY not in filled['sample_3'][0]


def test_filling_rows_does_not_lend_metadata_across_sample_names():
    rows = _rows('A', 1) + _rows('B', 1)
    rows[0][METADATA_KEY] = dict(LUNG)
    filled = with_metadata_on_every_row({'k': rows})['k']
    assert METADATA_KEY not in filled[1]


def test_an_old_form_version_is_returned_as_it_is():
    runs = _old_form()
    assert with_metadata_on_every_row(runs)['sample_1'] is runs['sample_1']


def test_sample_metadata_is_found_on_any_row():
    rows = _rows('S1', 3)
    rows[2][METADATA_KEY] = dict(LUNG)
    assert sample_metadata_of(rows) == LUNG
    assert sample_metadata_of(_rows('S1', 2)) is None
    assert sample_metadata_of([]) is None


# --- every reader gives the same answer for both forms ------------------------

@pytest.fixture(params=['new', 'old'])
def runs(request):
    return _new_form() if request.param == 'new' else _old_form()


def test_project_level_readers(runs):
    project = _project(runs)
    assert has_sample_metadata(project)
    extra = get_extra_metadata_from_project(project)
    assert extra['S1']['cancer_subtype'] == 'Lung adenocarcinoma' and 'S3' not in extra
    assert metadata_coverage(project)['covered'] == 2
    assert summarize_project_metadata(project) == summarize_project_metadata(_project(_old_form()))


def test_coverage_counts_metadata_held_on_a_later_row():
    rows = _rows('S1', 2)
    rows[1][METADATA_KEY] = dict(LUNG)
    assert metadata_coverage(_project({'sample_1': rows}))['covered'] == 1


def test_sample_page_reader_finds_it_whatever_order_the_rows_come_in(runs):
    from caper.views import get_sample_metadata
    rows = [dict(row, Sample_metadata_JSON='Not Provided') for row in reversed(runs['sample_1'])]
    assert get_sample_metadata(rows)['cancer_subtype'] == 'Lung adenocarcinoma'


def test_feature_index_gives_every_row_its_samples_metadata():
    from caper.feature_index import feature_rows_for_project
    new = feature_rows_for_project(_project(_new_form()))
    old = feature_rows_for_project(_project(_old_form()))
    assert len(new) == 6
    assert [row['extra_metadata'] for row in new] == [row['extra_metadata'] for row in old]
    assert [row['metadata'] for row in new] == [row['metadata'] for row in old]
    s1 = [row for row in new if row['sample_name'] == 'S1']
    assert all(row['extra_metadata'].get('cancer_subtype') == 'Lung adenocarcinoma' for row in s1)
    assert all(row['metadata']['Cancer_type'] == 'Lung' for row in s1)


def _search(runs, query):
    from caper.search import get_samples_from_features
    return get_samples_from_features(
        [_project(runs)], None, None, None, None, None, None, query, no_filter=True)


@pytest.mark.parametrize('query, sample, features', [
    ('adenocarcinoma', 'S1', 3),
    ('Breast carcinoma', 'S2', 2),
])
def test_free_text_search_finds_every_feature_of_the_sample(runs, query, sample, features):
    found = _search(runs, query)
    assert {row['Sample_name'] for row in found} == {sample}
    assert len(found) == features
