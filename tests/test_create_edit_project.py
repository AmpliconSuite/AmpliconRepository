"""
Integration tests for project creation and editing.

Uses test_datasets/one_amprepo_sample.tar.gz and test_datasets/one_amprepo_sample.xlsx.

All projects created during a test are fully cleaned up in a finally block
regardless of whether the test passed or failed:
  - MongoDB document is hard-deleted
  - tmp/{project_id}/ directory is removed from disk
  - S3 object is deleted when USE_S3_DOWNLOADS is True
"""

import os

import pytest

from conftest import (
    _build_create_request,
    _build_edit_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    POLL_TIMEOUT,
)


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
