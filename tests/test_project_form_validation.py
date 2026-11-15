"""The create and edit forms are validated on the server, not only in the browser.

The license checkbox is required, and the browser enforces that on a normal
submit. Nothing behind it did:
  * the edit view never called is_valid(), so a POST without the box reached
    form.save() and raised ValueError -- a 500 -- after it had already cleared
    the project's alias when the alias field was left blank;
  * create_project answered an invalid form with a bare 404;
  * create_empty_project never looked at the box at all; its button submits
    with form.submit(), which skips the browser's required-field check.
"""
import json

import pytest
from bson.objectid import ObjectId

from conftest import _cleanup_project

pytestmark = pytest.mark.integration


def _with_messages(request):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.cache import SessionStore
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request


@pytest.fixture
def no_s3(monkeypatch):
    """The audit log looks up the archive's S3 size; nothing here has one."""
    from caper import views
    monkeypatch.setattr(views, '_get_project_s3_uri', lambda linkid: None)


@pytest.fixture
def live_project(mongo_collection, test_user):
    """One current project with an alias, which a blank alias field inherits."""
    doc_id = ObjectId()
    mongo_collection.insert_one({
        '_id': doc_id, 'linkid': str(doc_id),
        'project_name': 'FormValidationTest', 'description': 'before',
        'publication_link': '', 'date': '2026-01-01T00:00:00',
        'private': 'private', 'project_members': [test_user.username],
        'alias_name': 'form_validation_test_alias', 'creator': test_user.username,
        'delete': False, 'current': True, 'FINISHED?': True,
        'AA_version': '1.5.r1', 'AC_version': '2.0.0', 'ASP_version': '1.5.0',
        'runs': {}, 'previous_versions': [],
        'version_chain_id': ObjectId(), 'version_ordinal': 1, 'is_latest': True,
    })
    yield doc_id
    _cleanup_project(mongo_collection, str(doc_id))
    from caper.views import audit_log_handle
    audit_log_handle.delete_many({'project_uuid': str(doc_id)})


def _edit(request_factory, user, doc_id, data):
    from caper.views import edit_project_page
    request = _with_messages(request_factory.post(f'/project/{doc_id}/edit', data=data))
    request.user = user
    return edit_project_page(request, project_name=str(doc_id))


EDIT = {'project_name': 'FormValidationTest', 'description': 'after',
        'private': 'private', 'project_members': '', 'alias': '',
        'publication_link': ''}


def test_edit_without_license_is_400_and_writes_nothing(
        request_factory, test_user, mongo_collection, live_project):
    before = mongo_collection.find_one({'_id': live_project})
    response = _edit(request_factory, test_user, live_project, EDIT)
    assert response.status_code == 400
    assert 'This field is required' in response.content.decode()
    assert mongo_collection.find_one({'_id': live_project}) == before


def test_edit_with_blank_name_is_400_and_writes_nothing(
        request_factory, test_user, mongo_collection, live_project):
    before = mongo_collection.find_one({'_id': live_project})
    data = dict(EDIT, project_name='', accept_license='on')
    response = _edit(request_factory, test_user, live_project, data)
    assert response.status_code == 400
    assert mongo_collection.find_one({'_id': live_project}) == before


def test_edit_rerenders_what_was_submitted_not_the_inherited_alias(
        request_factory, test_user, live_project):
    # A blank alias field means "keep the alias"; were the alias copied into
    # the form before validation, the re-rendered page would submit it back
    # explicitly and the next save would skip the handoff.
    response = _edit(request_factory, test_user, live_project, EDIT)
    html = response.content.decode()
    assert 'value="after"' in html or '>after<' in html
    assert 'name="alias" value="form_validation_test_alias"' not in html


def test_edit_with_license_still_saves_and_keeps_the_alias(
        request_factory, test_user, mongo_collection, live_project, no_s3):
    response = _edit(request_factory, test_user, live_project,
                     dict(EDIT, accept_license='on'))
    assert response.status_code == 302
    after = mongo_collection.find_one({'_id': live_project})
    assert after['description'] == 'after'
    assert after['alias_name'] == 'form_validation_test_alias'


def test_create_without_license_is_400_and_creates_nothing(
        request_factory, test_user, mongo_collection):
    from caper.views import create_project
    name = f'FormValidationCreate-{ObjectId()}'
    request = request_factory.post('/create-project/', data={
        'project_name': name, 'description': 'd', 'private': 'private',
        'project_members': '', 'alias': '', 'publication_link': ''})
    request.user = test_user
    response = create_project(request)
    assert response.status_code == 400
    assert 'This field is required' in response.content.decode()
    assert mongo_collection.count_documents({'project_name': name}) == 0


@pytest.mark.parametrize('license_ticked', [False, True])
def test_create_empty_requires_the_license(
        request_factory, test_user, mongo_collection, license_ticked):
    from caper.views import create_empty_project
    name = f'FormValidationEmpty-{ObjectId()}'
    data = {'project_name': name, 'description': 'd', 'alias': '',
            'publication_link': '', 'project_members': ''}
    if license_ticked:
        data['accept_license'] = 'on'
    request = _with_messages(request_factory.post('/create-empty-project/', data=data))
    request.user = test_user
    try:
        response = create_empty_project(request)
        assert response.status_code == 302
        created = mongo_collection.count_documents({'project_name': name})
        assert created == (1 if license_ticked else 0)
        if not license_ticked:
            assert response.url == '/create-project/'
    finally:
        for doc in mongo_collection.find({'project_name': name}, {'_id': 1}):
            _cleanup_project(mongo_collection, str(doc['_id']))


# Issue #750: create_empty_project inserted a LIVE document and never put it in
# a version chain. predecessor_chain() refuses to extend a predecessor with no
# version_chain_id, so every version added to it later was left off the chain
# too, and validate_project_lineage.py reported each one under I1.
STATUS_AND_CHAIN_FIELDS = ('status', 'current', 'delete',
                           'version_chain_id', 'version_ordinal', 'is_latest')


def _create_empty(request_factory, user, name):
    from caper.views import create_empty_project
    request = _with_messages(request_factory.post('/create-empty-project/', data={
        'project_name': name, 'description': 'd', 'alias': '',
        'publication_link': '', 'project_members': '', 'accept_license': 'on'}))
    request.user = user
    response = create_empty_project(request)
    assert response.status_code == 302
    return ObjectId(response.url.rstrip('/').split('/')[-1])


def test_create_empty_puts_the_project_in_a_chain_of_one(
        request_factory, test_user, mongo_collection):
    name = f'EmptyChain-{ObjectId()}'
    try:
        doc_id = _create_empty(request_factory, test_user, name)
        doc = mongo_collection.find_one({'_id': doc_id})
        missing = [f for f in STATUS_AND_CHAIN_FIELDS if f not in doc]
        assert not missing, f'empty project lacks {missing}'
        assert doc['version_chain_id'] == doc_id
        assert doc['version_ordinal'] == 1
        assert doc['is_latest'] is True
    finally:
        for doc in mongo_collection.find({'project_name': name}, {'_id': 1}):
            _cleanup_project(mongo_collection, str(doc['_id']))


@pytest.mark.slow
def test_files_added_to_an_empty_project_join_its_chain(
        request_factory, test_user, mongo_collection, no_s3):
    from conftest import DATASET_SMALL_TAR, _poll_until_finished, POLL_TIMEOUT
    from caper.views import edit_project_page
    name = f'EmptyChainAdd-{ObjectId()}'
    created = []
    try:
        empty_id = _create_empty(request_factory, test_user, name)
        created.append(empty_id)

        # The edit page forces "replace" on an empty project, so send that.
        with open(DATASET_SMALL_TAR, 'rb') as fh:
            request = _with_messages(request_factory.post(
                f'/project/{empty_id}/edit', format='multipart', data={
                    'project_name': name, 'description': 'd', 'private': 'private',
                    'publication_link': '', 'project_members': '', 'alias': '',
                    'project_mode': 'replace', 'remap_sample_names': 'false',
                    'accept_license': 'on', 'document': fh}))
            request.user = test_user
            response = edit_project_page(request, project_name=str(empty_id))
        assert response.status_code == 302
        new_id = ObjectId(response.url.rstrip('/').split('/')[-1])
        assert new_id != empty_id
        created.append(new_id)

        new = _poll_until_finished(mongo_collection, str(new_id))
        assert new is not None, f'timed out waiting for aggregation ({POLL_TIMEOUT}s)'
        assert not new.get('aggregation_failed'), new.get('error_message')
        assert new['version_chain_id'] == empty_id
        assert new['version_ordinal'] == 2
        assert new['previous_version_id'] == empty_id
        assert new['is_latest'] is True

        old = mongo_collection.find_one({'_id': empty_id})
        assert old['next_version_id'] == new_id
        assert old['is_latest'] is False
    finally:
        for doc_id in reversed(created):
            _cleanup_project(mongo_collection, str(doc_id))


# The pages submit with XMLHttpRequest. XHR would follow a redirect itself, so
# the view hands back the URL as JSON; an invalid form is still the 400 page,
# whose errors the script shows in place.
XHR = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}


def test_xhr_edit_answers_a_redirect_with_its_url(
        request_factory, test_user, mongo_collection, live_project, no_s3):
    from caper.views import edit_project_page
    request = _with_messages(request_factory.post(
        f'/project/{live_project}/edit', data=dict(EDIT, accept_license='on'), **XHR))
    request.user = test_user
    response = edit_project_page(request, project_name=str(live_project))
    assert response.status_code == 200
    assert response['Content-Type'] == 'application/json'
    assert json.loads(response.content) == {'redirect': f'/project/{live_project}'}
    assert mongo_collection.find_one({'_id': live_project})['description'] == 'after'


def test_xhr_edit_without_license_is_still_the_400_page(
        request_factory, test_user, mongo_collection, live_project):
    from caper.views import edit_project_page
    before = mongo_collection.find_one({'_id': live_project})
    request = _with_messages(request_factory.post(
        f'/project/{live_project}/edit', data=EDIT, **XHR))
    request.user = test_user
    response = edit_project_page(request, project_name=str(live_project))
    assert response.status_code == 400
    assert 'id="error_1_id_accept_license"' in response.content.decode()
    assert mongo_collection.find_one({'_id': live_project}) == before
