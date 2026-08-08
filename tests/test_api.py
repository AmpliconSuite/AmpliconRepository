"""
Integration tests for REST API endpoints.

FileUploadView (/upload_api/) and ProjectFileAddView (/add_samples_to_project_api/)
are tested via DRF's APIRequestFactory so that multipart uploads work correctly.

ProjectFileAddView requires a real Django User object in the database
(it calls User.objects.get(...)).  Tests that need this are skipped when no
suitable database user exists, to avoid hard failures in environments that
only use mock users.

BackgroundTaskStatusView (/api/background-task-status/) is a simple GET
endpoint with no auth requirements and is always tested.
"""

import os
import pytest

from conftest import (
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_MEDIUM_TAR,
    DATASET_ADDL_TAR,
)


# ---------------------------------------------------------------------------
# Background task status (no auth, no file upload)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_background_task_status_returns_200():
    """
    GET /api/background-task-status/ must return 200 and a JSON body
    containing the 'is_busy' key.
    """
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import BackgroundTaskStatusView

    rf  = APIRequestFactory()
    req = rf.get('/api/background-task-status/')
    resp = BackgroundTaskStatusView.as_view()(req)

    assert resp.status_code == 200, \
        f"Expected 200 from BackgroundTaskStatusView, got {resp.status_code}"
    assert 'is_busy' in resp.data, \
        f"Response JSON must contain 'is_busy'; got keys: {list(resp.data.keys())}"
    assert 'active_count' in resp.data, \
        f"Response JSON must contain 'active_count'; got keys: {list(resp.data.keys())}"


# ---------------------------------------------------------------------------
# File upload API
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_file_upload_get_returns_200():
    """GET /upload_api/ must return 200 with a success response body."""
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import FileUploadView

    rf = APIRequestFactory()
    req = rf.get('/upload_api/')
    resp = FileUploadView.as_view()(req)

    assert resp.status_code == 200, \
        f"Expected 200 from FileUploadView GET, got {resp.status_code}"
    assert resp.data.get('response') == 'success', \
        f"Expected {{'response': 'success'}}, got {resp.data}"

@pytest.mark.slow
@pytest.mark.integration
def test_upload_api_accepts_tar_file(mongo_collection):
    """
    POST /upload_api/ with a valid tar.gz and required form fields must
    return 201.  The project document is cleaned up after the test.

    The FileUploadView starts an async thread for aggregation; this test
    only verifies that the upload itself is accepted (201), not that
    aggregation succeeds.
    """
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import FileUploadView

    assert os.path.exists(DATASET_SMALL_TAR), \
        f"Test dataset not found: {DATASET_SMALL_TAR}"

    rf = APIRequestFactory()

    with open(DATASET_SMALL_TAR, 'rb') as fh:
        resp = rf.post(
            '/upload_api/',
            data={
                'project_name':    'APITest_Upload',
                'description':     'Automated pytest API upload test',
                'private':         'private',
                'publication_link': '',
                'project_members':  'pytest_test_user',
                'alias':           '',
                'remap_sample_names': 'false',
                'accept_license':  'on',
                'file':             fh,
            },
            format='multipart')

    response = FileUploadView.as_view()(resp)

    assert response.status_code == 201, \
        f"Expected 201 from FileUploadView, got {response.status_code}: {getattr(response, 'data', '')}"

    # If a project document was created, clean it up
    if response.status_code in (200, 201):
        new_doc = mongo_collection.find_one(
            {'project_name': 'APITest_Upload', 'delete': False})
        if new_doc:
            _cleanup_project(mongo_collection, str(new_doc['_id']))


# ---------------------------------------------------------------------------
# Add samples to project API
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_add_samples_requires_valid_project_key(mongo_collection):
    """
    POST /add_samples_to_project_api/ with an invalid project_key must
    return 403 Forbidden.

    Uses a real existing project because the endpoint requires the project to
    exist. The endpoint validates the key before touching files.
    """
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import ProjectFileAddView

    assert os.path.exists(DATASET_ADDL_TAR), \
        f"Test dataset not found: {DATASET_ADDL_TAR}"

    # Use an existing non-deleted project to exercise the authorization check
    # against realistic MongoDB state without invoking aggregation.
    existing = mongo_collection.find_one({'delete': False, 'current': True})
    if not existing:
        pytest.skip("No existing projects in database — cannot test add_samples auth")

    project_uuid = str(existing['_id'])

    rf = APIRequestFactory()
    with open(DATASET_ADDL_TAR, 'rb') as fh:
        req = rf.post(
            '/add_samples_to_project_api/',
            data={
                'project_uuid': project_uuid,
                'project_key':  'THIS_IS_A_WRONG_KEY',
                'username':     'pytest_test_user',
                'file':         fh,
            },
            format='multipart')

    response = ProjectFileAddView.as_view()(req)

    # Wrong key must be rejected — 403 or 404 (project not found as member)
    assert response.status_code in (403, 404), \
        f"Invalid project key should be rejected (403/404), got {response.status_code}"


@pytest.mark.integration
def test_add_samples_requires_project_member(mongo_collection):
    """
    POST /add_samples_to_project_api/ by a user who is not a project member
    must return 403 Forbidden, even with the correct project key.
    """
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import ProjectFileAddView

    assert os.path.exists(DATASET_ADDL_TAR), \
        f"Test dataset not found: {DATASET_ADDL_TAR}"

    existing = mongo_collection.find_one({'delete': False, 'current': True})
    if not existing:
        pytest.skip("No existing projects in database — cannot test add_samples auth")

    project_uuid = str(existing['_id'])
    real_key = existing.get('privateKey', 'no-key')

    rf = APIRequestFactory()
    with open(DATASET_ADDL_TAR, 'rb') as fh:
        req = rf.post(
            '/add_samples_to_project_api/',
            data={
                'project_uuid': project_uuid,
                'project_key':  real_key,
                'username':     'pytest_nonexistent_user_xyz',
                'file':         fh,
            },
            format='multipart')

    response = ProjectFileAddView.as_view()(req)

    # Non-member must be rejected
    assert response.status_code in (403, 404), \
        f"Non-member should be rejected (403/404), got {response.status_code}"


# ---------------------------------------------------------------------------
# project_members parsing on /upload_api/
# ---------------------------------------------------------------------------

def _members_from_post(data):
    """Build a POST request and run it through parse_project_members."""
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import parse_project_members

    req = APIRequestFactory().post('/upload_api/', data=data, format='multipart')
    return parse_project_members(req)


@pytest.mark.integration
def test_parse_project_members_single_user():
    """
    A single member must come back whole, not split into characters.

    Regression: the old code did request.POST['project_members'][0], which
    took the first *character* of the string, so the project creator ended
    up as 'p' instead of 'pytest_test_user'.
    """
    members = _members_from_post({'project_members': 'pytest_test_user'})
    assert members == ['pytest_test_user'], \
        f"Expected the whole username, got {members}"


@pytest.mark.integration
def test_parse_project_members_multiple_and_order():
    """Separators are commas/semicolons/whitespace; the owner stays first."""
    members = _members_from_post(
        {'project_members': 'owner@example.com, second@example.com;third@example.com  fourth'})
    assert members == ['owner@example.com', 'second@example.com',
                       'third@example.com', 'fourth'], \
        f"Members parsed in the wrong order or wrongly split: {members}"


@pytest.mark.integration
def test_parse_project_members_empty():
    """Blank or absent project_members yields no members."""
    assert _members_from_post({'project_members': '  '}) == []
    assert _members_from_post({'project_name': 'x'}) == []


@pytest.mark.integration
def test_upload_api_without_project_members_returns_400():
    """
    POST /upload_api/ with no project_members must be rejected with 400
    rather than creating an ownerless project (or raising a 500).
    """
    from rest_framework.test import APIRequestFactory
    from caper.views_apis import FileUploadView

    assert os.path.exists(DATASET_SMALL_TAR), \
        f"Test dataset not found: {DATASET_SMALL_TAR}"

    rf = APIRequestFactory()
    with open(DATASET_SMALL_TAR, 'rb') as fh:
        req = rf.post(
            '/upload_api/',
            data={
                'project_name':     'APITest_NoMembers',
                'description':      'Automated pytest API upload test',
                'private':          'private',
                'publication_link': '',
                'project_members':  '',
                'alias':            '',
                'remap_sample_names': 'false',
                'accept_license':   'on',
                'file':             fh,
            },
            format='multipart')

    response = FileUploadView.as_view()(req)

    assert response.status_code == 400, \
        f"Expected 400 without project_members, got {response.status_code}"
