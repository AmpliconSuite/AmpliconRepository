"""The index has to still be right after a real upload, and after a real delete.

Every other test of the index checks the builder against a document it was
handed. This one checks the part that actually breaks: whether the hooks fire
on the paths a user takes, and whether the drift check tells the truth
afterwards. It is the difference between "the builder is correct" and "the
index is current", and only the second one matters to a search.
"""
import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
)

from caper import views
from caper.feature_index import (
    feature_index_drift,
    feature_index_handle,
    feature_rows_for_project,
    manifest_handle,
)
from caper.project_events import reindex_project


@pytest.fixture
def uploaded_project(request_factory, test_user, mongo_collection):
    """Create a project the way the site does, and clean it up afterwards."""
    name = 'pytest feature index sync'
    request, handles = _build_create_request(
        request_factory, test_user, name, tar_path=DATASET_SMALL_TAR)
    try:
        response = views.create_project(request)
        project_id = _project_id_from_redirect(response)
        assert project_id, 'upload did not redirect to a project'
        document = _poll_until_finished(mongo_collection, project_id)
        assert document is not None, 'upload never finished'
        assert not document.get('aggregation_failed'), document.get('error_message')
        yield project_id, document
    finally:
        for handle in handles:
            handle.close()
        try:
            _cleanup_project(mongo_collection, project_id)
        except Exception:
            pass
        feature_index_handle.delete_many({'project_id': ObjectId(project_id)})
        manifest_handle.delete_one({'project_id': ObjectId(project_id)})


@pytest.mark.slow
def test_upload_leaves_the_project_indexed(uploaded_project):
    """The rows have to exist after the *extraction* finishes, not after insert.

    The document is inserted before its samples are written, so a hook that
    fired only at insert would index a project with no runs and leave it
    permanently empty in search while looking perfectly current.
    """
    project_id, document = uploaded_project
    rows = list(feature_index_handle.find({'project_id': ObjectId(project_id)}))
    expected = feature_rows_for_project(document)

    assert expected, 'the fixture project has no features to index'
    assert len(rows) == len(expected)
    assert {row['sample_name'] for row in rows} == {row['sample_name'] for row in expected}


@pytest.mark.slow
def test_upload_leaves_no_drift(uploaded_project):
    """The standing check has to agree that the index is current."""
    project_id, _ = uploaded_project
    drift = feature_index_drift()
    object_id = ObjectId(project_id)

    assert object_id not in drift['missing']
    assert object_id not in drift['stale']
    assert object_id not in drift['orphaned']


@pytest.mark.slow
def test_deleting_the_project_takes_its_rows_out(uploaded_project, mongo_collection):
    """A soft-deleted project must stop being a search result.

    Rows outliving the project is the failure that shows up as a search
    returning a sample whose page 404s, which is worse than the search being
    slow.
    """
    project_id, _ = uploaded_project
    assert feature_index_handle.count_documents({'project_id': ObjectId(project_id)}) > 0

    mongo_collection.update_one({'_id': ObjectId(project_id)},
                                {'$set': {'delete': True, 'current': False}})
    reindex_project(project_id)

    assert feature_index_handle.count_documents({'project_id': ObjectId(project_id)}) == 0


@pytest.mark.slow
def test_a_project_can_come_back(uploaded_project, mongo_collection):
    """Reindexing asks the database, so a restore restores the rows too.

    This is why the primitive takes an id rather than a document: the same call
    that removed the rows puts them back, without the caller having to know
    which direction it is going.
    """
    project_id, _ = uploaded_project
    mongo_collection.update_one({'_id': ObjectId(project_id)},
                                {'$set': {'delete': True, 'current': False}})
    reindex_project(project_id)
    assert feature_index_handle.count_documents({'project_id': ObjectId(project_id)}) == 0

    mongo_collection.update_one({'_id': ObjectId(project_id)},
                                {'$set': {'delete': False, 'current': True}})
    reindex_project(project_id)
    assert feature_index_handle.count_documents({'project_id': ObjectId(project_id)}) > 0


# ---------------------------------------------------------------------------
# Where the reindex reads from
# ---------------------------------------------------------------------------

def test_the_reindex_reads_the_primary_not_a_replica():
    """The hook runs immediately after the write that triggered it.

    The cluster URI ends ``readPreference=secondaryPreferred``, so the default
    handle can serve the replica's pre-write copy -- and the reindex would
    then index the document as it was *before* the change that called it,
    reporting success and leaving the search one edit behind.

    That is not hypothetical. Measured on prod on 2026-09-12: three projects
    stale, each with an index manifest written 2 to 4 seconds after its
    document, each missing exactly the content that write had added. Two were
    metadata sheets whose cancer types could then not be filtered on at all.
    """
    from pymongo import ReadPreference
    from caper import project_events, feature_index

    for module in (project_events, feature_index):
        handle = getattr(module, 'collection_handle_primary')
        assert handle.read_preference == ReadPreference.PRIMARY, module.__name__
        # And the replica-preferring handle is not reachable to be used by
        # mistake: importing it is how this regressed in the first place.
        assert not hasattr(module, 'collection_handle'), module.__name__


def test_the_reindex_uses_that_handle_to_read_its_source(monkeypatch):
    """Behavioural half: the read actually goes through the pinned handle."""
    from caper import project_events

    project_id = ObjectId()
    calls = []

    class _Pinned:
        def find_one(self, query, projection=None):
            calls.append(query)
            return None       # not indexable -> unindex, no rows written

    monkeypatch.setattr(project_events, 'collection_handle_primary', _Pinned())
    monkeypatch.setattr(project_events.feature_index, 'unindex_project',
                        lambda oid: None)
    assert project_events.reindex_project(project_id) == 0
    assert calls and calls[0]['_id'] == project_id
