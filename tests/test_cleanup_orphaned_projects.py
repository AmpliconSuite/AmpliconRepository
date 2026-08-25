from bson import ObjectId

from cleanup_orphaned_projects import (
    collect_needs_review_ids,
    collect_protected_ids,
    delete_gridfs_files_for_project,
    is_resolvable_by_url,
    redact_uri,
)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection):
        for doc in self.docs:
            if all(self._matches(doc, key, value) for key, value in query.items()):
                yield {
                    key: doc[key]
                    for key in projection
                    if key in doc
                }

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if all(self._matches(doc, key, value) for key, value in query.items()):
                return doc
        return None

    @staticmethod
    def _matches(doc, key, value):
        if isinstance(value, dict) and '$exists' in value:
            return (key in doc) == value['$exists']
        return doc.get(key) == value


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

    assert delete_gridfs_files_for_project(fs, project) == 3
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

    assert delete_gridfs_files_for_project(fs, project) == 5
    for missed in (run_meta_id, recon_id, cycles_id):
        assert str(missed) in fs.deleted


def test_redact_uri_removes_the_password():
    uri = "mongodb://someuser:sup3rs3cret@cluster.example.com:27017/?tls=true"
    out = redact_uri(uri)
    assert 'sup3rs3cret' not in out
    assert 'someuser' not in out
    assert 'cluster.example.com' in out
