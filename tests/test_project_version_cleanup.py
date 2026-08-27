from bson import ObjectId

from caper.project_status import matches
from caper.project_version_cleanup import (
    DIRECTORY_FILE_KEYS,
    FEATURE_FILE_KEYS,
    GRIDFS_FILE_KEYS,
    PROJECT_FILE_KEYS,
    build_deleted_version_tombstone,
    delete_gridfs_payload_for_project,
    discard_unrecorded_gridfs_files,
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

    assert delete_gridfs_payload_for_project(fs.delete, project) == 1
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
        fs.delete,
        project,
        protected_file_ids={shared_id},
    ) == 1
    assert fs.deleted == [str(old_only_id)]


def test_a_file_that_will_not_delete_is_logged_and_the_rest_still_go(caplog):
    """
    A delete that fails here cannot raise -- the caller is partway through
    promoting a version, and abandoning that leaves the chain inconsistent.
    But it used to pass silently, which is how bytes stopped being countable:
    nothing references the file, so no other path will ever collect it.  The
    log line naming the id is the only way back to it.
    """
    stubborn, fine = ObjectId(), ObjectId()

    def delete_file(file_id):
        if file_id == stubborn:
            raise RuntimeError('socket timed out')

    project = {
        '_id': ObjectId(),
        'tarfile': stubborn,
        'runs': {'sample1': [{'AA_directory': fine}]},
    }

    with caplog.at_level('WARNING'):
        assert delete_gridfs_payload_for_project(delete_file, project) == 1

    assert str(stubborn) in caplog.text
    assert str(fine) not in caplog.text


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
        """Delegates to the application's own evaluator.

        This used to be a hand-written matcher with its own ideas about $in,
        $exists and previous_versions.linkid -- a third opinion on what a query
        means, in a repo whose defining bug is a predicate maintained twice.
        project_status.matches() is checked against a real MongoDB in
        tests/test_project_status.py, so this fake now agrees with the database
        by construction rather than by coincidence.
        """
        return matches(doc, query)


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
    # The view deletes through the batched helper, not the GridFS handle,
    # so that a multi-gigabyte tarfile cannot exceed the socket timeout.
    monkeypatch.setattr(views, 'delete_gridfs_file', fs.delete)

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
    # The view deletes through the batched helper, not the GridFS handle,
    # so that a multi-gigabyte tarfile cannot exceed the socket timeout.
    monkeypatch.setattr(views, 'delete_gridfs_file', fs.delete)
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


def test_every_uploaded_key_is_also_a_deletable_key():
    """Every key ingestion writes a GridFS id to must be one the cleanup paths
    recognise, in both the spaced and underscored spelling.

    A key that is uploaded but not listed for deletion produces a file the site
    can never reclaim.  Six such keys accumulated before this was caught
    (Reconstruction directory, Cycles file, Graph file, Graph PNG/PDF file,
    Run metadata JSON), which is a large part of why the GridFS orphan
    population exists at all.
    """
    for key in FEATURE_FILE_KEYS + DIRECTORY_FILE_KEYS + PROJECT_FILE_KEYS:
        assert key in GRIDFS_FILE_KEYS, f"{key!r} is uploaded but never deleted"
        assert key.replace(' ', '_') in GRIDFS_FILE_KEYS, (
            f"{key!r} is not covered in its underscored spelling"
        )


def test_ingestion_loop_uses_the_shared_key_list():
    """views.py must iterate the shared constant rather than a private copy.

    The original defect was a hand-maintained list in views.py that drifted
    from the one the deletion code used.  Importing the same object is what
    makes drift impossible, so assert the identity rather than the contents.
    """
    assert views.FEATURE_FILE_KEYS is FEATURE_FILE_KEYS


def test_gridfs_ids_are_found_under_the_coral_era_keys():
    """Regression: the CoRAL/aggregator-7.0 key names must be walked."""
    file_id = ObjectId()
    project = {
        'runs': {
            'sample_1': [{
                'Graph_file': str(file_id),
                'Reconstruction_directory': str(ObjectId()),
                'Cycles_file': str(ObjectId()),
                'Run_metadata_JSON': str(ObjectId()),
            }],
        },
    }
    found = list(iter_gridfs_file_ids(project))
    assert file_id in found
    assert len(found) == 4


def test_discard_unrecorded_gridfs_files_deletes_only_object_ids():
    """Ingestion accumulates raw ids; sentinels like "Not Provided" must not
    be passed to fs.delete()."""
    fs = FakeGridFS()
    good_a, good_b = ObjectId(), ObjectId()

    deleted = discard_unrecorded_gridfs_files(
        fs.delete, [good_a, 'Not Provided', None, good_b, str(ObjectId())]
    )

    assert deleted == 2
    assert fs.deleted == [str(good_a), str(good_b)]


def test_discard_unrecorded_gridfs_files_never_masks_the_original_error():
    """A failure to clean up must not raise: the caller is already handling a
    more important exception, and losing it would hide the real cause."""
    class ExplodingGridFS:
        def __init__(self):
            self.attempts = 0

        def delete(self, file_id):
            self.attempts += 1
            raise RuntimeError('gridfs is unreachable')

    fs = ExplodingGridFS()
    assert discard_unrecorded_gridfs_files(fs.delete, [ObjectId(), ObjectId()]) == 0
    assert fs.attempts == 2


def test_discard_unrecorded_gridfs_files_tolerates_empty_input():
    fs = FakeGridFS()
    assert discard_unrecorded_gridfs_files(fs.delete, []) == 0
    assert discard_unrecorded_gridfs_files(fs.delete, None) == 0
    assert fs.deleted == []


def test_ingestion_discards_files_it_cannot_record():
    """The orphan factory, guarded.

    extract_project_files() writes artifacts to GridFS before the document that
    names them is saved, and its outer handler swallows exceptions.  Assert the
    source actually resets the tracking list only after the document is written,
    and discards on the failure path -- the ordering is the whole correctness
    argument and is easy to silently break.
    """
    import inspect

    source = inspect.getsource(views.extract_project_files)

    assert 'uploaded_file_ids = []' in source
    assert 'discard_unrecorded_gridfs_files' in source

    # The reset must come AFTER the update that records the ids, otherwise a
    # later failure would delete files the project legitimately references.
    record_at = source.index('collection_handle.update_one(query, new_val)')
    reset_at = source.index('uploaded_file_ids = []', record_at)
    discard_at = source.index('discard_unrecorded_gridfs_files(delete_gridfs_file, stranded)')
    assert record_at < reset_at < discard_at
