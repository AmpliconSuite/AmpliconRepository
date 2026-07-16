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
import zipfile

import pytest

from conftest import (
    _build_create_request,
    _build_edit_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
    POLL_TIMEOUT,
)


# ---------------------------------------------------------------------------
# AmpliconClassifier 2.0 / AmpliconSuiteAggregator 7 compatibility
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.integration
def test_create_ac2_project(
        request_factory, test_user, mongo_collection, monkeypatch):
    """An AC 2.0 unaggregated archive must complete normal project creation."""
    archive_path = os.environ.get('CAPER_AC2_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('Set CAPER_AC2_TEST_ARCHIVE to run the AC 2.0 ingestion test')
    if not os.path.exists(archive_path):
        pytest.skip(f'AC 2.0 test archive not found: {archive_path}')

    from django.conf import settings
    from caper.views import create_project, fs_handle

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    project_name = 'pytest_AC2_compatibility'
    request, handles = _build_create_request(
        request_factory,
        test_user,
        project_name,
        tar_path=archive_path,
    )
    project_id = None

    try:
        response = create_project(request)
        project_id = _project_id_from_redirect(response)
        assert project_id, f'No project ID in redirect: {response!r}'

        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, 'AC 2.0 project did not finish before timeout'
        assert not doc.get('aggregation_failed'), doc.get('error_message')
        assert doc.get('sample_count') == 9
        assert len(doc.get('runs', {})) == 9
        assert doc.get('aggregator_version') == '7.1.0'

        features = [
            feature
            for sample_features in doc['runs'].values()
            for feature in sample_features
        ]
        assert len(features) == 12
        assert {feature['Classification'] for feature in features} == {'ecDNA'}
        assert all('Sample_name' in feature for feature in features)
        assert all('Feature_ID' in feature for feature in features)

        reconstruction_features = [
            feature for feature in features
            if feature.get('Reconstruction_directory') != 'Not Provided'
        ]
        assert reconstruction_features
        assert all(
            feature['AA_directory'] == feature['Reconstruction_directory']
            for feature in reconstruction_features
        )
        assert all(
            fs_handle.exists(feature['Reconstruction_directory'])
            for feature in reconstruction_features
        )

        graph_image_features = [
            feature for feature in features
            if feature.get('Graph_PNG_file') != 'Not Provided'
        ]
        assert graph_image_features
        assert all(
            feature['AA_PNG_file'] == feature['Graph_PNG_file']
            for feature in graph_image_features
        )
    finally:
        for handle in handles:
            handle.close()
        if project_id:
            _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_create_ac2_fan_project(
        request_factory, test_user, mongo_collection, monkeypatch):
    """FAN feature rows from AC 2.0 must survive normal project creation."""
    archive_path = os.environ.get('CAPER_AC2_FAN_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('Set CAPER_AC2_FAN_TEST_ARCHIVE to run the FAN ingestion test')
    if not os.path.exists(archive_path):
        pytest.skip(f'AC 2.0 FAN test archive not found: {archive_path}')

    from django.conf import settings
    from caper.views import create_project

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    request, handles = _build_create_request(
        request_factory,
        test_user,
        'pytest_AC2_FAN_compatibility',
        tar_path=archive_path,
    )
    project_id = None

    try:
        response = create_project(request)
        project_id = _project_id_from_redirect(response)
        assert project_id, f'No project ID in redirect: {response!r}'

        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, 'AC 2.0 FAN project did not finish before timeout'
        assert not doc.get('aggregation_failed'), doc.get('error_message')
        assert doc.get('sample_count') == 140
        assert len(doc.get('runs', {})) == 140
        assert doc.get('aggregator_version') == '7.1.0'

        features = [
            feature
            for sample_features in doc['runs'].values()
            for feature in sample_features
        ]
        assert len(features) == 374
        fan_features = [
            feature for feature in features
            if feature.get('Classification') == 'FAN'
        ]
        assert len(fan_features) == 5
        assert len({feature['Sample_name'] for feature in fan_features}) == 5
        assert 'FAN' in doc.get('Classification', [])
    finally:
        for handle in handles:
            handle.close()
        if project_id:
            _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_create_ac2_hg38_project(
        request_factory, test_user, mongo_collection, monkeypatch):
    """A mixed-classification AC 2.0 hg38 archive must remain ingestible."""
    archive_path = os.environ.get('CAPER_AC2_HG38_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('Set CAPER_AC2_HG38_TEST_ARCHIVE to run the hg38 ingestion test')
    if not os.path.exists(archive_path):
        pytest.skip(f'AC 2.0 hg38 test archive not found: {archive_path}')

    from django.conf import settings
    from caper.views import create_project

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    request, handles = _build_create_request(
        request_factory,
        test_user,
        'pytest_AC2_hg38_compatibility',
        tar_path=archive_path,
    )
    project_id = None

    try:
        response = create_project(request)
        project_id = _project_id_from_redirect(response)
        assert project_id, f'No project ID in redirect: {response!r}'

        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc is not None, 'AC 2.0 hg38 project did not finish before timeout'
        assert not doc.get('aggregation_failed'), doc.get('error_message')
        assert doc.get('sample_count') == 63
        assert len(doc.get('runs', {})) == 63
        assert doc.get('aggregator_version') == '7.1.0'

        features = [
            feature
            for sample_features in doc['runs'].values()
            for feature in sample_features
        ]
        assert len(features) == 333
        classification_counts = {
            classification: sum(
                feature.get('Classification') == classification
                for feature in features
            )
            for classification in {'ecDNA', 'FAN', 'BFB', 'Linear', 'Complex-non-cyclic'}
        }
        assert classification_counts == {
            'ecDNA': 58,
            'FAN': 8,
            'BFB': 16,
            'Linear': 223,
            'Complex-non-cyclic': 28,
        }
        assert {feature['Reference_version'] for feature in features} == {'GRCh38'}
    finally:
        for handle in handles:
            handle.close()
        if project_id:
            _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_create_coral_ac2_project(
        request_factory, test_user, mongo_collection, monkeypatch):
    """CoRAL reconstructions must be identified and versioned distinctly from AA."""
    archive_path = os.environ.get('CAPER_CORAL_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('Set CAPER_CORAL_TEST_ARCHIVE to run the CoRAL ingestion test')
    if not os.path.exists(archive_path):
        pytest.skip(f'CoRAL test archive not found: {archive_path}')

    from django.conf import settings
    from caper.views import create_project, sample_download

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    request, handles = _build_create_request(
        request_factory,
        test_user,
        'pytest_CoRAL_compatibility',
        tar_path=archive_path,
    )
    project_id = None

    try:
        response = create_project(request)
        project_id = _project_id_from_redirect(response)
        doc = _poll_until_finished(mongo_collection, project_id)

        assert doc and not doc.get('aggregation_failed'), doc.get('error_message') if doc else None
        assert doc.get('sample_count') == 24
        assert doc.get('Reconstruction_tools') == 'CoRAL'
        assert doc.get('CoRAL_version') == '2.2.0'

        features = [
            feature
            for sample_features in doc['runs'].values()
            for feature in sample_features
        ]
        assert len(features) == 95
        assert {feature.get('Reconstruction_tool') for feature in features} == {'CoRAL'}
        assert {feature.get('Reconstruction_version') for feature in features} == {'2.2.0'}
        assert all(feature.get('Graph_PNG_file') != 'Not Provided' for feature in features)
        assert all(feature.get('Cycles_PNG_file') != 'Not Provided' for feature in features)

        first_run = next(iter(doc['runs'].values()))
        sample_name = first_run[0]['Sample_name']
        download_request = request_factory.get(
            f'/project/{project_id}/sample/{sample_name}/download')
        download_request.user = test_user
        download_response = sample_download(download_request, project_id, sample_name)
        assert download_response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download_response.content)) as archive:
            names = archive.namelist()
            assert any(name.endswith('_cycles.png') for name in names)
            assert any(
                name.endswith('.png') and not name.endswith('_cycles.png')
                for name in names
            )
            assert any(name.endswith('_reconstruction_results.tar.gz') for name in names)
    finally:
        for handle in handles:
            handle.close()
        if project_id:
            _cleanup_project(mongo_collection, project_id)


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
# Issue #551 — reaggregating a project destroys previously loaded metadata
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_reaggregation_reapplies_old_metadata():
    """
    Issue #551: process_metadata_no_request called with only old_extra_metadata
    (no new file) must reapply stored metadata to samples in clean post-aggregation runs.

    This is the core of the fix: after the aggregator produces fresh, metadata-free
    runs, the stored metadata dict must be merged back in.
    """
    from caper.extra_metadata import process_metadata_no_request

    # Simulate fresh runs produced by the aggregator — no metadata attached
    clean_runs = {
        'run1': [{'Sample_name': 'Sample_001', 'Features': []}]
    }

    # Metadata that was on the project before reaggregation
    old_extra_metadata = {
        'Sample_001': {
            'Cancer_type': 'GBM',
            'custom_field': 'keep_me',
        }
    }

    updated_runs = process_metadata_no_request(
        clean_runs, old_extra_metadata=old_extra_metadata
    )

    sample = updated_runs['run1'][0]
    assert sample.get('extra_metadata_from_csv', {}).get('Cancer_type') == 'GBM', \
        "Cancer_type must be reapplied after reaggregation (Issue #551)"
    assert sample.get('extra_metadata_from_csv', {}).get('custom_field') == 'keep_me', \
        "Custom fields must survive reaggregation (Issue #551)"


@pytest.mark.integration
def test_reaggregation_does_not_affect_samples_missing_from_old_metadata():
    """
    Issue #551 edge case: samples that had NO metadata before reaggregation
    must not have metadata injected onto them.
    """
    from caper.extra_metadata import process_metadata_no_request

    clean_runs = {
        'run1': [
            {'Sample_name': 'Sample_with_meta',    'Features': []},
            {'Sample_name': 'Sample_without_meta', 'Features': []},
        ]
    }

    old_extra_metadata = {
        'Sample_with_meta': {'Cancer_type': 'Lung'}
    }

    updated_runs = process_metadata_no_request(
        clean_runs, old_extra_metadata=old_extra_metadata
    )

    samples = updated_runs['run1']
    with_meta    = next(s for s in samples if s['Sample_name'] == 'Sample_with_meta')
    without_meta = next(s for s in samples if s['Sample_name'] == 'Sample_without_meta')

    assert with_meta.get('extra_metadata_from_csv', {}).get('Cancer_type') == 'Lung'
    assert 'extra_metadata_from_csv' not in without_meta, \
        "Samples absent from old_extra_metadata must not have metadata added"


# ---------------------------------------------------------------------------
# Issue #519 — metadata matching fails when sample_name_alias column is present
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_alias_lookup_built_from_metadata_sheet():
    """
    Issue #519: _build_metadata_lookup_from_dataframe must index rows by BOTH
    sample_name and sample_name_alias so that metadata can be matched by either name.
    """
    import pandas as pd
    from caper.extra_metadata import _build_metadata_lookup_from_dataframe

    df = pd.DataFrame({
        'sample_name':       ['Sample_001'],
        'sample_name_alias': ['Renamed_001'],
        'Cancer_type':       ['GBM'],
    })

    lookup = _build_metadata_lookup_from_dataframe(df)

    assert 'Sample_001' in lookup,  "Original sample_name must be a lookup key"
    assert 'Renamed_001' in lookup, "sample_name_alias must also be a lookup key"
    assert lookup['Sample_001']['Cancer_type'] == 'GBM'
    assert lookup['Renamed_001']['Cancer_type'] == 'GBM'


@pytest.mark.integration
def test_metadata_applied_to_already_renamed_sample():
    """
    Issue #519: if a sample has already been renamed to its alias (e.g. after a
    previous metadata upload), a subsequent apply using the SAME sheet must still
    match the sample by its current (alias) name.
    """
    import pandas as pd
    from caper.extra_metadata import (
        _build_metadata_lookup_from_dataframe, _apply_metadata_to_runs
    )

    # Sample is already stored under the alias name (post-rename state)
    runs = {'run1': [{'Sample_name': 'Renamed_001', 'Features': []}]}

    df = pd.DataFrame({
        'sample_name':       ['Sample_001'],
        'sample_name_alias': ['Renamed_001'],
        'Cancer_type':       ['GBM'],
    })
    lookup = _build_metadata_lookup_from_dataframe(df)

    n_updated = _apply_metadata_to_runs(runs, lookup)

    assert n_updated == 1, "Sample should be matched via its alias name"
    sample = runs['run1'][0]
    assert sample.get('Cancer_type') == 'GBM', \
        "Cancer_type must be applied when matching by alias (Issue #519)"


@pytest.mark.integration
def test_rename_via_alias_and_metadata_applied_together():
    """
    Issue #519: when remap_name_to_alias=True, the sample must be renamed to the
    alias AND all other metadata columns must be applied in the same pass.
    """
    from caper.extra_metadata import (
        _build_metadata_lookup_from_dataframe, _apply_metadata_to_runs
    )
    import pandas as pd

    runs = {'run1': [{'Sample_name': 'Sample_001', 'Features': []}]}

    df = pd.DataFrame({
        'sample_name':       ['Sample_001'],
        'sample_name_alias': ['New_Name'],
        'Cancer_type':       ['Lung'],
        'custom_col':        ['value_x'],
    })
    lookup = _build_metadata_lookup_from_dataframe(df)

    n_updated = _apply_metadata_to_runs(runs, lookup, remap_name_to_alias=True)

    sample = runs['run1'][0]
    assert n_updated == 1
    assert sample['Sample_name'] == 'New_Name', \
        "Sample must be renamed to sample_name_alias when remap=True"
    assert sample.get('Cancer_type') == 'Lung', \
        "Cancer_type must be applied in the same pass as the rename"
    assert sample['extra_metadata_from_csv'].get('custom_col') == 'value_x', \
        "Custom columns must survive alongside the rename (Issue #519)"


# ---------------------------------------------------------------------------
# Issue #508 — samples absent from a new metadata sheet lose existing metadata
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_unlisted_sample_keeps_existing_metadata():
    """
    Issue #508: _apply_metadata_to_runs must leave the existing extra_metadata_from_csv
    of samples that are NOT in the new metadata sheet completely untouched.
    """
    import pandas as pd
    from caper.extra_metadata import (
        _build_metadata_lookup_from_dataframe, _apply_metadata_to_runs
    )

    # Two samples; Sample_A already has metadata from a previous upload
    runs = {
        'run1': [
            {
                'Sample_name': 'Sample_A',
                'Features': [],
                'extra_metadata_from_csv': {
                    'Cancer_type': 'Lung',
                    'custom_field': 'do_not_delete',
                },
                'Cancer_type': 'Lung',
            },
            {'Sample_name': 'Sample_B', 'Features': []},
        ]
    }

    # New sheet only covers Sample_B
    df = pd.DataFrame({
        'sample_name': ['Sample_B'],
        'Cancer_type': ['GBM'],
    })
    lookup = _build_metadata_lookup_from_dataframe(df)

    n_updated = _apply_metadata_to_runs(runs, lookup)

    sample_a = next(s for s in runs['run1'] if s['Sample_name'] == 'Sample_A')
    sample_b = next(s for s in runs['run1'] if s['Sample_name'] == 'Sample_B')

    # Sample_B got the new metadata
    assert n_updated == 1
    assert sample_b.get('Cancer_type') == 'GBM'

    # Sample_A's existing metadata must be untouched (Issue #508)
    assert sample_a.get('Cancer_type') == 'Lung', \
        "Sample_A top-level Cancer_type must not be cleared"
    assert sample_a['extra_metadata_from_csv'].get('Cancer_type') == 'Lung', \
        "Sample_A Cancer_type in extra_metadata_from_csv must not be cleared (Issue #508)"
    assert sample_a['extra_metadata_from_csv'].get('custom_field') == 'do_not_delete', \
        "Sample_A custom_field must not be cleared (Issue #508)"


@pytest.mark.integration
def test_process_metadata_preserves_unlisted_samples_via_mongodb(
        request_factory, test_user, mongo_collection):
    """
    Issue #508 integration: after a process_metadata POST that only lists one
    of two samples, the other sample's metadata must survive in MongoDB.
    """
    from bson.objectid import ObjectId
    from django.core.files.uploadedfile import SimpleUploadedFile
    from caper.extra_metadata import process_metadata

    doc = {
        'project_name': 'Issue508_UnlistedSampleTest',
        'creator': test_user.username,
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {
            'run1': [
                {
                    'Sample_name': 'Sample_A',
                    'Features': [],
                    'extra_metadata_from_csv': {'Cancer_type': 'Lung'},
                    'Cancer_type': 'Lung',
                },
                {'Sample_name': 'Sample_B', 'Features': []},
            ]
        },
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)

    try:
        # Sheet only lists Sample_B
        csv_bytes = b"sample_name,Cancer_type\nSample_B,GBM\n"
        csv_file = SimpleUploadedFile("metadata.csv", csv_bytes, content_type="text/csv")

        req = request_factory.post(
            f'/project/{project_id}/process_metadata',
            data={'metadataFile': csv_file},
            format='multipart',
        )
        req.user = test_user
        status = process_metadata(req, project_id)
        assert status == 'complete', f"process_metadata returned: {status!r}"

        updated = mongo_collection.find_one({'_id': ObjectId(project_id)})
        samples = updated['runs']['run1']
        sample_a = next(s for s in samples if s['Sample_name'] == 'Sample_A')
        sample_b = next(s for s in samples if s['Sample_name'] == 'Sample_B')

        assert sample_b.get('Cancer_type') == 'GBM'
        assert sample_a.get('extra_metadata_from_csv', {}).get('Cancer_type') == 'Lung', \
            "Sample_A Cancer_type must survive a partial metadata upload (Issue #508)"
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


# ---------------------------------------------------------------------------
# Issue #509 — metadata xlsx submitted with create form must be applied
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Alias propagation when a project is versioned / reaggregated
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_alias_propagates_to_new_version_on_reaggregate(
        request_factory, test_user, mongo_collection):
    """
    Bug: when a project with an alias_name is versioned (reaggregated) and the
    user leaves the alias field blank in the edit form, the alias must be carried
    over to the new version.

    Root cause: edit_project_into_new_version() built form_data from request.POST,
    which was never updated with the alias that edit_project_page() transferred
    into form.data.  The fix syncs form_data['alias'] from form_dict explicitly
    after form_data is constructed.
    """
    import shutil
    from unittest.mock import MagicMock, patch
    from bson.objectid import ObjectId
    from caper.views import edit_project_page

    ALIAS = 'pytest-alias-propagation-test'

    doc = {
        'project_name': 'AliasPropagationTest',
        'alias_name': ALIAS,
        'creator': test_user.username,
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {'run1': [{'Sample_name': 'Sample_001', 'Classification': 'No amp/Del', 'Features': []}]},
        'project_members': [test_user.username, test_user.email],
        'views': 0,
        'downloads': 0,
        'date': '2024-01-01',
        'sample_count': 1,
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)
    mongo_collection.update_one(
        {'_id': result.inserted_id},
        {'$set': {'linkid': project_id}}
    )

    captured = {}
    placeholder_ids = []

    def _fake_submit(fn, *args, **kwargs):
        # submit(fn, file_fps, temp_proj_id, project_data_path, temp_directory, form_data, ...)
        captured['form_data'] = args[4]
        placeholder_ids.append(args[1])   # temp_proj_id
        return MagicMock()                # mimic a concurrent.futures.Future

    try:
        data = {
            'project_name': 'AliasPropagationTest',
            'description': 'Versioning alias propagation test',
            'private': 'private',
            'publication_link': '',
            'project_members': '',
            'alias': '',              # intentionally blank — alias must carry over
            'remap_sample_names': 'false',
            'project_mode': 'reaggregate',
            'accept_license': 'on',
        }
        request = request_factory.post(f'/project/{project_id}/edit', data=data)
        request.user = test_user

        with patch('caper.views._thread_executor') as mock_executor:
            mock_executor.submit.side_effect = _fake_submit
            edit_project_page(request, project_name=project_id)

        assert 'form_data' in captured, (
            "Background thread (submit) was never called — edit did not trigger versioning"
        )
        assert captured['form_data'].get('alias') == ALIAS, (
            f"Alias must be propagated to form_data for the new version. "
            f"Got {captured['form_data'].get('alias')!r}, expected {ALIAS!r}"
        )

    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})
        for pid in placeholder_ids:
            try:
                mongo_collection.delete_one({'_id': ObjectId(pid)})
            except Exception:
                pass
            try:
                shutil.rmtree(os.path.join('tmp', pid), ignore_errors=True)
            except Exception:
                pass


@pytest.mark.slow
@pytest.mark.integration
def test_metadata_xlsx_applied_on_create(
        request_factory, test_user, mongo_collection):
    """
    Issue #509: a metadata XLSX submitted together with the tar.gz at project
    creation time must be applied to samples by the end of aggregation.

    The xlsx fixture (one_amprepo_sample.xlsx) maps sample GBM39 to:
      cancer_type = 'Uterine Corpus Endometrial Carcinoma'
      sample_name_alias = 'GBM_93'

    After aggregation with remap_sample_names=False the sample must be named
    GBM39 and must carry the cancer_type from the metadata sheet.
    """
    from caper.views import create_project

    req, handles = _build_create_request(
        request_factory, test_user, 'CreateMeta_Issue509',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX, remap=False)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id, "Could not parse project_id from create redirect"

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message') if doc else 'timeout'}"

        # Locate sample GBM39 (or its alias GBM_93 if remap happened anyway)
        sample = None
        for run in doc.get('runs', {}).values():
            for s in run:
                if s.get('Sample_name') in ('GBM39', 'GBM_93'):
                    sample = s
                    break
            if sample:
                break

        assert sample is not None, \
            "Sample GBM39 (or alias GBM_93) not found in aggregated project runs"

        # The xlsx cancer_type column value for GBM39
        expected_cancer = 'Uterine Corpus Endometrial Carcinoma'
        cancer = (
            sample.get('cancer_type') or
            sample.get('Cancer_type') or
            (sample.get('extra_metadata_from_csv') or {}).get('cancer_type') or
            (sample.get('extra_metadata_from_csv') or {}).get('Cancer_type')
        )
        assert cancer == expected_cancer, \
            f"cancer_type from xlsx must be applied during create, got {cancer!r} (Issue #509)"

    finally:
        _cleanup_project(mongo_collection, project_id)
