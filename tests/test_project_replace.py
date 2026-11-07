"""
upload_api/replace/<id>/: a site admin replacing a project's results by API token.

The route exists for amprepo_reclassify. What matters most: only admins get through, a
refused or dry-run request changes nothing, a project that moved on is refused, everything
but the results carries forward, and the dry run's prediction matches what gets stored.

The slow tests run a real aggregation and aren't in CI's selection.
"""

import io
import json
import os
import tarfile
import time
import uuid
from unittest.mock import patch

import pytest
from bson.objectid import ObjectId
from rest_framework.test import APIRequestFactory

from conftest import (DATASET_SMALL_TAR, DATASET_SMALL_XLSX, POLL_TIMEOUT, _cleanup_project,
                      _poll_until_finished)
from test_version_file_sharing import _build_tar, versions  # noqa: F401 (a fixture)

from caper import project_replace, utils
from caper.project_status import LIVE, SUPERSEDED, classify, status_flags
from caper.shutdown_guard import _API_UPLOAD_RE

pytestmark = pytest.mark.integration


def _user(is_staff):
    from django.contrib.auth.models import User
    from rest_framework.authtoken.models import Token

    name = f'pytest_replace_{uuid.uuid4().hex[:8]}'
    user = User.objects.create_user(username=name, email=f'{name}@example.com', is_staff=is_staff)
    return user, Token.objects.create(user=user).key


@pytest.fixture
def admin():
    user, token = _user(True)
    yield user, token
    user.delete()


@pytest.fixture
def member():
    """Not an admin, though a member of the project."""
    user, token = _user(False)
    yield user, token
    user.delete()


@pytest.fixture
def project(member):
    """A finished project with nothing stored in GridFS."""
    doc = {'project_name': f'pytest replace {uuid.uuid4().hex[:8]}', 'description': 'keep me',
           'publication_link': 'PMID:1', 'private': 'public', 'alias_name': None,
           'project_members': [member[0].username], 'date': '2026-11-06', 'runs': {},
           'FINISHED?': True, 'sample_count': 0, 'views': 7, 'downloads': 3,
           'subscribers': ['someone@example.com'], 'featured': True,
           'previous_versions': [], **status_flags(LIVE)}
    collection = utils.collection_handle_primary
    new_id = collection.insert_one(doc).inserted_id
    yield collection.find_one({'_id': new_id})
    collection.delete_one({'_id': new_id})


def _archive(name='upload.tar.gz'):
    payload = io.BytesIO(b'not looked at before the checks')
    payload.name = name
    return payload


def _post(project_id, token=None, **fields):
    from caper.views_apis import ProjectReplaceView

    headers = {'HTTP_AUTHORIZATION': f'Token {token}'} if token else {}
    request = APIRequestFactory().post(f'/upload_api/replace/{project_id}/', data=fields,
                                       format='multipart', **headers)
    return ProjectReplaceView.as_view()(request, project_id=project_id)


def _status(job_id, token):
    from caper.views_apis import ProjectReplaceStatusView

    request = APIRequestFactory().post(f'/upload_api/replace-status/{job_id}/',
                                       HTTP_AUTHORIZATION=f'Token {token}')
    return ProjectReplaceStatusView.as_view()(request, job_id=job_id)


def _prepare(version_id, token=None):
    from caper.views_apis import ProjectReplacePrepareView

    headers = {'HTTP_AUTHORIZATION': f'Token {token}'} if token else {}
    request = APIRequestFactory().post(f'/upload_api/replace-prepare/{version_id}/', **headers)
    return ProjectReplacePrepareView.as_view()(request, version_id=version_id)


def _state(doc):
    """What a refused request must leave alone."""
    return utils.collection_handle_primary.find_one({'_id': doc['_id']})


# ---- Who may ----

def test_no_token_is_refused(project):
    assert _post(str(project['_id']), dry_run='true').status_code == 401


def test_a_bad_token_is_refused(project):
    assert _post(str(project['_id']), token='nope', dry_run='true').status_code == 401


def test_a_token_is_not_enough_without_being_an_admin(project, member):
    """Anyone with an account can make a token, and this one's owner is even a project member."""
    response = _post(str(project['_id']), token=member[1], dry_run='false', document=_archive(),
                     expected_latest_id=str(project['_id']))
    assert response.status_code == 403
    assert response.data['code'] == 'admin_required'
    assert _state(project) == project


def test_a_browser_session_does_not_count(project, admin):
    """An admin's cookie, no token. Without this a logged-in admin could be made to POST it."""
    from caper.views_apis import ProjectReplaceView

    request = APIRequestFactory().post(f'/upload_api/replace/{project["_id"]}/',
                                       data={'dry_run': 'false'}, format='multipart')
    request.user = admin[0]
    assert ProjectReplaceView.as_view()(request, project_id=str(project['_id'])).status_code == 401


def test_status_needs_an_admin_too(member):
    assert _status(str(ObjectId()), member[1]).status_code == 403


# ---- What gets refused, changing nothing ----

@pytest.mark.parametrize('fields, code', [
    ({}, 'invalid_dry_run'),
    ({'dry_run': 'flase'}, 'invalid_dry_run'),
    ({'dry_run': 'false'}, 'no_archive'),
])
def test_a_malformed_request_writes_nothing(project, admin, fields, code):
    """dry_run in particular: a mistyped one must not fall through to a real replacement."""
    response = _post(str(project['_id']), token=admin[1], expected_latest_id=str(project['_id']), **fields)
    assert response.status_code == 400
    assert response.data['code'] == code
    assert _state(project) == project


def test_expected_latest_id_is_required(project, admin):
    response = _post(str(project['_id']), token=admin[1], dry_run='false', document=_archive())
    assert response.data['code'] == 'no_expected_id'


def test_a_project_that_moved_on_is_refused(project, admin):
    response = _post(str(project['_id']), token=admin[1], dry_run='false', document=_archive(),
                     expected_latest_id=str(ObjectId()))
    assert response.status_code == 409
    assert response.data['code'] == 'stale_version'
    assert response.data['latest_id'] == str(project['_id'])
    assert _state(project) == project


def test_a_version_still_processing_is_refused(project, admin):
    utils.collection_handle_primary.update_one({'_id': project['_id']}, {'$set': {'FINISHED?': False}})
    response = _post(str(project['_id']), token=admin[1], dry_run='false', document=_archive(),
                     expected_latest_id=str(project['_id']))
    assert response.status_code == 409
    assert response.data['code'] == 'in_progress'


def test_an_unknown_project_is_404(admin):
    response = _post(str(ObjectId()), token=admin[1], dry_run='true', document=_archive(),
                     expected_latest_id=str(ObjectId()))
    assert response.status_code == 404


def test_shutdown_mode_covers_the_route_but_not_its_status():
    assert _API_UPLOAD_RE.match('/upload_api/replace/abc/')
    assert _API_UPLOAD_RE.match('/upload_api/')
    # A deploy waits for these jobs, so a client must still be able to see them end.
    assert not _API_UPLOAD_RE.match('/upload_api/replace-status/abc/')


# ---- The dry run ----

def _run_inline(monkeypatch):
    from caper import background_tasks
    monkeypatch.setattr(background_tasks._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: fn(*a, **kw))


def _upload(path):
    return open(path, 'rb')


def _counts():
    return (utils.collection_handle_primary.count_documents({}),
            utils.db_handle_primary['fs.files'].count_documents({}),
            utils.db_handle_primary['fs.chunks'].count_documents({}))


def _cleanup_job(job_id):
    import shutil
    shutil.rmtree(os.path.join('tmp', job_id), ignore_errors=True)


def test_the_prediction_matches_what_gets_stored(versions, tmp_path):  # noqa: F811
    first = versions()
    report = project_replace.predict(first, _build_tar(tmp_path, changed=('S2',)), None,
                                     str(tmp_path / 'unpacked'))
    second = versions(changed=('S2',))
    assert report['files']['new'] > 0 and report['files']['shared'] > 0
    assert report['samples'] == 2 and report['features'] == 4
    stored = project_replace._stored_files(second)
    for key in ('shared', 'shared_bytes', 'new', 'new_bytes'):
        assert report['files'][key] == stored[key], key


def test_an_identical_archive_is_predicted_to_share_everything(versions, tmp_path):  # noqa: F811
    report = project_replace.predict(versions(), _build_tar(tmp_path), None, str(tmp_path / 'unpacked'))
    assert report['files']['new'] == 0 and report['files']['new_bytes'] == 0
    assert report['files']['missing'] == 0


def test_a_missing_file_is_reported(versions, tmp_path):  # noqa: F811
    first = versions()
    source = tmp_path / 'src' / 'results'
    archive = _build_tar(tmp_path)
    (source / 'S1' / 'cnv.bed').unlink()
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname='results')
    report = project_replace.predict(first, archive, None, str(tmp_path / 'unpacked'))
    assert report['files']['missing'] == 1
    assert 'S1' in report['files']['missing_examples'][0]


def test_a_dry_run_through_the_route_writes_nothing(versions, tmp_path, admin, monkeypatch):  # noqa: F811
    first = versions()
    _run_inline(monkeypatch)
    before = _counts()
    head_before = _state(first)
    with _upload(_build_tar(tmp_path)) as archive:
        response = _post(str(first['_id']), token=admin[1], dry_run='true', document=archive,
                         expected_latest_id=str(first['_id']))
    assert response.status_code == 202, response.data
    job_id = response.data['job_id']
    try:
        assert _counts() == before
        assert _state(first) == head_before
        result = _status(job_id, admin[1]).data
        assert result['state'] == 'finished'
        assert result['dry_run'] is True
        assert result['files']['new'] == 0
        assert result['record']['fits'] is True
        assert result['current']['id'] == str(first['_id'])
        # The upload is gone, and only the result stays until the cleanup takes it.
        assert os.listdir(os.path.join('tmp', job_id)) == ['dry_run.json']
    finally:
        _cleanup_job(job_id)


def test_a_dry_run_on_an_archive_without_run_json_fails_plainly(versions, tmp_path, admin, monkeypatch):  # noqa: F811
    first = versions()
    _run_inline(monkeypatch)
    source = tmp_path / 'readme.txt'
    source.write_text('not a result')
    archive = tmp_path / 'broken.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname='results/readme.txt')
    with _upload(archive) as handle:
        response = _post(str(first['_id']), token=admin[1], dry_run='true', document=handle,
                         expected_latest_id=str(first['_id']))
    job_id = response.data['job_id']
    try:
        result = _status(job_id, admin[1]).data
        assert result['state'] == 'failed'
        assert 'run.json' in result['error']
    finally:
        _cleanup_job(job_id)


def test_a_bad_job_id_is_404_not_a_path(admin):
    assert _status('../../etc', admin[1]).status_code == 404
    assert _status(str(ObjectId()), admin[1]).status_code == 404


# ---- What a real replacement hands to the form's code ----

def test_a_replacement_is_the_forms_replace(project, admin, tmp_path, monkeypatch):
    """Everything but the results carries forward."""
    from caper import views

    submitted = {}
    monkeypatch.setattr(views._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: submitted.update(args=a, kw=kw))
    monkeypatch.setattr(views, 'project_removed', lambda *a, **kw: None)
    monkeypatch.setattr(views, 'project_content_changed', lambda project_id: None)

    (tmp_path / 'a.tar.gz').write_bytes(b'archive')
    with _upload(tmp_path / 'a.tar.gz') as archive:
        response = _post(str(project['_id']), token=admin[1], dry_run='false', document=archive,
                         expected_latest_id=str(project['_id']))
    assert response.status_code == 202, response.data
    new_id = response.data['job_id']
    assert response.data['replaces'] == str(project['_id'])
    try:
        args = submitted['args']
        form_data, user, old_subscribers = args[4], args[5], args[9]
        assert user == admin[0]
        assert form_data['project_name'] == project['project_name']
        assert form_data['description'] == 'keep me'
        assert form_data['publication_link'] == 'PMID:1'
        assert form_data['private'] == 'public'
        assert form_data['project_members'] == project['project_members'][0]
        assert 'AC_version' not in form_data, 'read from the new data, not carried over'
        assert args[8] == [7, 3], 'views and downloads'
        assert old_subscribers == ['someone@example.com']
        assert args[13] is True, 'replace, so the old archive is not aggregated in'
        assert submitted['kw']['oldFeatured'] is True
        assert submitted['kw']['rollback_project_id'] == str(project['_id'])
        assert submitted['kw']['keep_members'] == project['project_members']

        placeholder = _state({'_id': ObjectId(new_id)})
        assert classify(placeholder) == LIVE
        assert classify(_state(project)) == SUPERSEDED
    finally:
        utils.collection_handle_primary.delete_one({'_id': ObjectId(new_id)})
        _cleanup_job(new_id)


def test_status_follows_the_placeholder(project, admin):
    collection = utils.collection_handle_primary
    collection.update_one({'_id': project['_id']}, {'$set': {'FINISHED?': False}})
    assert _status(str(project['_id']), admin[1]).data['state'] == 'running'

    collection.update_one({'_id': project['_id']}, {'$set': {
        'FINISHED?': True, 'aggregation_failed': True, 'error_message': 'it broke',
        'rollback_project_id': 'abc'}})
    failed = _status(str(project['_id']), admin[1]).data
    assert failed['state'] == 'failed'
    assert failed['error'] == 'it broke'
    assert failed['restored_project_id'] == 'abc'


# ---- Getting a new version's page ready ----

def test_prepare_needs_an_admin(project, member):
    assert _prepare(str(project['_id'])).status_code == 401
    assert _prepare(str(project['_id']), member[1]).status_code == 403
    assert _state(project) == project


@pytest.mark.parametrize('version_id', ['not-an-id', str(ObjectId())])
def test_prepare_on_no_such_version_is_404(admin, version_id):
    assert _prepare(version_id, admin[1]).status_code == 404


@pytest.mark.parametrize('change', [{'FINISHED?': False}, {'aggregation_failed': True}])
def test_prepare_refuses_a_version_that_has_not_finished(project, admin, change):
    utils.collection_handle_primary.update_one({'_id': project['_id']}, {'$set': change})
    response = _prepare(str(project['_id']), admin[1])
    assert response.status_code == 409
    assert response.data['code'] == 'not_finished'
    assert 'metadata_stored' not in _state(project)


def test_prepare_does_nothing_twice(project, admin, monkeypatch):
    from caper import background_tasks

    def must_not_run(*args, **kwargs):
        raise AssertionError('the fields were already stored')

    monkeypatch.setattr(background_tasks._thread_executor, 'submit', must_not_run)
    utils.collection_handle_primary.update_one({'_id': project['_id']}, {'$set': {'metadata_stored': 'Yes'}})
    response = _prepare(str(project['_id']), admin[1])
    assert response.status_code == 202
    assert response.data == {'page_ready': True}


def test_page_fields_read_the_archive_from_the_primary(project, monkeypatch):
    """A replica can lack an archive written moments ago, and a failed read is stored for good."""
    import caper.views

    seen = {}
    monkeypatch.setattr(caper.views, 'store_page_fields',
                        lambda doc, fs=None: seen.update(fs=fs, id=doc['_id']))
    monkeypatch.setattr(project_replace.gridfs, 'GridFS', lambda database: database)
    project_replace._store_page_fields(project['_id'])
    assert seen['id'] == project['_id']
    assert seen['fs'] is utils.db_handle_primary


# ---- For real, with the aggregator ----

def _wait_for_job(job_id, token):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        result = _status(job_id, token).data
        if result['state'] != 'running':
            return result
        time.sleep(2)
    raise AssertionError(f'job {job_id} did not finish')


def _aggregated(source, where):
    from AmpliconSuiteAggregator import Aggregator

    aggregated = Aggregator(input_paths=[source], project_name='pytest_replace', work_dir=str(where))
    assert aggregated.completed
    return aggregated.aggregated_filename


@pytest.fixture
def real_project(request_factory, admin):
    """A project made through the form, then given what must survive a replacement."""
    from conftest import _build_create_request
    from caper.views import create_project
    from conftest import _project_id_from_redirect

    collection = utils.collection_handle_primary
    request, handles = _build_create_request(request_factory, admin[0], f'pytest replace {uuid.uuid4().hex[:8]}',
                                             tar_path=DATASET_SMALL_TAR)
    try:
        response = create_project(request)
    finally:
        for handle in handles:
            handle.close()
    project_id = _project_id_from_redirect(response)
    assert _poll_until_finished(collection, project_id)
    alias = f'pytest_alias_{uuid.uuid4().hex[:8]}'
    collection.update_one({'_id': ObjectId(project_id)}, {'$set': {
        'alias_name': alias, 'description': 'keep me', 'publication_link': 'PMID:1',
        'subscribers': ['someone@example.com'], 'views': 7, 'downloads': 3, 'featured': True,
        'creator': 'the_real_creator', 'project_members': ['the_real_creator']}})
    made = [project_id]
    yield collection.find_one({'_id': ObjectId(project_id)}), made
    for each in reversed(made):
        _cleanup_project(collection, each)


@pytest.mark.slow
def test_a_real_replacement_matches_its_dry_run(real_project, admin, tmp_path):
    collection = utils.collection_handle_primary
    old, made = real_project
    old_id = str(old['_id'])
    archive_path = _aggregated(DATASET_SMALL_TAR, tmp_path / 'agg')

    with patch('caper.user_preferences.notify_subscribers_of_project_update'):
        with _upload(archive_path) as archive:
            dry = _post(old_id, token=admin[1], dry_run='true', document=archive, expected_latest_id=old_id)
        assert dry.status_code == 202, dry.data
        prediction = _wait_for_job(dry.data['job_id'], admin[1])
        _cleanup_job(dry.data['job_id'])
        assert prediction['state'] == 'finished', prediction
        assert prediction['files']['new'] == 0, 'the same data again should store no new file'

        with _upload(archive_path) as archive:
            real = _post(old_id, token=admin[1], dry_run='false', document=archive, expected_latest_id=old_id)
        assert real.status_code == 202, real.data
        new_id = real.data['job_id']
        made.append(new_id)
        result = _wait_for_job(new_id, admin[1])

    assert result['state'] == 'finished', result
    assert result['page_ready'] is False

    # What the first page view would have stored, without a page view.
    new_id = result['project_id']
    assert _prepare(new_id, admin[1]).data == {'page_ready': False}
    deadline = time.time() + POLL_TIMEOUT
    while not _status(new_id, admin[1]).data['page_ready'] and time.time() < deadline:
        time.sleep(1)
    prepared = collection.find_one({'_id': ObjectId(new_id)})
    assert prepared['metadata_stored'] == 'Yes'
    assert prepared['reference_genome'] and prepared['sample_data']
    assert isinstance(prepared['ecDNA_context'], dict)
    assert prepared['views'] == 0, 'preparing a page is not a visit'
    for key in ('shared', 'new'):
        assert result['files'][key] == prediction['files'][key], key
    # Both near the upload's size, and always written: it holds the version's id.
    assert abs(result['files']['archive_bytes'] - prediction['files']['archive_bytes']) < 1000
    assert result['samples'] == prediction['samples']
    assert result['features'] == prediction['features']

    new = collection.find_one({'_id': ObjectId(new_id)})
    assert classify(new) == LIVE and classify(collection.find_one({'_id': old['_id']})) == SUPERSEDED
    # The admin ran this version, so is its creator, but doesn't join the project.
    assert new['creator'] == admin[0].username
    assert new['project_members'] == ['the_real_creator']
    assert collection.find_one({'_id': old['_id']})['creator'] == 'the_real_creator'
    assert new['featured'] is True
    assert new['project_name'] == old['project_name']
    assert new['alias_name'] == old['alias_name']
    assert new['description'] == 'keep me' and new['publication_link'] == 'PMID:1'
    assert new['subscribers'] == ['someone@example.com']
    assert new['previous_versions'][-1]['linkid'] == old_id

    # A second replacement from the old id now finds it has moved on.
    with _upload(archive_path) as archive:
        stale = _post(old_id, token=admin[1], dry_run='false', document=archive, expected_latest_id=old_id)
    assert stale.status_code == 409
    assert stale.data['latest_id'] == new_id


@pytest.fixture
def no_subscriber_mail():
    """Held until the background version finishes, which is when subscribers are told."""
    with patch('caper.user_preferences.notify_subscribers_of_project_update'):
        yield


def _form_edit(request_factory, user, project, tar_path=None, **fields):
    """The edit page's POST, as a browser sends it, and the new version's id."""
    from django.contrib.messages.storage.fallback import FallbackStorage

    from conftest import _project_id_from_redirect
    from caper.views import edit_project_page

    project_id = str(project['_id'])
    data = {'project_name': project['project_name'], 'description': project['description'],
            'private': project['private'], 'publication_link': project['publication_link'],
            'project_members': ','.join(project['project_members']), 'alias': '',
            'remap_sample_names': 'false', 'accept_license': 'on', **fields}
    handle = open(tar_path, 'rb') if tar_path else None
    try:
        if handle:
            data.update(document=handle, project_mode='replace')
        request = request_factory.post(f'/project/{project_id}/edit', data=data, format='multipart')
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        response = edit_project_page(request, project_name=project_id)
    finally:
        if handle:
            handle.close()
    return _project_id_from_redirect(response)


@pytest.mark.slow
def test_an_admin_uploading_through_the_form_stays_outside_the_project(
        real_project, admin, request_factory, no_subscriber_mail):
    collection = utils.collection_handle_primary
    old, made = real_project

    new_id = _form_edit(request_factory, admin[0], old, tar_path=DATASET_SMALL_TAR,
                        project_members='the_real_creator,added_by_the_admin')
    made.append(new_id)
    done = _poll_until_finished(collection, new_id)
    assert done and not done.get('aggregation_failed'), done and done.get('error_message')

    assert done['creator'] == admin[0].username
    # The box on the form still decides who else is on the project.
    assert sorted(done['project_members']) == ['added_by_the_admin', 'the_real_creator']


@pytest.mark.slow
def test_an_admin_emptying_a_project_through_the_form_stays_outside_it(
        real_project, admin, request_factory, no_subscriber_mail):
    collection = utils.collection_handle_primary
    old, made = real_project
    samples = sorted({feature['Sample_name'] for features in old['runs'].values() for feature in features})

    new_id = _form_edit(request_factory, admin[0], old, samples_to_remove=samples)
    made.append(new_id)
    done = _poll_until_finished(collection, new_id)
    assert done and done.get('EMPTY?') and not done.get('aggregation_failed'), done

    assert done['creator'] == admin[0].username
    assert done['project_members'] == ['the_real_creator']


@pytest.mark.slow
def test_a_member_uploading_through_the_form_stays_a_member(
        real_project, member, request_factory, no_subscriber_mail):
    """The rule for admins from outside changes nothing for the project's own people."""
    collection = utils.collection_handle_primary
    old, made = real_project
    collection.update_one({'_id': old['_id']}, {'$set': {
        'project_members': ['the_real_creator', member[0].username]}})
    old = collection.find_one({'_id': old['_id']})

    new_id = _form_edit(request_factory, member[0], old, tar_path=DATASET_SMALL_TAR)
    made.append(new_id)
    done = _poll_until_finished(collection, new_id)
    assert done and not done.get('aggregation_failed'), done and done.get('error_message')

    assert done['creator'] == member[0].username
    assert sorted(done['project_members']) == sorted(['the_real_creator', member[0].username])


@pytest.mark.slow
def test_a_failed_replacement_leaves_the_project_as_it_was(real_project, admin, tmp_path):
    """Including its alias, which is how the projects this tool updates are often linked."""
    collection = utils.collection_handle_primary
    old, made = real_project
    old_id = str(old['_id'])

    note = tmp_path / 'readme.txt'
    note.write_text('not an AmpliconSuite result')
    broken = tmp_path / 'broken.tar.gz'
    with tarfile.open(broken, 'w:gz') as tar:
        tar.add(note, arcname='results/readme.txt')

    with _upload(broken) as archive:
        response = _post(old_id, token=admin[1], dry_run='false', document=archive, expected_latest_id=old_id)
    assert response.status_code == 202, response.data
    new_id = response.data['job_id']
    made.append(new_id)
    result = _wait_for_job(new_id, admin[1])

    assert result['state'] == 'failed', result
    assert result['restored_project_id'] == old_id
    restored = collection.find_one({'_id': old['_id']})
    assert classify(restored) == LIVE
    assert restored['alias_name'] == old['alias_name']
    assert restored['description'] == 'keep me'


def _project_with_sheet(request_factory, admin, xlsx_path):
    from conftest import _build_create_request, _project_id_from_redirect
    from caper.views import create_project

    request, handles = _build_create_request(request_factory, admin[0], f'pytest replace {uuid.uuid4().hex[:8]}',
                                             tar_path=DATASET_SMALL_TAR, xlsx_path=xlsx_path)
    try:
        response = create_project(request)
    finally:
        for handle in handles:
            handle.close()
    project_id = _project_id_from_redirect(response)
    assert _poll_until_finished(utils.collection_handle_primary, project_id)
    return project_id


def _metadata(project_id):
    from caper.extra_metadata import get_extra_metadata_from_project

    return get_extra_metadata_from_project(
        utils.collection_handle_primary.find_one({'_id': ObjectId(project_id)}))


@pytest.mark.slow
def test_a_replacement_keeps_the_projects_metadata(request_factory, admin, tmp_path):
    """No sheet sent, so what the project already holds stays, as the form's retain box does."""
    made = [_project_with_sheet(request_factory, admin, DATASET_SMALL_XLSX)]
    try:
        before = _metadata(made[0])
        assert before, 'the sheet should have given the project some metadata'
        archive_path = _aggregated(DATASET_SMALL_TAR, tmp_path / 'agg')

        with _upload(archive_path) as archive:
            dry = _post(made[0], token=admin[1], dry_run='true', document=archive, expected_latest_id=made[0])
        prediction = _wait_for_job(dry.data['job_id'], admin[1])
        _cleanup_job(dry.data['job_id'])
        assert prediction['metadata'] == 'retained'

        with _upload(archive_path) as archive:
            real = _post(made[0], token=admin[1], dry_run='false', document=archive, expected_latest_id=made[0])
        made.append(real.data['job_id'])
        assert _wait_for_job(made[1], admin[1])['state'] == 'finished'
        assert _metadata(made[1]) == before
    finally:
        for each in reversed(made):
            _cleanup_project(utils.collection_handle_primary, each)


@pytest.mark.slow
def test_a_sheet_sent_with_a_replacement_is_applied(request_factory, admin, tmp_path):
    """How Hartwig's richer sheet goes up with its AC replacement."""
    made = [_project_with_sheet(request_factory, admin, None)]
    try:
        assert not _metadata(made[0])
        archive_path = _aggregated(DATASET_SMALL_TAR, tmp_path / 'agg')

        with _upload(archive_path) as archive, open(DATASET_SMALL_XLSX, 'rb') as sheet:
            dry = _post(made[0], token=admin[1], dry_run='true', document=archive, metadataFile=sheet,
                        expected_latest_id=made[0])
        prediction = _wait_for_job(dry.data['job_id'], admin[1])
        _cleanup_job(dry.data['job_id'])
        assert prediction['state'] == 'finished', prediction
        assert prediction['metadata'] == 'new sheet'

        with _upload(archive_path) as archive, open(DATASET_SMALL_XLSX, 'rb') as sheet:
            real = _post(made[0], token=admin[1], dry_run='false', document=archive, metadataFile=sheet,
                         expected_latest_id=made[0])
        made.append(real.data['job_id'])
        assert _wait_for_job(made[1], admin[1])['state'] == 'finished'
        assert _metadata(made[1])
        # The dry run counts the sheet in the record size, which is what Hartwig needs from it.
        assert prediction['record']['bytes'] > 0
    finally:
        for each in reversed(made):
            _cleanup_project(utils.collection_handle_primary, each)
