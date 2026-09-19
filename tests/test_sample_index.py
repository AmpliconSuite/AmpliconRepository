"""The sample page reads one sample from an indexed copy, not from the project.

``get_one_sample`` used to ask the server to ``$objectToArray`` the whole
``runs`` dict to return one sample's rows, so a sample page cost what the
project weighed: measured on prod 2026-09-18, 1,033 ms for a 7-row sample of
Hartwig (4,170 samples) and 1.6 ms for the same rows from an indexed
collection.  ``feature_index.sample_documents_for_project`` is that copy.

The contract these tests pin down:

  * the copy holds the rows verbatim, one document per run, empty runs kept
  * the indexed read returns exactly what the aggregation returns -- rows,
    prev and next -- for every sample of a project, including its quirks
  * the index declines rather than guesses: unindexed project, older builder,
    unknown sample all fall back to the aggregation, which still exists
  * the sync hooks write and remove the copy with the rows

Parity is asserted against the old path directly rather than against a
hand-written expectation, so the test says the two paths agree, which is the
property the fallback depends on.
"""

import pytest
from bson.objectid import ObjectId

from caper import feature_index, utils
from caper.feature_index import (
    DERIVED_COLLECTIONS,
    DERIVED_DRIFT_CHECKS,
    SAMPLE_INDEX_COLLECTION,
    SCHEMA_VERSION,
    index_project,
    sample_documents_for_project,
    sample_index_handle,
    sample_slice_from_index,
    unindex_project,
)


def _feature(sample_name, feature_id, **extra):
    row = {
        'Sample_name': sample_name,
        'Feature_ID': feature_id,
        'AA_amplicon_number': 1,
        'Classification': 'ecDNA',
        'Location': "['chr1:1000-2000']",
        'Reference_version': 'hg38',
        'AA_PNG_file': ObjectId(),
        'AA_PDF_file': ObjectId(),
        'CNV_BED_file': ObjectId(),
        'Feature_BED_file': ObjectId(),
        'Sample_metadata_JSON': ObjectId(),
        'All_genes': "['MYC', 'PVT1']",
        'Oncogenes': "['MYC']",
    }
    row.update(extra)
    return row


# Run keys deliberately out of step with sample names, an empty run in the
# middle of the order, two runs sharing a Sample_name, and a key with a space
# in it -- every shape the aggregation path has had to be taught about.
RUNS = {
    'run_03': [_feature('gamma', 'gamma_amplicon1')],
    'run_01': [_feature('alpha', 'alpha_amplicon1'),
               _feature('alpha', 'alpha_amplicon2'),
               _feature('alpha', 'alpha_amplicon3', **{'Sample type': 'cell line'})],
    'run_02': [],
    'run_04': [_feature('delta', 'delta_amplicon1')],
    'run_05': [_feature('delta', 'delta_amplicon1_again')],
}


def _project_doc(name, runs, creator):
    return {
        'project_name': name,
        'creator': creator,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': runs,
        'sample_count': len(runs),
    }


@pytest.fixture
def indexed_project(mongo_collection, test_user):
    """A project inserted the way the older tests do it, then indexed."""
    result = mongo_collection.insert_one(
        _project_doc('pytest sample index', RUNS, test_user.username))
    project_id = result.inserted_id
    mongo_collection.update_one({'_id': project_id}, {'$set': {'linkid': str(project_id)}})
    document = mongo_collection.find_one({'_id': project_id})
    index_project(document)
    try:
        yield document
    finally:
        unindex_project(project_id)
        mongo_collection.delete_one({'_id': project_id})


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def test_one_document_per_run_with_the_rows_verbatim():
    docs = {d['run_key']: d for d in sample_documents_for_project(
        {'_id': ObjectId(), 'runs': RUNS})}
    assert set(docs) == set(RUNS)
    for run_key, features in RUNS.items():
        assert docs[run_key]['features'] == features
        assert docs[run_key]['feature_count'] == len(features)
        assert docs[run_key]['schema_version'] == SCHEMA_VERSION


def test_an_empty_run_is_kept_and_named_by_its_key():
    """It is a position in the prev/next order, so it cannot be dropped."""
    project_id = ObjectId()
    docs = {d['run_key']: d for d in sample_documents_for_project(
        {'_id': project_id, 'runs': RUNS})}
    assert docs['run_02']['sample_name'] == 'run_02'
    assert docs['run_02']['sample_key'] == f'{project_id}:run_02'
    assert docs['run_02']['features'] == []


def test_the_name_is_the_first_feature_that_has_one():
    """``$$r.v.Sample_name`` skips features without the key; so does this."""
    docs = sample_documents_for_project({'_id': ObjectId(), 'runs': {
        'r': [{'Feature_ID': 'nameless'}, _feature('named', 'named_amplicon1')]}})
    assert docs[0]['sample_name'] == 'named'


def test_runs_that_is_not_a_dict_yields_no_documents():
    assert sample_documents_for_project({'_id': ObjectId(), 'runs': []}) == []
    assert sample_documents_for_project({'_id': ObjectId()}) == []


def test_the_copy_is_a_derived_collection_the_drift_check_covers():
    assert SAMPLE_INDEX_COLLECTION in DERIVED_COLLECTIONS
    assert SAMPLE_INDEX_COLLECTION in DERIVED_DRIFT_CHECKS


# ---------------------------------------------------------------------------
# Parity with the aggregation path
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_indexed_read_matches_the_aggregation_for_every_sample(indexed_project):
    """Rows, prev and next, for every name the project has and one it has not.

    Includes the aggregation's quirks on purpose -- a neighbour that is an
    empty run is ``None``, a duplicated name resolves to the lower run key --
    because the page is served from either path and must not change shape
    depending on which.
    """
    project_id = indexed_project['_id']
    names = ['alpha', 'gamma', 'delta', 'run_02', 'nobody']
    for name in names:
        expected = utils._fetch_sample_slice({'_id': project_id}, name)
        indexed = sample_slice_from_index(project_id, name)
        if expected[0] is None:
            assert indexed is None, f'{name}: index answered where the aggregation found nothing'
        else:
            assert indexed == expected, f'{name}: the two paths disagree'


@pytest.mark.integration
def test_the_quirks_the_parity_test_is_relying_on(indexed_project):
    """Spelled out, so a change to either path fails here with a reason."""
    project_id = indexed_project['_id']
    rows, prev_name, next_name = sample_slice_from_index(project_id, 'alpha')
    assert [r['Feature_ID'] for r in rows] == [
        'alpha_amplicon1', 'alpha_amplicon2', 'alpha_amplicon3']
    assert prev_name is None                 # run_01 is first
    assert next_name is None                 # run_02 is empty: no link, not a skip

    rows, prev_name, next_name = sample_slice_from_index(project_id, 'gamma')
    assert prev_name is None                 # run_02 again, from the other side
    assert next_name == 'delta'

    rows, prev_name, next_name = sample_slice_from_index(project_id, 'delta')
    assert [r['Feature_ID'] for r in rows] == ['delta_amplicon1']   # run_04, not run_05
    assert (prev_name, next_name) == ('gamma', 'delta')             # run_05 is its neighbour

    assert sample_slice_from_index(project_id, 'run_02') is None    # an empty run has no page


@pytest.mark.integration
def test_rows_only_read_skips_the_neighbours(indexed_project):
    rows, prev_name, next_name = sample_slice_from_index(
        indexed_project['_id'], 'gamma', neighbours=False)
    assert rows[0]['Feature_ID'] == 'gamma_amplicon1'
    assert prev_name is None and next_name is None


# ---------------------------------------------------------------------------
# Declining
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_an_unindexed_project_falls_back(mongo_collection, test_user, monkeypatch):
    result = mongo_collection.insert_one(
        _project_doc('pytest sample index unindexed', RUNS, test_user.username))
    project_id = result.inserted_id
    mongo_collection.update_one({'_id': project_id}, {'$set': {'linkid': str(project_id)}})
    calls = []
    original = utils._fetch_sample_slice
    monkeypatch.setattr(utils, '_fetch_sample_slice',
                        lambda *a, **k: calls.append(a) or original(*a, **k))
    try:
        assert sample_slice_from_index(project_id, 'gamma') is None
        _, rows, prev_sample, next_sample = utils.get_one_sample(str(project_id), 'gamma')
        assert rows[0]['Feature_ID'] == 'gamma_amplicon1'
        assert next_sample[0]['Sample_name'] == 'delta'
        assert len(calls) == 1, 'the aggregation path was not what served this'
    finally:
        mongo_collection.delete_one({'_id': project_id})


@pytest.mark.integration
def test_documents_from_an_older_builder_are_not_served(indexed_project):
    """A deploy that bumps SCHEMA_VERSION serves the old way until the rebuild."""
    project_id = indexed_project['_id']
    sample_index_handle.update_many({'project_id': project_id},
                                    {'$set': {'schema_version': SCHEMA_VERSION - 1}})
    assert sample_slice_from_index(project_id, 'gamma') is None
    # And the fallback still answers.
    _, rows, _, _ = utils.get_one_sample(str(project_id), 'gamma')
    assert rows[0]['Feature_ID'] == 'gamma_amplicon1'


@pytest.mark.integration
def test_a_read_error_falls_back_rather_than_failing(indexed_project, monkeypatch):
    from pymongo.errors import OperationFailure

    def broken(*args, **kwargs):
        raise OperationFailure('pytest: index unavailable')
    monkeypatch.setattr(feature_index, 'sample_slice_from_index', broken)
    _, rows, _, next_sample = utils.get_one_sample(str(indexed_project['_id']), 'gamma')
    assert rows[0]['Feature_ID'] == 'gamma_amplicon1'
    assert next_sample[0]['Sample_name'] == 'delta'


# ---------------------------------------------------------------------------
# The callers
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_get_one_sample_is_served_from_the_index(indexed_project, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError('the aggregation ran for an indexed project')
    monkeypatch.setattr(utils, '_fetch_sample_slice', must_not_run)

    project, rows, prev_sample, next_sample = utils.get_one_sample(
        str(indexed_project['_id']), 'delta')
    assert 'runs' not in project
    assert rows[0]['Feature_ID'] == 'delta_amplicon1'
    assert prev_sample[0]['Sample_name'] == 'gamma'
    assert next_sample[0]['Sample_name'] == 'delta'


@pytest.mark.integration
def test_get_one_sample_rows_is_served_from_the_index(indexed_project, monkeypatch):
    class NoAggregate:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def aggregate(self, *args, **kwargs):
            raise AssertionError('the aggregation ran for an indexed project')
    monkeypatch.setattr(utils, 'collection_handle', NoAggregate(utils.collection_handle))

    project, rows = utils.get_one_sample_rows(str(indexed_project['_id']), 'alpha')
    assert 'runs' not in project
    assert len(rows) == 3
    # Space-containing keys are normalised on the way out, as before.
    assert rows[2]['Sample_type'] == 'cell line'
    assert 'Sample type' not in rows[2]


@pytest.mark.integration
def test_the_returned_rows_are_normalised_the_same_way(indexed_project):
    _, rows, _, _ = utils.get_one_sample(str(indexed_project['_id']), 'alpha')
    assert rows[2]['Sample_type'] == 'cell line'
    assert 'Sample type' not in rows[2]
    # And the stored copy was not rewritten by that normalisation.
    stored = sample_index_handle.find_one({'project_id': indexed_project['_id'],
                                           'run_key': 'run_01'})
    assert 'Sample type' in stored['features'][2]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_reindexing_replaces_the_copy(indexed_project, mongo_collection):
    """Delete-then-insert: a run that no longer exists leaves no document."""
    project_id = indexed_project['_id']
    assert sample_index_handle.count_documents({'project_id': project_id}) == len(RUNS)

    renamed = dict(indexed_project)
    renamed['runs'] = {'only_run': [_feature('epsilon', 'epsilon_amplicon1')]}
    index_project(renamed)
    remaining = list(sample_index_handle.find({'project_id': project_id}))
    assert [d['run_key'] for d in remaining] == ['only_run']
    assert sample_slice_from_index(project_id, 'gamma') is None


@pytest.mark.integration
def test_unindexing_removes_the_copy(mongo_collection, test_user):
    document = _project_doc('pytest sample index unindex', RUNS, test_user.username)
    document['_id'] = ObjectId()
    index_project(document)
    assert sample_index_handle.count_documents({'project_id': document['_id']}) == len(RUNS)
    unindex_project(document['_id'])
    assert sample_index_handle.count_documents({'project_id': document['_id']}) == 0
