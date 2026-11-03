"""
/add_samples_to_project_api/ makes its new version the way the edit page does.

It used to run its own copy of the edit flow, which handed the aggregator a
random uuid4 hex as the project name.  The aggregator prefixes the archive's
consolidated_classification files with that name, so an archive made through
this endpoint carried an id that matched no project on the site, while one made
through the edit page carries the id of the version it belongs to.  It also
supersedes the old version before creating the new one with no rollback, and
recorded a shorter previous_versions entry than the edit page.

The endpoint now fixes the new version's id first, inserts the same placeholder
the edit page does, and hands both to _process_edit_and_notify.

The fast tests pin that hand-off without running the aggregator.  The slow one
runs a real add and reads the archive the new version stored.
"""

import os
import re
import tarfile
import uuid
from unittest.mock import MagicMock, patch

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_ADDL_TAR,
    DATASET_MEDIUM_TAR,
    POLL_TIMEOUT,
)

pytestmark = pytest.mark.integration


def _add_samples_request(project_id, project_key, username, tar_path):
    from rest_framework.test import APIRequestFactory

    with open(tar_path, 'rb') as fh:
        return APIRequestFactory().post(
            '/add_samples_to_project_api/',
            data={'project_uuid': project_id, 'project_key': project_key,
                  'username': username, 'file': fh},
            format='multipart')


@pytest.fixture
def api_user():
    """A real Django user: the endpoint looks the uploader up with the ORM,
    which the session's mock test_user cannot satisfy."""
    from django.contrib.auth.models import User

    username = 'pytest_api_add_samples'
    User.objects.filter(username=username).delete()
    user = User.objects.create_user(username=username,
                                    email='pytest_api_add_samples@example.com')
    try:
        yield user
    finally:
        user.delete()


@pytest.fixture
def keyed_project(mongo_collection, api_user):
    """A finished project the test user may add samples to, cleaned up afterwards."""
    doc = {
        'project_name': f'ApiAddSamplesTest_{uuid.uuid4().hex[:8]}',
        'description': 'API add-samples versioning test',
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {},
        'project_members': [api_user.username, api_user.email],
        'privateKey': uuid.uuid4().hex,
        'views': 3,
        'downloads': 2,
        'date': '2024-01-01',
        'sample_count': 0,
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})
    try:
        yield mongo_collection.find_one({'_id': result.inserted_id})
    finally:
        _cleanup_project(mongo_collection, project_id)


def _submitted_add(project, username, mongo_collection):
    """Call the endpoint with the background thread stubbed out.

    Returns (response, captured submit args or None, placeholder document or
    None).  The placeholder is removed afterwards.
    """
    from caper.views_apis import ProjectFileAddView

    captured = {}

    def _fake_submit(fn, *args, **kwargs):
        captured['args'] = args
        return MagicMock()

    request = _add_samples_request(str(project['_id']), project['privateKey'],
                                   username, DATASET_ADDL_TAR)
    placeholder = None
    try:
        with patch('caper.background_tasks._thread_executor') as mock_executor:
            mock_executor.submit.side_effect = _fake_submit
            response = ProjectFileAddView.as_view()(request)
        args = captured.get('args')
        if args:
            placeholder = mongo_collection.find_one({'_id': ObjectId(args[1])})
        return response, args, placeholder
    finally:
        if captured.get('args'):
            _cleanup_project(mongo_collection, captured['args'][1])


def test_the_new_version_id_is_fixed_before_aggregation(
        api_user, mongo_collection, keyed_project):
    response, args, placeholder = _submitted_add(
        keyed_project, api_user.username, mongo_collection)

    assert response.status_code == 200, response.data
    assert args is not None, 'No aggregation was started'
    # args: (file_fps, temp_proj_id, project_data_path, form_data, user,
    #        previous_versions, project)
    temp_proj_id = args[1]
    assert re.fullmatch(r'[0-9a-f]{24}', temp_proj_id), \
        f'The new version id is not an ObjectId: {temp_proj_id!r}'
    assert placeholder is not None, \
        'No placeholder document holds the id the aggregator will be given'
    assert placeholder['linkid'] == temp_proj_id
    assert placeholder.get('aggregation_in_progress') is True
    assert args[2] == f'tmp/{temp_proj_id}'
    assert all(fp.startswith(f'tmp/{temp_proj_id}/') for fp in args[0]), args[0]


def test_the_history_entry_matches_the_edit_page(
        api_user, mongo_collection, keyed_project):
    from caper.views import previous_version_entry

    _, args, _ = _submitted_add(keyed_project, api_user.username, mongo_collection)

    history = args[5]
    assert history[-1] == previous_version_entry(keyed_project), \
        'The version being superseded is recorded differently from an edit-page edit'
    assert 'Reconstruction_tools' in history[-1] and 'CoRAL_version' in history[-1]


def test_a_rejected_request_leaves_the_project_alone(
        api_user, mongo_collection, keyed_project):
    """The key check runs before anything is retired or inserted."""
    from caper.views_apis import ProjectFileAddView

    before = mongo_collection.count_documents({})
    request = _add_samples_request(str(keyed_project['_id']), 'WRONG',
                                   api_user.username, DATASET_ADDL_TAR)
    response = ProjectFileAddView.as_view()(request)

    assert response.status_code == 403
    assert mongo_collection.count_documents({}) == before
    old = mongo_collection.find_one({'_id': keyed_project['_id']})
    assert old['current'] is True and old['delete'] is False


# ---------------------------------------------------------------------------
# End to end, against real archives
# ---------------------------------------------------------------------------

def _consolidated_prefixes(fs_handle, tarfile_id):
    """The project-name prefixes on the archive's consolidated_classification files."""
    pattern = re.compile(r'consolidated_classification/([^/]+?)_'
                         r'(?:classification_bed_files|annotated_cycles_files|SV_summaries)')
    prefixes = set()
    with tarfile.open(fileobj=fs_handle.get(ObjectId(str(tarfile_id))), mode='r|gz') as t:
        for member in t:
            match = pattern.search(member.name)
            if match:
                prefixes.add(match.group(1))
    return prefixes


@pytest.mark.slow
def test_adding_samples_end_to_end(
        request_factory, api_user, mongo_collection, monkeypatch):
    """Create a project, add samples through the API, and read the new archive.

    Needs the local server on :8000: the old archive is fetched from it, as on
    the edit page.
    """
    from django.conf import settings
    from caper.utils import fs_handle
    from caper.views import create_project
    from caper.views_apis import ProjectFileAddView

    for path in (DATASET_MEDIUM_TAR, DATASET_ADDL_TAR):
        if not os.path.exists(path):
            pytest.skip(f'Test dataset not found: {path}')

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    sent = []

    class _CapturedEmail:
        def __init__(self, subject, body, *args, **kwargs):
            self.body = body

        def send(self, **kwargs):
            sent.append(self.body)

    monkeypatch.setattr('caper.views_apis.EmailMessage', _CapturedEmail)
    created_ids = []

    request, handles = _build_create_request(
        request_factory, api_user, 'PyTest_ApiAddSamples', tar_path=DATASET_MEDIUM_TAR)
    try:
        response = create_project(request)
    finally:
        for handle in handles:
            handle.close()

    project_id = _project_id_from_redirect(response)
    assert project_id, f'[Create] No project id in redirect: {response!r}'
    created_ids.append(project_id)

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, f'[Create] Timed out after {POLL_TIMEOUT}s'
        assert not doc.get('aggregation_failed'), doc.get('error_message')
        assert _consolidated_prefixes(fs_handle, doc['tarfile']) <= {project_id}

        # A project has no key until a member asks for one on its page.
        project_key = uuid.uuid4().hex
        mongo_collection.update_one({'_id': ObjectId(project_id)},
                                    {'$set': {'privateKey': project_key}})
        add_request = _add_samples_request(project_id, project_key,
                                           api_user.username, DATASET_ADDL_TAR)
        add_response = ProjectFileAddView.as_view()(add_request)
        assert add_response.status_code == 200, add_response.data

        # The placeholder is the uploader's only other project; it is given its
        # history when aggregation finishes, so it cannot be found by that yet.
        placeholder = mongo_collection.find_one(
            {'owner': api_user.username, '_id': {'$ne': ObjectId(project_id)}})
        assert placeholder is not None, '[Add] No placeholder was inserted for the new version'
        new_id = str(placeholder['_id'])
        created_ids.append(new_id)

        new_doc = _poll_until_finished(mongo_collection, new_id)
        assert new_doc is not None, f'[Add] Timed out after {POLL_TIMEOUT}s'
        assert not new_doc.get('aggregation_failed'), new_doc.get('error_message')
        assert project_id in [entry['linkid'] for entry in new_doc['previous_versions']], \
            '[Add] The old version is not in the new version history'
        assert new_doc['sample_count'] > doc['sample_count'], \
            f"[Add] {new_doc['sample_count']} samples after adding to {doc['sample_count']}"

        prefixes = _consolidated_prefixes(fs_handle, new_doc['tarfile'])
        assert prefixes == {new_id}, \
            f'[Add] The new archive is labelled {prefixes}, not with its own id {new_id}'

        old_doc = mongo_collection.find_one({'_id': ObjectId(project_id)})
        assert old_doc is not None and old_doc['current'] is False, \
            '[Add] The old version is not superseded'
        assert any(new_id in body for body in sent), \
            f'[Add] The uploader was not told the new version id: {sent}'
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)
