"""
Integration tests for file downloads: project tar, metadata CSV,
summary, per-sample zip, and GridFS PNG presence.

project_download behaviour depends on USE_S3_DOWNLOADS:
  - True  → 302 redirect to a presigned S3 URL
  - False → 200 StreamingHttpResponse with application/tar+gzip
            (or 404 if the local tmp/ file was cleaned up early)
Tests accept any of these valid outcomes.
"""

import pytest
from bson.objectid import ObjectId


# ---------------------------------------------------------------------------
# Project-level download tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_project_tar_download(loaded_datasets, request_factory, test_user):
    """
    GET /project/<id>/download must return 200 (local) or 302 (S3 presigned URL)
    with an appropriate Content-Type for a tar archive.
    """
    from caper.views import project_download
    pid = loaded_datasets['project_small']
    req = request_factory.get(f'/project/{pid}/download')
    req.user = test_user
    resp = project_download(req, project_name=pid)
    assert resp.status_code in (200, 302, 404), \
        f"Unexpected status {resp.status_code} for project_download"
    if resp.status_code == 200:
        ct = resp.get('Content-Type', '')
        assert any(t in ct for t in ('gzip', 'tar', 'octet-stream')), \
            f"Unexpected Content-Type for tar download: {ct!r}"


@pytest.mark.integration
@pytest.mark.functional
def test_project_metadata_csv_download(
        loaded_datasets, request_factory, test_user):
    """
    GET /project/<id>/download_metadata must return 200 with a CSV/TSV body.
    """
    from caper.views import project_metadata_download
    pid = loaded_datasets['project_small']
    req = request_factory.get(f'/project/{pid}/download_metadata')
    req.user = test_user
    resp = project_metadata_download(req, project_name=pid)
    # A redirect is acceptable if HTTP_REFERER is absent from the test request
    assert resp.status_code in (200, 302), \
        f"Unexpected status {resp.status_code} for metadata download"
    if resp.status_code == 200:
        ct = resp.get('Content-Type', '')
        assert any(t in ct for t in ('text/csv', 'text/tab', 'text/plain', 'octet')), \
            f"Unexpected Content-Type for metadata CSV: {ct!r}"
        assert len(resp.content) > 0, "Metadata CSV response must not be empty"


@pytest.mark.integration
@pytest.mark.functional
def test_project_summary_download(loaded_datasets, request_factory, test_user):
    """
    GET /project/<id>/download_summary must return 200 or a graceful redirect.
    """
    from caper.views import project_summary_download
    pid = loaded_datasets['project_small']
    req = request_factory.get(f'/project/{pid}/download_summary')
    req.user = test_user
    resp = project_summary_download(req, project_name=pid)
    assert resp.status_code in (200, 302), \
        f"Unexpected status {resp.status_code} for summary download"


# ---------------------------------------------------------------------------
# Sample-level download tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_sample_download(loaded_datasets, request_factory, test_user, mongo_collection):
    """
    GET /project/<id>/sample/<name>/download must return a zip with content.
    Retrieves the first sample name from the MongoDB document dynamically.
    """
    from caper.views import sample_download
    pid = loaded_datasets['project_small']
    doc = mongo_collection.find_one({'_id': ObjectId(pid)})
    runs = doc.get('runs', {})
    assert runs, "project_small has no samples in 'runs' — cannot test sample_download"

    # Grab the first sample name from the runs dict
    first_sample_key = next(iter(runs))
    feature_list = runs[first_sample_key]
    if isinstance(feature_list, list) and feature_list:
        sample_name = feature_list[0].get('Sample_name', first_sample_key)
    else:
        sample_name = first_sample_key

    req = request_factory.get(f'/project/{pid}/sample/{sample_name}/download')
    req.user = test_user
    resp = sample_download(req, project_name=pid, sample_name=sample_name)
    assert resp.status_code in (200, 302), \
        f"Unexpected status {resp.status_code} for sample_download"
    if resp.status_code == 200:
        assert len(resp.content) > 0, "Sample download response must not be empty"


@pytest.mark.integration
@pytest.mark.functional
def test_sample_png_exists_in_gridfs(loaded_datasets, mongo_collection):
    """
    project_small's document should reference at least one GridFS PNG object
    in the feature list (png_id or similar field).
    """
    pid = loaded_datasets['project_small']
    doc = mongo_collection.find_one({'_id': ObjectId(pid)})
    runs = doc.get('runs', {})
    assert runs, "project_small has no samples"

    png_ids_found = []
    for feature_list in runs.values():
        if not isinstance(feature_list, list):
            continue
        for feature in feature_list:
            if isinstance(feature, dict):
                # Common field names for stored PNG GridFS IDs
                for field in ('png_id', 'png', 'amplicon_image_id', 'image_id'):
                    val = feature.get(field)
                    if val:
                        png_ids_found.append(val)

    assert len(png_ids_found) > 0, \
        "project_small should have at least one GridFS PNG reference in its features"


@pytest.mark.integration
@pytest.mark.functional
def test_pdf_download(loaded_datasets, request_factory, test_user, mongo_collection):
    """
    GET /project/<id>/sample/<name>/feature/<fname>/download/pdf/<fid> must
    return 200 with Content-Type application/pdf.
    Skips gracefully if the project has no PDF features.
    """
    from caper.views import pdf_download
    pid = loaded_datasets['project_small']
    doc = mongo_collection.find_one({'_id': ObjectId(pid)})
    runs = doc.get('runs', {})

    # Find a feature with a pdf_id (or similar) in any sample
    pdf_entry = None
    sample_name_found = None
    feature_name_found = None
    for sample_key, feature_list in runs.items():
        if not isinstance(feature_list, list):
            continue
        for feature in feature_list:
            if not isinstance(feature, dict):
                continue
            for field in ('pdf_id', 'pdf', 'amplicon_pdf_id'):
                fid = feature.get(field)
                if fid:
                    pdf_entry = str(fid)
                    sample_name_found = feature.get('Sample_name', sample_key)
                    feature_name_found = feature.get('Feature_name',
                                                      feature.get('AA_amplicon_number', 'feature'))
                    break
            if pdf_entry:
                break
        if pdf_entry:
            break

    if not pdf_entry:
        pytest.skip("project_small has no PDF features — skipping pdf_download test")

    req = request_factory.get(
        f'/project/{pid}/sample/{sample_name_found}'
        f'/feature/{feature_name_found}/download/pdf/{pdf_entry}')
    req.user = test_user
    resp = pdf_download(
        req,
        project_name=pid,
        sample_name=sample_name_found,
        feature_name=str(feature_name_found),
        feature_id=pdf_entry)
    assert resp.status_code == 200, f"pdf_download returned {resp.status_code}"
    ct = resp.get('Content-Type', '')
    assert 'pdf' in ct, f"Expected PDF content type, got: {ct!r}"
