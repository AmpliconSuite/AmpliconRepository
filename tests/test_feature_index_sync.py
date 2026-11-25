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


# ---------------------------------------------------------------------------
# Two writers of one project at once
# ---------------------------------------------------------------------------

def _overlap_project(name):
    from caper.project_status import LIVE, status_flags
    return {
        'project_name': 'pytest index overlap', 'private': 'private',
        'project_members': [], **status_flags(LIVE),
        'runs': {'r': [{'Sample_name': name, 'Feature_ID': 'f', 'AA_amplicon_number': 1,
                        'Classification': 'ecDNA'}]},
    }


@pytest.fixture
def overlap_project():
    from caper import feature_index, project_events

    created = project_events.collection_handle_primary.insert_one(
        _overlap_project('OLD-746'))
    project_id = created.inserted_id
    yield project_id
    project_events.collection_handle_primary.delete_one({'_id': project_id})
    feature_index.unindex_project(project_id)
    feature_index.writer_handle.delete_one({'_id': project_id})


def _indexed_names(project_id):
    from caper import feature_index
    return (sorted(r['sample_name'] for r in feature_index.feature_index_handle.find(
                {'project_id': project_id})),
            sorted(r['sample_name'] for r in feature_index.sample_index_handle.find(
                {'project_id': project_id})))


@pytest.mark.parametrize('wait_seconds', [0, 20])
def test_an_older_writer_cannot_leave_its_rows_behind_a_newer_one(
        overlap_project, monkeypatch, wait_seconds):
    """Reproduced locally 2026-11-24 (#746): the slower writer had read the
    project before an edit and wrote after the edit's own reindex, so the index
    served the old sample name while the document held the new one.

    The newer writer either gives up waiting (0 s) and leaves its change to the
    writer that holds the project, or waits (20 s) and runs after it.
    """
    import threading

    from caper import feature_index, project_events

    read_old, release = threading.Event(), threading.Event()
    original = feature_index.index_project

    def paused_if_old(project):
        if project['runs']['r'][0]['Sample_name'] == 'OLD-746':
            read_old.set()
            assert release.wait(30)
        return original(project)

    monkeypatch.setattr(feature_index, 'index_project', paused_if_old)
    monkeypatch.setattr(feature_index, 'WRITER_WAIT_SECONDS', wait_seconds)

    older = threading.Thread(target=project_events.reindex_project, args=(overlap_project,))
    older.start()
    assert read_old.wait(10)
    project_events.collection_handle_primary.update_one(
        {'_id': overlap_project}, {'$set': {'runs': _overlap_project('NEW-746')['runs']}})
    newer = threading.Thread(target=project_events.reindex_project, args=(overlap_project,))
    newer.start()
    newer.join(1.5)
    release.set()
    older.join(30)
    newer.join(30)

    assert _indexed_names(overlap_project) == (['NEW-746'], ['NEW-746'])
    source = project_events.collection_handle_primary.find_one({'_id': overlap_project})
    assert (manifest_handle.find_one({'project_id': overlap_project})['digest']
            == feature_index.project_digest(source))


def test_a_writer_that_died_does_not_block_the_next_one(overlap_project):
    import datetime

    from caper import feature_index, project_events

    feature_index.writer_handle.insert_one({
        '_id': overlap_project, 'requested': 1, 'owner': 'dead',
        'lease_until': datetime.datetime.utcnow() - datetime.timedelta(seconds=1)})
    project_events.reindex_project(overlap_project)
    assert _indexed_names(overlap_project) == (['OLD-746'], ['OLD-746'])
    assert feature_index.writer_handle.find_one({'_id': overlap_project}) is None


def test_a_write_that_fails_part_way_reads_as_stale(overlap_project, monkeypatch):
    """Otherwise the old digest still matches the document and the drift check
    calls an index with missing rows current."""
    from caper import feature_index, project_events

    project_events.reindex_project(overlap_project)

    class _Fails:
        def insert_many(self, rows):
            raise RuntimeError('disk full')

        def __getattr__(self, name):
            return getattr(feature_index_handle, name)

    with monkeypatch.context() as patch:
        patch.setattr(feature_index, 'feature_index_handle', _Fails())
        assert project_events.reindex_project(overlap_project) is None
    assert overlap_project in feature_index_drift()['stale']
    project_events.reindex_project(overlap_project)
    assert overlap_project not in feature_index_drift()['stale']


@pytest.fixture
def scratch_index(monkeypatch):
    """Every collection a rebuild touches, replaced by empty ones: a rebuild's
    cleanup is corpus-wide, so it must not see the real index."""
    from caper import feature_index, project_events

    prefix = 'pytest_746_' + str(ObjectId()) + '_'
    database = feature_index.manifest_handle.database
    created = []
    for attribute in ('collection_handle_primary', 'feature_index_handle', 'sample_index_handle',
                      'manifest_handle', 'search_names_handle', 'writer_handle'):
        handle = database[prefix + attribute]
        created.append(handle)
        monkeypatch.setattr(feature_index, attribute, handle, raising=False)
    monkeypatch.setattr(project_events, 'collection_handle_primary',
                        feature_index.collection_handle_primary)
    feature_index.ensure_feature_index_indexes()
    yield feature_index
    for handle in created:
        assert handle.name.startswith('pytest_746_')
        handle.drop()


def test_a_full_rebuild_does_not_remove_a_project_indexed_while_it_runs(scratch_index, monkeypatch):
    """The rebuild's list of projects is read once; one created after that is
    absent from it, and the cleanup used to unindex whatever was absent."""
    from caper import project_events

    projects = scratch_index.collection_handle_primary
    first = projects.insert_one(_overlap_project('FIRST-746')).inserted_id
    created_during = []
    real = scratch_index.index_project

    def create_another_while_indexing(project):
        result = real(project)
        if not created_during:
            created_during.append(projects.insert_one(_overlap_project('DURING-746')).inserted_id)
            project_events.reindex_project(created_during[0])
        return result

    # The cursor may or may not return a document inserted behind it; a list
    # taken first makes this deterministic.
    class _Snapshot:
        def find(self, *args, **kwargs):
            return list(projects.find(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(projects, name)

    monkeypatch.setattr(scratch_index, 'index_project', create_another_while_indexing)
    monkeypatch.setattr(scratch_index, 'collection_handle_primary', _Snapshot())
    scratch_index.rebuild_feature_index()

    assert sorted(r['sample_name'] for r in scratch_index.feature_index_handle.find({})) == [
        'DURING-746', 'FIRST-746']
    assert scratch_index.manifest_handle.count_documents({}) == 2
    assert first
