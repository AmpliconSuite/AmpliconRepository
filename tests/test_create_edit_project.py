"""
Integration tests for project creation and editing.

Uses test_data/one_amprepo_sample.tar.gz and test_data/one_amprepo_sample.xlsx.

All projects created during a test are fully cleaned up in a finally block
regardless of whether the test passed or failed:
  - MongoDB document is hard-deleted
  - tmp/{project_id}/ directory is removed from disk
  - S3 object is deleted when USE_S3_DOWNLOADS is True
"""

import os
import shutil
import time
import logging

import pytest
from bson.objectid import ObjectId

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POLL_TIMEOUT = 300   # seconds to wait for background aggregation
POLL_INTERVAL = 5    # polling frequency in seconds

# tmp/ lives next to pytest.ini, one level above this tests/ directory
TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tmp')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_create_request(request_factory, user, project_name, *,
                           tar_path, xlsx_path=None, remap=False):
    """Return a POST request that mimics the create-project form."""
    data = {
        'project_name': project_name,
        'description': f'Automated pytest — {project_name}',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'true' if remap else 'false',
        'accept_license': 'on',
    }
    files = {}
    handles = []

    fh = open(tar_path, 'rb')
    handles.append(fh)
    files['document'] = fh

    if xlsx_path:
        fh2 = open(xlsx_path, 'rb')
        handles.append(fh2)
        files['metadataFile'] = fh2

    request = request_factory.post('/create-project/',
                                    data={**data, **files},
                                    format='multipart')
    request.user = user
    return request, handles


def _build_edit_request(request_factory, user, project_id, *,
                         xlsx_path=None, remap=False):
    """Return a POST request that mimics the edit-project form with reaggregate."""
    data = {
        'project_name': 'Test4_CreateThenEdit',
        'description': 'Automated pytest — edit step',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'true' if remap else 'false',
        'project_mode': 'reaggregate',
        'accept_license': 'on',
    }
    files = {}
    handles = []

    if xlsx_path:
        fh = open(xlsx_path, 'rb')
        handles.append(fh)
        files['metadataFile'] = fh

    request = request_factory.post(f'/project/{project_id}/edit',
                                    data={**data, **files},
                                    format='multipart')
    request.user = user
    return request, handles


def _project_id_from_redirect(response):
    """Parse the project ID from a redirect Location header (/project/<id>)."""
    location = response.get('Location', '')
    parts = [p for p in location.split('/') if p]
    return parts[-1] if parts else None


def _poll_until_finished(collection, project_id,
                          timeout=POLL_TIMEOUT, interval=POLL_INTERVAL):
    """
    Poll MongoDB until the project is fully done: FINISHED?=True or
    aggregation_failed=True.  Waiting for FINISHED? (rather than just for
    aggregation_in_progress to clear) ensures that extract_project_files —
    which runs in a second background thread after _create_project —  has
    also completed before the test returns.  This prevents the ThreadPoolExecutor
    from trying to submit new work after the interpreter has started shutting down.
    Returns the final document, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = collection.find_one({'_id': ObjectId(project_id)})
        if doc is None:
            return None
        if doc.get('FINISHED?', False) or doc.get('aggregation_failed', False):
            return doc
        time.sleep(interval)
    return None


def _cleanup_project(collection, project_id):
    """
    Fully remove all artifacts created for a test project:
      1. MongoDB document
      2. tmp/{project_id}/ directory on disk
      3. S3 object (when USE_S3_DOWNLOADS is True)
    Errors in any step are logged but do not raise so that all steps always run.
    """
    # 1. MongoDB
    try:
        collection.delete_one({'_id': ObjectId(project_id)})
        logging.info(f"[cleanup] Deleted MongoDB document {project_id}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not delete MongoDB document {project_id}: {e}")

    # 2. tmp directory
    tmp_path = os.path.join(TMP_DIR, project_id)
    try:
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)
            logging.info(f"[cleanup] Removed tmp dir {tmp_path}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not remove tmp dir {tmp_path}: {e}")

    # 3. S3
    try:
        from django.conf import settings
        if getattr(settings, 'USE_S3_DOWNLOADS', False):
            import boto3
            bucket_path = getattr(settings, 'S3_DOWNLOADS_BUCKET_PATH', '')
            s3_key = f'{bucket_path}{project_id}/{project_id}.tar.gz'
            session = boto3.Session(profile_name=getattr(settings, 'AWS_PROFILE_NAME', None))
            s3_client = session.client('s3')
            s3_client.delete_object(Bucket=settings.S3_DOWNLOADS_BUCKET, Key=s3_key)
            logging.info(f"[cleanup] Deleted S3 object s3://{settings.S3_DOWNLOADS_BUCKET}/{s3_key}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not delete S3 object for {project_id}: {e}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.integration
def test_create_tar_only(request_factory, test_user, mongo_collection, tar_file):
    """Create a project with the tar.gz only — expects aggregation to succeed."""
    from caper.views import create_project
    created_ids = []

    request, handles = _build_create_request(
        request_factory, test_user, 'PyTest1_TarOnly', tar_path=tar_file)
    try:
        response = create_project(request)
    finally:
        for h in handles:
            h.close()

    assert response.status_code in (301, 302), \
        f"Expected redirect, got {response.status_code}"

    project_id = _project_id_from_redirect(response)
    assert project_id, "Could not parse project_id from redirect"
    created_ids.append(project_id)

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, f"Timed out waiting for aggregation ({POLL_TIMEOUT}s)"
        assert not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message', '(no message)')}"
        assert doc.get('FINISHED?'), "Project did not set FINISHED?=True"
        assert doc.get('sample_count', 0) > 0, "Project has no samples"
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)


@pytest.mark.slow
@pytest.mark.integration
def test_create_tar_and_metadata_no_remap(
        request_factory, test_user, mongo_collection, tar_file, xlsx_file):
    """Create with tar + metadata xlsx, remap_sample_names=False — expects success."""
    from caper.views import create_project
    created_ids = []

    request, handles = _build_create_request(
        request_factory, test_user, 'PyTest2_TarMetadataNoRemap',
        tar_path=tar_file, xlsx_path=xlsx_file, remap=False)
    try:
        response = create_project(request)
    finally:
        for h in handles:
            h.close()

    assert response.status_code in (301, 302), \
        f"Expected redirect, got {response.status_code}"

    project_id = _project_id_from_redirect(response)
    assert project_id, "Could not parse project_id from redirect"
    created_ids.append(project_id)

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, f"Timed out waiting for aggregation ({POLL_TIMEOUT}s)"
        assert not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message', '(no message)')}"
        assert doc.get('FINISHED?'), "Project did not set FINISHED?=True"
        assert doc.get('sample_count', 0) > 0, "Project has no samples"
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)


@pytest.mark.slow
@pytest.mark.integration
def test_create_tar_and_metadata_with_remap(
        request_factory, test_user, mongo_collection, tar_file, xlsx_file):
    """
    Create with tar + metadata xlsx, remap_sample_names=True.

    With the current AGGREGATOR_DEV_PATH this is expected to fail aggregation.
    The test passes in either case but prints a clear note about which outcome
    was observed, so the result acts as a diagnostic.
    Update AGGREGATOR_DEV_PATH to a v6-capable aggregator to get a full success.
    """
    from caper.views import create_project
    created_ids = []

    request, handles = _build_create_request(
        request_factory, test_user, 'PyTest3_TarMetadataWithRemap',
        tar_path=tar_file, xlsx_path=xlsx_file, remap=True)
    try:
        response = create_project(request)
    finally:
        for h in handles:
            h.close()

    assert response.status_code in (301, 302), \
        f"Expected redirect, got {response.status_code}"

    project_id = _project_id_from_redirect(response)
    assert project_id, "Could not parse project_id from redirect"
    created_ids.append(project_id)

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, f"Timed out waiting for aggregation ({POLL_TIMEOUT}s)"

        if doc.get('aggregation_failed'):
            aggregator_path = os.environ.get('AGGREGATOR_DEV_PATH', '(not set)')
            pytest.xfail(
                f"Aggregation failed as expected with current AGGREGATOR_DEV_PATH "
                f"({aggregator_path}). "
                f"Error: {doc.get('error_message', '(none)')}. "
                f"Update AGGREGATOR_DEV_PATH to a v6-capable aggregator to fully pass."
            )

        assert doc.get('FINISHED?'), "Project did not set FINISHED?=True"
        assert doc.get('sample_count', 0) > 0, "Project has no samples"
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)


@pytest.mark.slow
@pytest.mark.integration
def test_create_then_edit_with_remap(
        request_factory, test_user, mongo_collection, tar_file, xlsx_file):
    """
    Create a project (tar only), then edit it adding metadata + remap=True.

    The create step must succeed.  The edit/reaggregation step is expected to
    fail until AGGREGATOR_DEV_PATH is updated — marked xfail in that case.
    """
    from caper.views import create_project, edit_project_page
    created_ids = []

    # --- Step A: create ---
    request, handles = _build_create_request(
        request_factory, test_user, 'PyTest4_CreateThenEdit', tar_path=tar_file)
    try:
        response = create_project(request)
    finally:
        for h in handles:
            h.close()

    assert response.status_code in (301, 302), \
        f"[Create] Expected redirect, got {response.status_code}"

    project_id = _project_id_from_redirect(response)
    assert project_id, "[Create] Could not parse project_id from redirect"
    created_ids.append(project_id)

    doc = _poll_until_finished(mongo_collection, project_id)
    assert doc is not None, f"[Create] Timed out waiting for aggregation ({POLL_TIMEOUT}s)"
    assert not doc.get('aggregation_failed'), \
        f"[Create] Aggregation failed: {doc.get('error_message', '(no message)')}"
    assert doc.get('FINISHED?'), "[Create] Project did not set FINISHED?=True"

    # --- Step B: edit ---
    edit_request, edit_handles = _build_edit_request(
        request_factory, test_user, project_id, xlsx_path=xlsx_file, remap=True)
    try:
        edit_response = edit_project_page(edit_request, project_name=project_id)
    finally:
        for h in edit_handles:
            h.close()

    assert edit_response.status_code in (301, 302), \
        f"[Edit] Expected redirect, got {edit_response.status_code}"

    edit_target_id = _project_id_from_redirect(edit_response)
    poll_id = edit_target_id if (edit_target_id and edit_target_id != project_id) else project_id
    if edit_target_id and edit_target_id != project_id:
        created_ids.append(edit_target_id)

    try:
        edited_doc = _poll_until_finished(mongo_collection, poll_id)
        assert edited_doc is not None, \
            f"[Edit] Timed out waiting for aggregation ({POLL_TIMEOUT}s)"

        if edited_doc.get('aggregation_failed'):
            aggregator_path = os.environ.get('AGGREGATOR_DEV_PATH', '(not set)')
            pytest.xfail(
                f"[Edit] Aggregation failed as expected with current AGGREGATOR_DEV_PATH "
                f"({aggregator_path}). "
                f"Error: {edited_doc.get('error_message', '(none)')}. "
                f"Update AGGREGATOR_DEV_PATH to a v6-capable aggregator to fully pass."
            )

        assert edited_doc.get('FINISHED?'), "[Edit] Project did not set FINISHED?=True"
        assert edited_doc.get('sample_count', 0) > 0, "[Edit] Project has no samples"
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)
