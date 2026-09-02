from bson import ObjectId

from caper.project_status import matches
from cleanup_orphaned_projects import (
    collect_needs_review_ids,
    collect_protected_ids,
    collect_retained_file_ids,
    delete_gridfs_files_for_project,
    is_resolvable_by_url,
    redact_uri,
)


class FakeCollection:
    """An in-memory stand-in that evaluates queries with project_status.matches().

    It used to carry its own two-line matcher, which was a third copy of query
    semantics in a repo whose defining bug is a predicate maintained twice.
    matches() is the same evaluator the application uses, and
    tests/test_project_status.py checks it against a real MongoDB.
    """

    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection):
        for doc in self.docs:
            if matches(doc, query):
                yield {key: doc[key] for key in projection if key in doc}

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if matches(doc, query):
                return doc
        return None


class FakeGridFS:
    def __init__(self):
        self.deleted = []

    def delete(self, object_id):
        self.deleted.append(str(object_id))


def test_collect_protected_ids_accepts_legacy_string_previous_versions():
    current_id = ObjectId()
    dict_prev_id = ObjectId()
    string_prev_id = ObjectId()
    soft_deleted_id = ObjectId()

    collection = FakeCollection([
        {
            '_id': current_id,
            'delete': False,
            'previous_versions': [
                {'linkid': dict_prev_id},
                str(string_prev_id),
            ],
        },
        {
            '_id': soft_deleted_id,
            'delete': True,
            'current': True,
            'previous_versions': [],
        },
    ])

    assert collect_protected_ids(collection) == {
        str(current_id),
        str(dict_prev_id),
        str(string_prev_id),
        str(soft_deleted_id),
    }


def test_delete_gridfs_files_for_project_handles_current_underscore_keys():
    tar_id = ObjectId()
    png_id = ObjectId()
    graph_id = ObjectId()
    fs = FakeGridFS()
    project = {
        'tarfile': tar_id,
        'runs': {
            'sample1': [{
                'AA_PNG_file': png_id,
                'AA_graph_file': graph_id,
            }],
        },
    }

    assert delete_gridfs_files_for_project(fs.delete, project) == 3
    assert fs.deleted == [str(tar_id), str(png_id), str(graph_id)]


def test_collect_protected_ids_preserves_deleted_version_tombstones():
    tombstone_id = ObjectId()
    collection = FakeCollection([
        {
            '_id': tombstone_id,
            'delete': True,
            'current': False,
            'version_deleted_from_history': True,
            'payload_purged': True,
            'redirect_to_project': str(ObjectId()),
        },
    ])

    assert collect_protected_ids(collection) == {str(tombstone_id)}


# ---------------------------------------------------------------------------
# Regression tests for the defect found on prod 2026-08-25: the script would
# have deleted 84 documents, 14 of which the application could still resolve
# by URL, and 77 of which still held a GridFS payload.
# ---------------------------------------------------------------------------

def test_superseded_versions_are_protected_because_get_one_project_resolves_them():
    """delete=True + current=False is reachable via caper/utils.py:722 and :736.

    This is the exact class the script used to delete. If this test fails, old
    project-version URLs are being destroyed.
    """
    live_id = ObjectId()
    superseded_id = ObjectId()

    collection = FakeCollection([
        {'_id': live_id, 'delete': False, 'current': True},
        # Superseded, and deliberately NOT referenced by previous_versions --
        # that is what made it look orphaned to the old rules.
        {'_id': superseded_id, 'delete': True, 'current': False,
         'project_name': 'Some Older Version'},
    ])

    protected = collect_protected_ids(collection)
    assert str(superseded_id) in protected, (
        "a superseded version resolvable by URL was classified as orphaned")
    assert str(live_id) in protected


def test_missing_current_field_is_reported_not_deleted():
    """{'current': False} does not match a *missing* field.

    Those documents escape the URL rule on a technicality, so they are surfaced
    for a human instead of being deleted.
    """
    no_current_id = ObjectId()
    collection = FakeCollection([
        {'_id': no_current_id, 'delete': True, 'project_name': 'No Current Field'},
    ])

    assert str(no_current_id) not in collect_protected_ids(collection)
    assert collect_needs_review_ids(collection) == {str(no_current_id)}


def test_is_resolvable_by_url_matches_the_resolver_fallbacks():
    superseded_id = ObjectId()
    collection = FakeCollection([
        {'_id': superseded_id, 'delete': True, 'current': False,
         'project_name': 'Superseded'},
    ])
    assert is_resolvable_by_url(collection, str(superseded_id), 'Superseded')

    unreachable_id = ObjectId()
    empty = FakeCollection([])
    assert not is_resolvable_by_url(empty, str(unreachable_id), 'Nothing')


def test_is_resolvable_by_url_ignores_a_name_collision_with_another_document():
    """A live project sharing a name does not make this document reachable."""
    live_id = ObjectId()
    orphan_id = ObjectId()
    collection = FakeCollection([
        {'_id': live_id, 'delete': False, 'project_name': 'CCLE'},
        {'_id': orphan_id, 'delete': True, 'project_name': 'CCLE'},
    ])
    assert not is_resolvable_by_url(collection, str(orphan_id), 'CCLE')
    assert is_resolvable_by_url(collection, str(live_id), 'CCLE')


def test_gridfs_deletion_uses_the_canonical_key_list():
    """The old hand-written list missed 8 canonical keys.

    'Run metadata JSON' alone had 120,726 live values on prod; every cleanup
    left those files behind.
    """
    tar_id, png_id = ObjectId(), ObjectId()
    run_meta_id, recon_id, cycles_id = ObjectId(), ObjectId(), ObjectId()
    fs = FakeGridFS()
    project = {
        'tarfile': tar_id,
        'runs': {'sample1': [{
            'AA_PNG_file': png_id,
            'Run_metadata_JSON': run_meta_id,       # was missed
            'Reconstruction_directory': recon_id,   # was missed
            'Cycles_file': cycles_id,               # was missed
        }]},
    }

    assert delete_gridfs_files_for_project(fs.delete, project) == 5
    for missed in (run_meta_id, recon_id, cycles_id):
        assert str(missed) in fs.deleted


def test_redact_uri_removes_the_password():
    uri = "mongodb://someuser:sup3rs3cret@cluster.example.com:27017/?tls=true"
    out = redact_uri(uri)
    assert 'sup3rs3cret' not in out
    assert 'someuser' not in out
    assert 'cluster.example.com' in out


# ─────────────────────────────────────────────────────────────────────
# The shared-file guard
# ─────────────────────────────────────────────────────────────────────

class _FakeClient:
    def close(self):
        pass


class _FakeDBHandle(dict):
    """Enough of a pymongo Database for main() to run end to end.

    main() reaches for three collections by name and nothing else, so the
    handle is a dict of them.  It exists so the wiring test below can call the
    real main() rather than re-assembling its steps by hand -- a test that
    rebuilds the call it is checking passes whatever the code does.
    """


class _FakeProjects(FakeCollection):
    def find(self, query, projection=None):
        for doc in self.docs:
            if matches(doc, query):
                if projection is None:
                    yield doc
                else:
                    yield {key: doc[key] for key in projection if key in doc}

    def count_documents(self, query):
        return sum(1 for doc in self.docs if matches(doc, query))

    def delete_one(self, query):
        self.docs = [d for d in self.docs if not matches(d, query)]


def test_a_file_a_surviving_project_names_is_never_deleted():
    """The guard's whole point: shared payload outlives the orphan."""
    shared_id = ObjectId()
    orphan_only_id = ObjectId()
    survivor = {'_id': ObjectId(), 'tarfile': shared_id}
    orphan = {'_id': ObjectId(), 'tarfile': shared_id,
              'Run metadata JSON': orphan_only_id}

    retained = collect_retained_file_ids([survivor, orphan],
                                         {str(orphan['_id'])})
    assert retained == {str(shared_id)}

    fs = FakeGridFS()
    deleted = delete_gridfs_files_for_project(fs.delete, orphan, retained)

    assert deleted == 1
    assert fs.deleted == [str(orphan_only_id)]


def test_the_guard_does_not_protect_the_orphans_own_files():
    """A guard that keeps everything would be indistinguishable from a bug."""
    orphan = {'_id': ObjectId(), 'tarfile': ObjectId()}
    retained = collect_retained_file_ids([orphan], {str(orphan['_id'])})
    assert retained == set()

    fs = FakeGridFS()
    assert delete_gridfs_files_for_project(fs.delete, orphan, retained) == 1


def test_main_passes_the_retained_ids_to_the_deleter(monkeypatch, tmp_path):
    """main() must hand the guard to the deleter, not merely compute it.

    Asserted through the real entry point.  The predecessor of this test
    called the two functions itself in the order main() was believed to use,
    which proves the functions compose and says nothing about whether main()
    composes them -- the failure mode that shipped a cleanup able to delete a
    live payload despite a green suite.
    """
    import cleanup_orphaned_projects as cop

    shared_id = ObjectId()
    survivor = {'_id': ObjectId(), 'project_name': 'live', 'tarfile': shared_id,
                'delete': False, 'current': True, 'linkid': str(ObjectId())}
    # No 'delete' field at all, so no resolver query can reach it: orphaned.
    orphan = {'_id': ObjectId(), 'project_name': 'orphan', 'tarfile': shared_id}

    projects = _FakeProjects([survivor, orphan])
    handle = _FakeDBHandle({'projects': projects,
                            'fs.files': object(), 'fs.chunks': object()})

    monkeypatch.setattr(cop, 'get_db_handle', lambda *a, **k: (handle, _FakeClient()))
    monkeypatch.setattr(cop, 'delete_gridfs_file_in_batches',
                        lambda *a, **k: 0)
    monkeypatch.setenv('DB_URI_SECRET', 'mongodb://user:pw@localhost/test')
    monkeypatch.setenv('DB_NAME', 'caper-test')
    monkeypatch.delenv('S3_FILE_DOWNLOADS', raising=False)
    monkeypatch.setattr(cop.os.path, 'isdir', lambda path: False)
    monkeypatch.setattr(cop.sys, 'argv', ['cleanup_orphaned_projects.py', '--execute'])

    seen = {}
    real_deleter = cop.delete_gridfs_files_for_project

    def recording_deleter(delete_file, project, protected_file_ids=None,
                          dry_run=False):
        seen[str(project['_id'])] = protected_file_ids
        return real_deleter(delete_file, project, protected_file_ids,
                            dry_run=dry_run)

    monkeypatch.setattr(cop, 'delete_gridfs_files_for_project', recording_deleter)

    cop.main()

    assert str(orphan['_id']) in seen, 'the orphan was never offered for deletion'
    assert seen[str(orphan['_id'])] == {str(shared_id)}
