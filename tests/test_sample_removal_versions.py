"""
Removing a sample makes a new version of the project.

The edit form has always said so -- the note under the checkboxes reads
"Removing samples will create a new version of the project" -- but the code did
not: samples_to_remove was missing from the needs_new_version test, so a removal
went through edit_project_without_reversioning and edited the document in place.
The sample left the runs, it left the archive on the next download, and there
was nothing to go back to.

Deleting data is the operation that most needs an undo, and the project's undo
is its version history, so the removal now takes the same road as the other
three edits that reshape a project's data.

The fast tests here pin the routing decision without running the aggregator.
The slow one at the bottom does the whole thing against real data.
"""

import os
import shutil
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
    POLL_TIMEOUT,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Which road the edit takes
# ---------------------------------------------------------------------------

@pytest.fixture
def two_sample_project(mongo_collection, test_user):
    """A finished project with two samples, cleaned up afterwards."""
    doc = {
        'project_name': f'SampleRemovalTest_{uuid.uuid4().hex[:8]}',
        'description': 'Sample removal versioning test',
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {
            'Sample_001': [{'Sample_name': 'Sample_001', 'Classification': 'ecDNA',
                            'Oncogenes': [], 'Features': []}],
            'Sample_002': [{'Sample_name': 'Sample_002', 'Classification': 'ecDNA',
                            'Oncogenes': [], 'Features': []}],
        },
        'sample_data': [{'Sample_name': 'Sample_001'}, {'Sample_name': 'Sample_002'}],
        'project_members': [test_user.username, test_user.email],
        'views': 0,
        'downloads': 0,
        'date': '2024-01-01',
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})
    try:
        yield project_id
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


def _sample_names(doc):
    """The sample names in a project, which are not the keys of runs.

    run.json is keyed by run -- 'sample_1', 'sample_2' -- and the name people
    see, and the name the removal form submits, is on the features inside.
    """
    return sorted({feature['Sample_name']
                   for features in doc['runs'].values()
                   for feature in features})


def _edit_post(request_factory, test_user, project_id, project_name, **extra):
    data = {
        'project_name': project_name,
        'description': 'Sample removal versioning test',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'false',
        'project_mode': 'append',
        'accept_license': 'on',
    }
    data.update(extra)
    request = request_factory.post(f'/project/{project_id}/edit', data=data)
    request.user = test_user
    return request


def _submitted_edit(request, mongo_collection):
    """Run edit_project_page with the background thread stubbed out.

    Returns (response, captured args of the submit call or None), and removes
    any placeholder project the view inserted.
    """
    from caper.views import edit_project_page

    captured = {}
    placeholder_ids = []

    def _fake_submit(fn, *args, **kwargs):
        captured['args'] = args
        placeholder_ids.append(args[1])
        return MagicMock()

    try:
        with patch('caper.views._thread_executor') as mock_executor:
            mock_executor.submit.side_effect = _fake_submit
            project_id = request.path.split('/')[2]
            response = edit_project_page(request, project_name=project_id)
        return response, captured.get('args')
    finally:
        for pid in placeholder_ids:
            mongo_collection.delete_one({'_id': ObjectId(pid)})
            shutil.rmtree(os.path.join('tmp', pid), ignore_errors=True)


def test_removing_a_sample_starts_a_new_version(
        request_factory, test_user, mongo_collection, two_sample_project):
    project = mongo_collection.find_one({'_id': ObjectId(two_sample_project)})
    request = _edit_post(request_factory, test_user, two_sample_project,
                         project['project_name'], samples_to_remove=['Sample_002'])

    response, args = _submitted_edit(request, mongo_collection)

    assert args is not None, (
        'No aggregation was started: the removal edited the project in place '
        'instead of versioning it'
    )
    # args: (file_fps, temp_proj_id, project_data_path, temp_directory, form_data,
    #        user, extra_metadata_fp, previous_versions, [views, downloads],
    #        subscribers, project, download_url, samples_to_remove, ...)
    assert args[12] == ['Sample_002'], \
        f'The samples to remove did not reach the aggregation: {args[12]!r}'
    assert args[13] is False, \
        'The old archive must be downloaded and stripped, not replaced'
    assert two_sample_project in [entry['linkid'] for entry in args[7]], \
        'The version being edited was not added to the version history'
    assert response.status_code in (301, 302)
    assert _project_id_from_redirect(response) != two_sample_project, \
        'The edit redirected back to the old version rather than the new one'


def test_the_version_being_edited_stays_reachable(
        request_factory, test_user, mongo_collection, two_sample_project):
    """A new version is only an undo if the old one is still there."""
    project = mongo_collection.find_one({'_id': ObjectId(two_sample_project)})
    request = _edit_post(request_factory, test_user, two_sample_project,
                         project['project_name'], samples_to_remove=['Sample_002'])

    _submitted_edit(request, mongo_collection)

    old = mongo_collection.find_one({'_id': ObjectId(two_sample_project)})
    assert old is not None, 'The edited version was deleted outright'
    assert len(old['runs']) == 2, \
        'The samples were removed from the old version as well as the new one'


def test_an_edit_that_removes_nothing_still_does_not_version(
        request_factory, test_user, mongo_collection, two_sample_project):
    """Renaming a project or fixing its description must stay cheap: those
    edits neither reshape the data nor cost a run of the aggregator."""
    project = mongo_collection.find_one({'_id': ObjectId(two_sample_project)})
    request = _edit_post(request_factory, test_user, two_sample_project,
                         project['project_name'],
                         description='Edited description, same samples')

    response, args = _submitted_edit(request, mongo_collection)

    assert args is None, 'A description edit started a new version'
    assert _project_id_from_redirect(response) == two_sample_project

    updated = mongo_collection.find_one({'_id': ObjectId(two_sample_project)})
    assert updated['description'] == 'Edited description, same samples'
    assert updated['previous_versions'] == []


# ---------------------------------------------------------------------------
# End to end, against a real archive
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_removing_a_sample_end_to_end(
        request_factory, test_user, mongo_collection, monkeypatch):
    """Create a two-sample project, remove one, and check both versions.

    This is the part the routing tests cannot reach: the old archive is
    downloaded, the sample's directories are stripped out of it, and the
    aggregator runs on what is left.
    """
    from django.conf import settings
    from caper.views import create_project, edit_project_page

    if not os.path.exists(DATASET_ADDL_TAR):
        pytest.skip(f'Test dataset not found: {DATASET_ADDL_TAR}')

    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False)
    created_ids = []

    request, handles = _build_create_request(
        request_factory, test_user, 'PyTest_RemoveSample',
        tar_path=DATASET_ADDL_TAR)
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
        sample_names = _sample_names(doc)
        assert len(sample_names) > 1, \
            f'Need more than one sample to remove one, got {sample_names}'

        doomed = sample_names[0]
        survivors = sample_names[1:]

        edit_request = _edit_post(request_factory, test_user, project_id,
                                  'PyTest_RemoveSample',
                                  samples_to_remove=[doomed])
        edit_response = edit_project_page(edit_request, project_name=project_id)

        new_id = _project_id_from_redirect(edit_response)
        assert new_id and new_id != project_id, \
            f'[Edit] Removal did not create a new version (redirect: {edit_response!r})'
        created_ids.append(new_id)

        new_doc = _poll_until_finished(mongo_collection, new_id)
        assert new_doc is not None, f'[Edit] Timed out after {POLL_TIMEOUT}s'
        assert not new_doc.get('aggregation_failed'), new_doc.get('error_message')

        assert _sample_names(new_doc) == survivors, \
            f'[Edit] New version holds {_sample_names(new_doc)}, expected {survivors}'
        assert new_doc.get('sample_count') == len(survivors)
        assert project_id in [entry['linkid'] for entry in new_doc['previous_versions']], \
            '[Edit] The old version is not in the new version history'

        old_doc = mongo_collection.find_one({'_id': ObjectId(project_id)})
        assert old_doc is not None, '[Edit] The old version was deleted'
        assert _sample_names(old_doc) == sample_names, \
            '[Edit] The old version lost the sample too, so there is nothing to go back to'
    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)
