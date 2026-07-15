from bson import ObjectId

from caper.project_version_cleanup import (
    build_deleted_version_tombstone,
    delete_gridfs_payload_for_project,
    iter_gridfs_file_ids,
    retarget_deleted_version_tombstones,
)
from caper import utils
from caper import views


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


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def __iter__(self):
        return iter(self.docs)

    def __getitem__(self, index):
        return self.docs[index]

    def sort(self, key, direction):
        reverse = direction == -1
        self.docs.sort(key=lambda doc: doc.get(key, ''), reverse=reverse)
        return self

    def close(self):
        pass


class FakeHistoryCollection:
    def __init__(self, docs):
        self.docs = {str(doc['_id']): doc.copy() for doc in docs}

    def find(self, query, projection=None):
        return FakeCursor([
            self._project(doc, projection)
            for doc in self.docs.values()
            if self._matches_query(doc, query)
        ])

    def find_one(self, query, projection=None):
        matches = list(self.find(query, projection))
        return matches[0] if matches else None

    def update_one(self, query, update):
        doc = self._find_mutable(query)
        if doc is not None:
            for key, value in update.get('$set', {}).items():
                doc[key] = value

    def update_many(self, query, update):
        modified_count = 0
        for doc in self.docs.values():
            if self._matches_query(doc, query):
                for key, value in update.get('$set', {}).items():
                    doc[key] = value
                modified_count += 1

        class Result:
            pass

        result = Result()
        result.modified_count = modified_count
        return result

    def replace_one(self, query, replacement, upsert=False):
        doc_id = str(query['_id'])
        if doc_id in self.docs or upsert:
            self.docs[doc_id] = replacement.copy()

    def _find_mutable(self, query):
        for doc in self.docs.values():
            if self._matches_query(doc, query):
                return doc
        return None

    @staticmethod
    def _project(doc, projection):
        if not projection:
            return doc.copy()
        projected = {}
        for key, include in projection.items():
            if include and key in doc:
                projected[key] = doc[key]
        if '_id' not in projected and projection.get('_id', 1):
            projected['_id'] = doc['_id']
        return projected

    @classmethod
    def _matches_query(cls, doc, query):
        for key, expected in query.items():
            if isinstance(expected, dict) and '$in' in expected:
                if doc.get(key) not in expected['$in']:
                    return False
                continue
            if isinstance(expected, dict) and '$exists' in expected:
                if (key in doc) != expected['$exists']:
                    return False
                continue
            if key == 'previous_versions.linkid':
                values = [
                    str(pv.get('linkid'))
                    for pv in doc.get('previous_versions', [])
                    if isinstance(pv, dict) and pv.get('linkid')
                ]
                if str(expected) not in values:
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


def test_delete_old_project_version_creates_redirect_tombstone_without_promotable_history(
        monkeypatch, request_factory, test_user):
    latest_id = ObjectId()
    old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'project_name': 'history-cleanup',
        'date': '2026-07-01T00:00:00.000000',
        'delete': False,
        'current': True,
        'private': 'private',
        'project_members': [test_user.username],
        'previous_versions': [{
            'date': '2026-06-01T00:00:00.000000',
            'linkid': str(old_id),
            'AA_version': 'AA-old',
            'AC_version': 'AC-old',
            'ASP_version': 'ASP-old',
            'aggregator_version': 'AGG-old',
        }],
        'tarfile': ObjectId(),
    }
    old = {
        '_id': old_id,
        'project_name': 'history-cleanup',
        'date': '2026-06-01T00:00:00.000000',
        'delete': True,
        'current': False,
        'private': 'private',
        'project_members': [test_user.username],
        'AA_version': 'AA-old',
        'AC_version': 'AC-old',
        'ASP_version': 'ASP-old',
        'aggregator_version': 'AGG-old',
        'tarfile': ObjectId(),
    }
    collection = FakeHistoryCollection([latest, old])
    fs = FakeGridFS()
    monkeypatch.setattr(utils, 'collection_handle', collection)
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'fs_handle', fs)

    request = request_factory.post(f'/project/{latest_id}/delete_version/{old_id}')
    request.user = test_user

    response = views.delete_project_version(request, str(latest_id), str(old_id))

    assert response.status_code == 200
    assert collection.docs[str(latest_id)]['previous_versions'] == []
    tombstone = collection.docs[str(old_id)]
    assert tombstone['version_deleted_from_history'] is True
    assert tombstone['payload_purged'] is True
    assert tombstone['redirect_to_project'] == str(latest_id)
    assert tombstone['AA_version'] == 'AA-old'


def test_delete_current_version_retargets_existing_tombstones_to_promoted_version(
        monkeypatch, request_factory, test_user):
    latest_id = ObjectId()
    promoted_id = ObjectId()
    deleted_old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'project_name': 'history-cleanup',
        'date': '2026-07-01T00:00:00.000000',
        'delete': False,
        'current': True,
        'private': 'private',
        'project_members': [test_user.username],
        'previous_versions': [{
            'date': '2026-06-01T00:00:00.000000',
            'linkid': str(promoted_id),
            'AA_version': 'AA-promoted',
            'AC_version': 'AC-promoted',
            'ASP_version': 'ASP-promoted',
            'aggregator_version': 'AGG-promoted',
        }],
        'tarfile': ObjectId(),
    }
    promoted = {
        '_id': promoted_id,
        'project_name': 'history-cleanup',
        'date': '2026-06-01T00:00:00.000000',
        'delete': True,
        'current': False,
        'private': 'private',
        'project_members': [test_user.username],
        'previous_versions': [],
        'tarfile': ObjectId(),
    }
    older_tombstone = {
        '_id': deleted_old_id,
        'date': '2026-05-01T00:00:00.000000',
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(latest_id),
    }
    collection = FakeHistoryCollection([latest, promoted, older_tombstone])
    fs = FakeGridFS()
    monkeypatch.setattr(utils, 'collection_handle', collection)
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'fs_handle', fs)
    monkeypatch.setattr(views, 'delete_project_from_site_statistics', lambda *args, **kwargs: None)

    request = request_factory.post(f'/project/{latest_id}/delete_version/{latest_id}')
    request.user = test_user

    response = views.delete_project_version(request, str(latest_id), str(latest_id))

    assert response.status_code == 200
    assert collection.docs[str(promoted_id)]['current'] is True
    assert collection.docs[str(promoted_id)]['delete'] is False
    assert collection.docs[str(latest_id)]['version_deleted_from_history'] is True
    assert collection.docs[str(latest_id)]['redirect_to_project'] == str(promoted_id)
    assert collection.docs[str(deleted_old_id)]['redirect_to_project'] == str(promoted_id)


def test_previous_versions_includes_deleted_redirect_tombstones(monkeypatch):
    latest_id = ObjectId()
    active_old_id = ObjectId()
    deleted_old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'linkid': latest_id,
        'project_name': 'history-display',
        'date': '2026-07-01T00:00:00.000000',
        'delete': False,
        'current': True,
        'previous_versions': [{
            'date': '2026-05-01T00:00:00.000000',
            'linkid': str(active_old_id),
            'AA_version': 'AA-active',
            'AC_version': 'AC-active',
            'ASP_version': 'ASP-active',
            'aggregator_version': 'AGG-active',
        }],
        'AA_version': 'AA-latest',
        'AC_version': 'AC-latest',
        'ASP_version': 'ASP-latest',
        'aggregator_version': 'AGG-latest',
    }
    deleted_tombstone = {
        '_id': deleted_old_id,
        'project_name': 'history-display',
        'date': '2026-06-01T00:00:00.000000',
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(latest_id),
        'delete_date': '2026-07-02T00:00:00.000000',
        'AA_version': 'AA-deleted',
        'AC_version': 'AC-deleted',
        'ASP_version': 'ASP-deleted',
        'aggregator_version': 'AGG-deleted',
    }
    collection = FakeHistoryCollection([latest, deleted_tombstone])
    monkeypatch.setattr(utils, 'collection_handle', collection)

    history, msg = utils.previous_versions(latest)

    assert msg is None
    assert [entry['linkid'] for entry in history] == [
        str(latest_id),
        str(deleted_old_id),
        str(active_old_id),
    ]
    deleted_entry = history[1]
    assert deleted_entry['version_deleted_from_history'] is True
    assert deleted_entry['payload_purged'] is True
    assert deleted_entry['redirect_to_project'] == str(latest_id)
    assert deleted_entry['AA_version'] == 'AA-deleted'
    assert len(latest['previous_versions']) == 1


def test_previous_versions_includes_tombstones_redirecting_to_prior_current(monkeypatch):
    latest_id = ObjectId()
    prior_current_id = ObjectId()
    deleted_old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'linkid': latest_id,
        'project_name': 'history-display',
        'date': '2026-07-01T00:00:00.000000',
        'delete': False,
        'current': True,
        'previous_versions': [{
            'date': '2026-06-01T00:00:00.000000',
            'linkid': str(prior_current_id),
            'AA_version': 'AA-prior',
            'AC_version': 'AC-prior',
            'ASP_version': 'ASP-prior',
            'aggregator_version': 'AGG-prior',
        }],
        'AA_version': 'AA-latest',
        'AC_version': 'AC-latest',
        'ASP_version': 'ASP-latest',
        'aggregator_version': 'AGG-latest',
    }
    stale_tombstone = {
        '_id': deleted_old_id,
        'date': '2026-05-01T00:00:00.000000',
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(prior_current_id),
        'AA_version': 'AA-deleted',
        'AC_version': 'AC-deleted',
        'ASP_version': 'ASP-deleted',
        'aggregator_version': 'AGG-deleted',
    }
    collection = FakeHistoryCollection([latest, stale_tombstone])
    monkeypatch.setattr(utils, 'collection_handle', collection)

    history, msg = utils.previous_versions(latest)

    assert msg is None
    assert str(deleted_old_id) in [entry['linkid'] for entry in history]
    deleted_entry = next(entry for entry in history if entry['linkid'] == str(deleted_old_id))
    assert deleted_entry['version_deleted_from_history'] is True
    assert deleted_entry['redirect_to_project'] == str(prior_current_id)


def test_get_one_project_resolves_stale_tombstone_redirect_to_latest(monkeypatch):
    latest_id = ObjectId()
    prior_current_id = ObjectId()
    deleted_old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'delete': False,
        'current': True,
        'runs': {},
        'project_name': 'latest',
        'previous_versions': [{'linkid': str(prior_current_id)}],
    }
    prior_current = {
        '_id': prior_current_id,
        'delete': True,
        'current': False,
        'runs': {},
        'project_name': 'prior',
    }
    stale_tombstone = {
        '_id': deleted_old_id,
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(prior_current_id),
        'project_name': 'deleted',
    }
    collection = FakeHistoryCollection([latest, prior_current, stale_tombstone])
    monkeypatch.setattr(utils, 'collection_handle', collection)

    project = utils.get_one_project(str(deleted_old_id))

    assert project['_id'] == latest_id
    assert project['linkid'] == latest_id


def test_project_page_warns_when_deleted_version_url_redirects(
        monkeypatch, request_factory, test_user):
    latest_id = ObjectId()
    deleted_old_id = ObjectId()
    latest = {
        '_id': latest_id,
        'linkid': latest_id,
        'delete': False,
        'current': True,
        'runs': {},
        'project_name': 'latest',
        'private': 'public',
        'project_members': [test_user.username],
    }
    tombstone = {
        '_id': deleted_old_id,
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(latest_id),
        'project_name': 'deleted',
    }
    collection = FakeHistoryCollection([latest, tombstone])
    captured_messages = []
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'get_one_project', lambda project_name: latest)
    monkeypatch.setattr(views, 'validate_project', lambda project, project_name: project)
    monkeypatch.setattr(
        views.messages,
        'warning',
        lambda request, message: captured_messages.append(message),
    )

    request = request_factory.get(f'/project/{deleted_old_id}')
    request.user = test_user

    response = views.project_page(request, str(deleted_old_id))

    assert response.status_code == 302
    assert response['Location'] == f'/project/{latest_id}'
    assert captured_messages == [
        "The project version you selected was deleted, so you were redirected "
        "to the latest version of the project."
    ]


def test_retarget_deleted_version_tombstones_points_to_new_latest():
    old_latest_id = ObjectId()
    new_latest_id = ObjectId()
    tombstone_id = ObjectId()
    untouched_tombstone_id = ObjectId()
    collection = FakeHistoryCollection([
        {
            '_id': tombstone_id,
            'version_deleted_from_history': True,
            'payload_purged': True,
            'redirect_to_project': str(old_latest_id),
        },
        {
            '_id': untouched_tombstone_id,
            'version_deleted_from_history': True,
            'payload_purged': True,
            'redirect_to_project': str(ObjectId()),
        },
    ])

    modified = retarget_deleted_version_tombstones(
        collection,
        old_latest_id,
        new_latest_id,
    )

    assert modified == 1
    assert collection.docs[str(tombstone_id)]['redirect_to_project'] == str(new_latest_id)
    assert collection.docs[str(untouched_tombstone_id)]['redirect_to_project'] != str(new_latest_id)
