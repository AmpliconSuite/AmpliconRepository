from bson import ObjectId

from caper.project_version_cleanup import (
    build_deleted_version_tombstone,
    delete_gridfs_payload_for_project,
    iter_gridfs_file_ids,
)
from caper import utils


class FakeGridFS:
    def __init__(self):
        self.deleted = []

    def delete(self, file_id):
        self.deleted.append(str(file_id))


def test_iter_gridfs_file_ids_finds_historical_and_current_keys():
    tar_id = ObjectId()
    png_id = ObjectId()
    graph_id = ObjectId()

    project = {
        'tarfile': tar_id,
        'runs': {
            'sample1': [{
                'AA PNG file': str(png_id),
                'AA_graph_file': graph_id,
                'not_a_gridfs_field': ObjectId(),
            }],
        },
    }

    assert set(iter_gridfs_file_ids(project)) == {tar_id, png_id, graph_id}


def test_delete_gridfs_payload_for_project_deduplicates_files():
    tar_id = ObjectId()
    fs = FakeGridFS()
    project = {
        'tarfile': tar_id,
        'runs': {'sample1': [{'AA_directory': tar_id}]},
    }

    assert delete_gridfs_payload_for_project(fs, project) == 1
    assert fs.deleted == [str(tar_id)]


def test_delete_gridfs_payload_for_project_skips_protected_shared_files():
    shared_id = ObjectId()
    old_only_id = ObjectId()
    fs = FakeGridFS()
    project = {
        'tarfile': shared_id,
        'runs': {'sample1': [{'AA_directory': old_only_id}]},
    }

    assert delete_gridfs_payload_for_project(
        fs,
        project,
        protected_file_ids={shared_id},
    ) == 1
    assert fs.deleted == [str(old_only_id)]


def test_build_deleted_version_tombstone_preserves_uuid_and_redirects_to_latest():
    old_id = ObjectId()
    latest_id = ObjectId()
    tombstone = build_deleted_version_tombstone(
        {
            '_id': old_id,
            'project_name': 'old name',
            'date': '2024-01-01T00:00:00',
            'private': 'private',
            'project_members': ['old@example.org'],
        },
        {
            '_id': latest_id,
            'project_name': 'new name',
            'private': 'public',
            'project_members': ['new@example.org'],
        },
        'deleter',
        '2026-07-08T00:00:00',
    )

    assert tombstone['_id'] == old_id
    assert tombstone['redirect_to_project'] == str(latest_id)
    assert tombstone['delete'] is True
    assert tombstone['current'] is False
    assert tombstone['payload_purged'] is True
    assert tombstone['project_members'] == ['new@example.org']


class FakeProjectCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        return [doc.copy() for doc in self.docs if self._matches(doc, query)]

    def find_one(self, query, projection=None):
        matches = self.find(query, projection)
        return matches[0] if matches else None

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            value = doc.get(key)
            if value != expected:
                return False
        return True


def test_get_one_project_resolves_deleted_version_tombstone(monkeypatch):
    old_id = ObjectId()
    latest_id = ObjectId()
    latest = {
        '_id': latest_id,
        'delete': False,
        'current': True,
        'runs': {},
        'project_name': 'latest',
    }
    tombstone = {
        '_id': old_id,
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(latest_id),
        'project_name': 'old',
    }
    monkeypatch.setattr(utils, 'collection_handle', FakeProjectCollection([tombstone, latest]))

    project = utils.get_one_project(str(old_id))

    assert project['_id'] == latest_id
    assert project['linkid'] == latest_id
