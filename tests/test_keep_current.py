"""The keep-current opt-in: "Keep this project's classifications up to date".

It belongs to the project, not to one version. What matters: it is off unless someone
ticks it, changing it alone doesn't make a new version, an admin's replacement keeps it,
deleting the newest version leaves the setting where the project now is, and only admins
can read the list the reclassifier works from.
"""

import json
import uuid

import pytest
from bson.objectid import ObjectId
from rest_framework.test import APIRequestFactory

from conftest import _cleanup_project
from caper import project_replace, utils
from caper.forms import RunForm, UpdateForm
from caper.project_fields import CARRIED_ON_PROMOTION, level_of
from caper.project_status import LIVE, SUPERSEDED, TOMBSTONE, classify, status_flags

pytestmark = pytest.mark.integration


def _user(is_staff):
    from django.contrib.auth.models import User
    from rest_framework.authtoken.models import Token

    name = f'pytest_keep_{uuid.uuid4().hex[:8]}'
    user = User.objects.create_user(username=name, email=f'{name}@example.com', is_staff=is_staff)
    return user, Token.objects.create(user=user).key


@pytest.fixture
def admin():
    user, token = _user(True)
    yield user, token
    user.delete()


@pytest.fixture
def member():
    user, token = _user(False)
    yield user, token
    user.delete()


@pytest.fixture
def make_project():
    """make(**fields) -> a finished one-version project with nothing in GridFS."""
    collection = utils.collection_handle_primary
    made = []

    def make(**fields):
        doc = {'project_name': f'pytest keep {uuid.uuid4().hex[:8]}', 'description': 'd',
               'publication_link': '', 'private': 'public', 'alias_name': None,
               'project_members': ['pytest_keep_owner'], 'date': '2026-11-07', 'runs': {},
               'sample_data': [], 'FINISHED?': True, 'sample_count': 0, 'views': 0,
               'downloads': 0, 'subscribers': [], 'previous_versions': [], **status_flags(LIVE)}
        doc.update(fields)
        new_id = collection.insert_one(doc).inserted_id
        made.append(new_id)
        return collection.find_one({'_id': new_id})

    yield make
    for doc_id in made:
        _cleanup_project(collection, str(doc_id))


def _doc(doc_id):
    return utils.collection_handle_primary.find_one({'_id': ObjectId(str(doc_id))})


# ---- The field ----

def test_it_belongs_to_the_project_and_promotion_carries_it():
    assert level_of('keep_current') == 'chain'
    assert 'keep_current' in CARRIED_ON_PROMOTION


@pytest.mark.parametrize('form_class', [RunForm, UpdateForm])
def test_it_is_on_both_forms_off_by_default_and_never_required(form_class):
    field = form_class().fields['keep_current']
    assert not field.required
    assert form_class().initial.get('keep_current') in (None, False)
    assert 'new version' in field.help_text, 'says what opting in lets admins do'


def test_an_unticked_box_means_off_and_a_ticked_one_on():
    base = {'project_name': 'p', 'description': 'd', 'private': 'public',
            'project_members': '', 'accept_license': 'on', 'alias': ''}
    for posted, expected in [({}, False), ({'keep_current': 'on'}, True),
                             ({'keep_current': 'true'}, True), ({'keep_current': 'false'}, False)]:
        form = RunForm(dict(base, **posted))
        assert form.is_valid(), form.errors
        assert utils.form_to_dict(form)['keep_current'] is expected, posted


# ---- An admin's replacement keeps whatever the project said ----

@pytest.mark.parametrize('stored, posted', [(True, 'true'), (False, 'false'), (None, 'false')])
def test_the_replace_route_posts_the_projects_own_setting(make_project, stored, posted):
    fields = {} if stored is None else {'keep_current': stored}
    project = make_project(**fields)

    form, post = project_replace.form_for(project)

    assert post['keep_current'] == posted
    assert form.is_valid(), form.errors
    assert utils.form_to_dict(form)['keep_current'] is (stored is True)


# ---- Changing it alone edits the project in place ----

def _form_edit(user, project, **fields):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.test import RequestFactory

    from caper.views import edit_project_page

    data = {'project_name': project['project_name'], 'description': project['description'],
            'private': project['private'], 'publication_link': project['publication_link'],
            'project_members': ','.join(project['project_members']), 'alias': '',
            'remap_sample_names': 'false', 'accept_license': 'on', **fields}
    request = RequestFactory().post(f"/project/{project['_id']}/edit", data=data)
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return edit_project_page(request, project_name=str(project['_id']))


@pytest.mark.parametrize('who', ['member', 'admin'])
def test_a_member_or_an_admin_can_tick_and_untick_it_without_a_new_version(
        make_project, member, admin, who):
    user = {'member': member, 'admin': admin}[who][0]
    project = make_project(project_members=[member[0].username])
    count = utils.collection_handle_primary.count_documents({'project_name': project['project_name']})

    _form_edit(user, project, keep_current='on')
    assert _doc(project['_id'])['keep_current'] is True

    _form_edit(user, _doc(project['_id']))
    after = _doc(project['_id'])
    assert after['keep_current'] is False
    assert classify(after) == LIVE
    assert utils.collection_handle_primary.count_documents(
        {'project_name': project['project_name']}) == count, 'no new version'


def test_someone_who_is_neither_cannot_change_it(make_project, member):
    project = make_project(project_members=['pytest_keep_owner'])

    _form_edit(member[0], project, keep_current='on')

    assert 'keep_current' not in _doc(project['_id'])


def test_the_edit_page_shows_the_current_setting(make_project, member):
    from django.test import RequestFactory

    from caper.views import edit_project_page

    project = make_project(project_members=[member[0].username], keep_current=True)
    request = RequestFactory().get(f"/project/{project['_id']}/edit")
    request.user = member[0]
    request.session = {}
    response = edit_project_page(request, project_name=str(project['_id']))

    page = response.content.decode()
    assert 'name="keep_current"' in page
    tag = page[page.index('name="keep_current"') - 200:page.index('name="keep_current"') + 200]
    assert 'checked' in tag
    assert 'data-toggle="tooltip"' in page[page.index('id="div_id_keep_current"'):][:1500]


# ---- Deleting the newest version leaves the setting where the project now is ----

@pytest.fixture
def chain(monkeypatch):
    """make(settings) -> one version per setting, oldest first; the last is the head."""
    from caper import version_purge

    monkeypatch.setattr(version_purge._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: fn(*a, **kw))
    collection = utils.collection_handle_primary
    made = []

    def make(settings):
        name = f'pytest-keep-chain-{uuid.uuid4().hex[:10]}'
        chain_id, ids = ObjectId(), [ObjectId() for _ in settings]
        docs = []
        for i, (doc_id, setting) in enumerate(zip(ids, settings), 1):
            head = i == len(ids)
            docs.append({
                '_id': doc_id, 'linkid': str(doc_id), 'project_name': name, 'description': '',
                'private': 'public', 'project_members': ['pytest_keep_owner'], 'runs': {},
                'date': f'2026-11-0{i}', 'FINISHED?': True, 'keep_current': setting,
                'version_chain_id': chain_id, 'version_ordinal': i, 'is_latest': head,
                'previous_version_id': ids[i - 2] if i > 1 else None,
                'next_version_id': ids[i] if not head else None,
                'previous_versions': [{'linkid': str(x), 'date': f'2026-11-0{j}'}
                                      for j, x in enumerate(ids[:i - 1], 1)],
                **status_flags(LIVE if head else SUPERSEDED)})
        collection.insert_many(docs)
        made.extend(ids)
        return docs

    yield make
    for doc_id in made:
        _cleanup_project(collection, str(doc_id))


def _delete_version(admin_user, head, victim):
    from django.test import RequestFactory

    from caper.views import delete_project_version

    request = RequestFactory().post(f"/project/{head['_id']}/delete_version/{victim['_id']}",
                                    data='{}', content_type='application/json')
    request.user = admin_user
    response = delete_project_version(request, str(head['_id']), str(victim['_id']))
    assert response.status_code == 200, response.content


@pytest.mark.parametrize('newest', [True, False])
def test_deleting_the_newest_version_hands_its_setting_to_the_promoted_one(
        chain, admin_user, newest):
    """Version 2 already deleted, then version 3: version 1 is the project now, with 3's setting."""
    v1, v2, v3 = chain([not newest, not newest, newest])
    _delete_version(admin_user, v3, v2)
    assert classify(_doc(v2['_id'])) == TOMBSTONE

    _delete_version(admin_user, v3, v3)

    promoted = _doc(v1['_id'])
    assert classify(promoted) == LIVE
    assert promoted['keep_current'] is newest


# ---- The list the reclassifier reads ----

def _keep_current_list(token=None):
    from caper.views_apis import KeepCurrentListView

    headers = {'HTTP_AUTHORIZATION': f'Token {token}'} if token else {}
    request = APIRequestFactory().post('/upload_api/keep-current/', **headers)
    return KeepCurrentListView.as_view()(request)


def test_only_an_admin_can_read_the_list(member):
    assert _keep_current_list().status_code == 401
    assert _keep_current_list(member[1]).status_code == 403


def test_the_list_is_every_opted_in_current_version_that_is_not_private(make_project, admin):
    public = make_project(keep_current=True)
    hidden = make_project(keep_current=True, private='hidden_public')
    private = make_project(keep_current=True, private='private')  # made private some other way
    make_project(keep_current=False)
    make_project()
    old = make_project(keep_current=True, **status_flags(SUPERSEDED))

    response = _keep_current_list(admin[1])

    assert response.status_code == 200
    rows = {row['id']: row for row in response.data['projects']}
    assert rows[str(public['_id'])]['visibility'] == 'public'
    assert rows[str(hidden['_id'])] == {'id': str(hidden['_id']),
                                        'project_name': hidden['project_name'],
                                        'visibility': 'hidden_public', 'sample_count': 0}
    assert str(private['_id']) not in rows
    assert str(old['_id']) not in rows
    names = {row['project_name'] for row in rows.values()}
    assert len([n for n in names if n.startswith('pytest keep ')]) == 2
    json.dumps(response.data)


# ---- Not for private projects ----

_FORM = {'project_name': 'p', 'description': 'd', 'project_members': '',
         'accept_license': 'on', 'alias': ''}


@pytest.mark.parametrize('form_class', [RunForm, UpdateForm])
@pytest.mark.parametrize('visibility, allowed', [
    ('public', True), ('hidden_public', True), ('private', False)])
def test_the_box_is_refused_on_a_private_project(form_class, visibility, allowed):
    form = form_class(dict(_FORM, private=visibility, keep_current='on'))
    assert form.is_valid() is allowed, form.errors
    if not allowed:
        assert 'keep_current' in form.errors
    assert form_class(dict(_FORM, private=visibility)).is_valid(), 'unticked is always fine'


def test_making_an_opted_in_project_private_with_the_box_still_ticked_is_refused(
        make_project, member):
    project = make_project(project_members=[member[0].username], keep_current=True)

    _form_edit(member[0], project, private='private', keep_current='on')

    after = _doc(project['_id'])
    assert after['private'] == 'public' and after['keep_current'] is True


def test_the_box_greys_out_for_private_in_the_page(make_project, member):
    from django.test import RequestFactory

    from caper.views import edit_project_page

    project = make_project(project_members=[member[0].username])
    request = RequestFactory().get(f"/project/{project['_id']}/edit")
    request.user = member[0]
    request.session = {}
    page = edit_project_page(request, project_name=str(project['_id'])).content.decode()
    block = page[page.index('id="div_id_keep_current"'):][:3000]
    assert "visibility.value === 'private'" in block and 'box.disabled = isPrivate' in block


# ---- An admin token can read a hidden-public project, not a private one ----

def _api_get(view, token, **kwargs):
    headers = {'HTTP_AUTHORIZATION': f'Token {token}'} if token else {}
    request = APIRequestFactory().get('/api/v1/projects/x/', **headers)
    return view.as_view()(request, **kwargs)


@pytest.mark.parametrize('visibility, admin_reads', [('hidden_public', True), ('private', False)])
def test_an_admin_token_reads_hidden_public_but_not_private(make_project, admin, member,
                                                            visibility, admin_reads):
    from caper.views_apis import ProjectDetailView

    project = make_project(private=visibility, linkid=None)
    project_id = str(project['_id'])

    assert (_api_get(ProjectDetailView, admin[1], project_id=project_id).status_code == 200) is admin_reads
    assert _api_get(ProjectDetailView, member[1], project_id=project_id).status_code != 200
    assert _api_get(ProjectDetailView, None, project_id=project_id).status_code != 200


# ---- Adding samples by API keeps it too ----

@pytest.mark.parametrize('stored', [True, False])
def test_adding_samples_by_api_keeps_the_projects_setting(make_project, member, tmp_path,
                                                           monkeypatch, stored):
    """The route builds its form from the stored project, so the setting has to be in it."""
    import shutil

    from caper import background_tasks
    from caper.views_apis import ProjectFileAddView

    project = make_project(project_members=[member[0].username], privateKey='k',
                           linkid=None, keep_current=stored)
    utils.collection_handle_primary.update_one({'_id': project['_id']},
                                               {'$set': {'linkid': str(project['_id'])}})
    submitted = {}
    monkeypatch.setattr(background_tasks._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: submitted.update(args=a))
    archive = tmp_path / 'more.tar.gz'
    archive.write_bytes(b'not read before the background step')

    with open(archive, 'rb') as handle:
        request = APIRequestFactory().post(
            '/add_samples_to_project_api/', format='multipart',
            data={'project_uuid': str(project['_id']), 'project_key': 'k',
                  'username': member[0].username, 'file': handle})
        response = ProjectFileAddView.as_view()(request)

    new_id = submitted['args'][1] if submitted else None
    try:
        assert response.status_code == 200, response.data
        assert submitted['args'][3]['keep_current'] == ('true' if stored else 'false')
    finally:
        if new_id:
            _cleanup_project(utils.collection_handle_primary, new_id)
            shutil.rmtree(f'tmp/{new_id}', ignore_errors=True)
