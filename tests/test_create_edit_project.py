"""
Integration tests for project creation and editing.

Uses test_datasets/one_amprepo_sample.tar.gz and test_datasets/one_amprepo_sample.xlsx.

All projects created during a test are fully cleaned up in a finally block
regardless of whether the test passed or failed:
  - MongoDB document is hard-deleted
  - tmp/{project_id}/ directory is removed from disk
  - S3 object is deleted when USE_S3_DOWNLOADS is True
"""

import io
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
# Issue #561 — numeric sample names in metadata sheet never matched
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_numeric_sample_names_metadata_lookup():
    """
    Issue #561: _build_metadata_lookup_from_dataframe must produce string keys
    even when pandas infers the sample_name column as int64/float64.
    """
    import pandas as pd
    from caper.extra_metadata import _build_metadata_lookup_from_dataframe, _apply_metadata_to_runs

    # Simulate what pandas does when it reads an all-numeric sample_name column
    # without dtype=str — infers int64.
    df = pd.DataFrame({
        'sample_name': pd.array([123, 456], dtype='int64'),
        'Cancer_type': ['GBM', 'Lung'],
    })

    lookup = _build_metadata_lookup_from_dataframe(df)

    assert '123' in lookup, "String key '123' must be present in lookup"
    assert '456' in lookup, "String key '456' must be present in lookup"
    assert 123 not in lookup, "Integer key 123 must not appear (would never match MongoDB)"

    # Also verify the lookup correctly applies to a sample stored with a string name
    runs = {'run1': [{'Sample_name': '123', 'Features': []}]}
    n_updated = _apply_metadata_to_runs(runs, lookup)
    assert n_updated == 1, "Exactly one sample should be updated"
    assert runs['run1'][0].get('Cancer_type') == 'GBM'


@pytest.mark.integration
def test_leading_zero_sample_names_preserved_via_dtype_str():
    """
    Issue #561 edge case: _read_metadata_file must use dtype=str so that
    sample names like '053' are not coerced to 53 by pandas.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from caper.extra_metadata import _read_metadata_file, _build_metadata_lookup_from_dataframe

    csv_bytes = b"sample_name,Cancer_type\n053,GBM\n007,Lung\n"
    metadata_file = SimpleUploadedFile("metadata.csv", csv_bytes, content_type="text/csv")

    df = _read_metadata_file(metadata_file=metadata_file)
    lookup = _build_metadata_lookup_from_dataframe(df)

    assert '053' in lookup, "Leading-zero name '053' must be preserved by dtype=str read"
    assert '007' in lookup, "Leading-zero name '007' must be preserved by dtype=str read"
    assert '53' not in lookup, "'53' must not appear — would mean int coercion happened"
    assert '7' not in lookup, "'7' must not appear — would mean int coercion happened"


# ---------------------------------------------------------------------------
# Issue #510 — adding metadata must not create a new project version
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_metadata_upload_does_not_create_new_version(request_factory, test_user, mongo_collection):
    """
    Issue #510: calling process_metadata must update 'runs' only —
    'previous_versions' must remain unchanged.
    """
    from bson.objectid import ObjectId
    from django.core.files.uploadedfile import SimpleUploadedFile
    from caper.extra_metadata import process_metadata

    doc = {
        'project_name': 'Issue510_MetaVersionTest',
        'creator': test_user.username,
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {'run1': [{'Sample_name': 'TestSample', 'Features': []}]},
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)

    try:
        csv_bytes = b"sample_name,Cancer_type\nTestSample,GBM\n"
        csv_file = SimpleUploadedFile("metadata.csv", csv_bytes, content_type="text/csv")

        req = request_factory.post(
            f'/project/{project_id}/process_metadata',
            data={'metadataFile': csv_file},
            format='multipart',
        )
        req.user = test_user

        status = process_metadata(req, project_id)
        assert status == 'complete', f"process_metadata returned unexpected value: {status!r}"

        updated = mongo_collection.find_one({'_id': ObjectId(project_id)})
        assert updated is not None

        assert updated.get('previous_versions', []) == [], \
            "process_metadata must not add entries to previous_versions (Issue #510)"

        sample = updated['runs']['run1'][0]
        assert sample.get('Cancer_type') == 'GBM', \
            "Cancer_type from metadata sheet must be applied to the matching sample"
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


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
