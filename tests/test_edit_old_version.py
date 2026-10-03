"""Editing an old version of a project: its description, link and tool versions.

Before this, GET /project/<old>/edit redirected to the current version and
nothing stopped a POST, which ran the full edit on the old document -- clearing
its alias, rewriting its members and date, or making a new version out of it.

The rules held here:
  * an old version's own description, publication link and tool versions can
    be corrected, and nothing else on it or on any other version moves;
  * edit permission comes from the current version, not the old document's
    stale copy of project_members;
  * anything that needs a new version is refused on an old version, and so is
    any edit at all to an old version whose project is deleted.
"""
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


def _messages_of(request):
    return [str(m) for m in request._messages]


@pytest.fixture
def no_s3(monkeypatch):
    """The audit log looks up the archive's S3 size; nothing here has one."""
    from caper import views
    monkeypatch.setattr(views, '_get_project_s3_uri', lambda linkid: None)


@pytest.fixture
def make_chain(mongo_collection, test_user):
    """Insert an old version and its current version as one chain."""
    created = []

    def make(old_members=None, head_members=None, private='private',
             head_status='LIVE', old_extra=None):
        chain_id = ObjectId()
        old_id, head_id = ObjectId(), ObjectId()
        members = [test_user.username]
        old = {
            '_id': old_id, 'linkid': str(old_id),
            'project_name': 'OldVersionEditTest', 'description': 'old description',
            'publication_link': '', 'date': '2025-01-01T00:00:00',
            'private': private, 'project_members': old_members or members,
            'alias_name': None, 'creator': test_user.username,
            'delete': True, 'current': False, 'FINISHED?': True,
            'AA_version': '1.3.r8', 'AC_version': '1.1.2', 'ASP_version': '1.3.5',
            'runs': {}, 'previous_versions': [],
            'version_chain_id': chain_id, 'version_ordinal': 1, 'is_latest': False,
            'next_version_id': head_id,
        }
        old.update(old_extra or {})
        head_flags = ({'delete': False, 'current': True} if head_status == 'LIVE'
                      else {'delete': True, 'current': True})
        head = {
            '_id': head_id, 'linkid': str(head_id),
            'project_name': 'OldVersionEditTest', 'description': 'head description',
            'publication_link': '', 'date': '2026-01-01T00:00:00',
            'private': private, 'project_members': head_members or members,
            'alias_name': 'old_version_edit_test_alias', 'creator': test_user.username,
            'FINISHED?': True, **head_flags,
            'AA_version': '1.5.r1', 'AC_version': '2.0.0', 'ASP_version': '1.5.0',
            'runs': {},
            'previous_versions': [{'linkid': str(old_id), 'date': old['date'],
                                   'AA_version': '1.3.r8', 'AC_version': '1.1.2',
                                   'ASP_version': '1.3.5'}],
            'version_chain_id': chain_id, 'version_ordinal': 2, 'is_latest': True,
            'previous_version_id': old_id,
        }
        mongo_collection.insert_many([old, head])
        created.extend([old_id, head_id])
        return old_id, head_id

    yield make
    for doc_id in created:
        _cleanup_project(mongo_collection, str(doc_id))
    from caper.views import audit_log_handle
    audit_log_handle.delete_many({'project_uuid': {'$in': [str(i) for i in created]}})


def _get(request_factory, user, doc_id):
    from caper.views import edit_project_page
    request = _with_messages(request_factory.get(f'/project/{doc_id}/edit'))
    request.user = user
    return request, edit_project_page(request, project_name=str(doc_id))


def _post(request_factory, user, doc_id, data):
    from caper.views import edit_project_page
    request = _with_messages(request_factory.post(f'/project/{doc_id}/edit', data=data))
    request.user = user
    return request, edit_project_page(request, project_name=str(doc_id))


EDIT = {'description': 'corrected description',
        'publication_link': 'https://doi.org/10.1000/xyz',
        'ASP_version': '1.3.5', 'AA_version': '1.3.r9', 'AC_version': '1.1.2'}


def test_get_renders_the_old_version_form_instead_of_redirecting(
        request_factory, test_user, make_chain):
    old_id, _ = make_chain()
    _, response = _get(request_factory, test_user, old_id)
    assert response.status_code == 200
    html = response.content.decode()
    assert 'Editing an older version' in html
    assert '1.3.r8' in html and 'old description' in html
    # No data-changing controls.
    assert 'samples_to_remove' not in html and 'type="file"' not in html


def test_post_changes_only_the_old_versions_own_fields(
        request_factory, test_user, make_chain, mongo_collection, no_s3):
    old_id, head_id = make_chain()
    before_old = mongo_collection.find_one({'_id': old_id})
    before_head = mongo_collection.find_one({'_id': head_id})

    request, response = _post(request_factory, test_user, old_id, EDIT)
    assert response.status_code == 302 and response.url.endswith(f'/project/{old_id}')

    after_old = mongo_collection.find_one({'_id': old_id})
    changed = {k for k in set(before_old) | set(after_old)
               if before_old.get(k) != after_old.get(k)}
    assert changed == {'description', 'publication_link', 'AA_version'}
    assert after_old['AA_version'] == '1.3.r9'
    assert after_old['description'] == 'corrected description'
    assert mongo_collection.find_one({'_id': head_id}) == before_head


def test_history_table_shows_the_corrected_version(
        request_factory, test_user, make_chain, mongo_collection, no_s3):
    from caper.utils import previous_versions
    old_id, head_id = make_chain()
    _post(request_factory, test_user, old_id, EDIT)
    entries, _ = previous_versions(mongo_collection.find_one({'_id': head_id}))
    row = next(e for e in entries if str(e['linkid']) == str(old_id))
    assert row['AA_version'] == '1.3.r9'


def test_an_audit_event_names_the_old_version(
        request_factory, test_user, make_chain, no_s3):
    from caper.views import audit_log_handle
    old_id, _ = make_chain()
    _post(request_factory, test_user, old_id, EDIT)
    event = audit_log_handle.find_one({'project_uuid': str(old_id)})
    assert event['event_type'] == 'edit_no_version'
    assert event['AA_version'] == '1.3.r9'


def test_blank_version_becomes_NA_and_an_absent_field_is_untouched(
        request_factory, test_user, make_chain, mongo_collection, no_s3):
    old_id, _ = make_chain(old_extra={'CoRAL_version': '1.0.0'})
    data = dict(EDIT, AA_version='   ')  # CoRAL_version not posted at all
    _post(request_factory, test_user, old_id, data)
    doc = mongo_collection.find_one({'_id': old_id})
    assert doc['AA_version'] == 'NA'
    assert doc['CoRAL_version'] == '1.0.0'


def test_no_change_writes_nothing(
        request_factory, test_user, make_chain, mongo_collection, no_s3):
    from caper.views import audit_log_handle
    old_id, _ = make_chain()
    before = mongo_collection.find_one({'_id': old_id})
    data = {'description': 'old description', 'publication_link': '',
            'ASP_version': '1.3.5', 'AA_version': '1.3.r8', 'AC_version': '1.1.2'}
    request, _ = _post(request_factory, test_user, old_id, data)
    assert mongo_collection.find_one({'_id': old_id}) == before
    assert audit_log_handle.count_documents({'project_uuid': str(old_id)}) == 0
    assert 'No changes to save.' in _messages_of(request)


@pytest.mark.parametrize('extra', [
    {'samples_to_remove': 'S1'},
    {'project_mode': 'reaggregate'},
    {'reaggregate_project': 'on'},
    {'remap_sample_names': 'true'},
])
def test_anything_needing_a_new_version_is_refused(
        request_factory, test_user, make_chain, mongo_collection, extra):
    old_id, head_id = make_chain()
    before = list(mongo_collection.find({'version_chain_id': mongo_collection.find_one({'_id': old_id})['version_chain_id']}))
    count_before = mongo_collection.count_documents({})
    _post(request_factory, test_user, old_id, dict(EDIT, **extra))
    after = list(mongo_collection.find({'version_chain_id': before[0]['version_chain_id']}))
    assert sorted(after, key=lambda d: d['_id']) == sorted(before, key=lambda d: d['_id'])
    assert mongo_collection.count_documents({}) == count_before


def test_permission_comes_from_the_current_version(
        request_factory, test_user, non_member_user, make_chain, mongo_collection, no_s3):
    # Removed from the project since the old version was superseded: the old
    # document still lists them, the current one does not.
    old_id, _ = make_chain(old_members=[non_member_user.username],
                           head_members=[test_user.username])
    _, response = _get(request_factory, non_member_user, old_id)
    assert response.content == b'Project does not exist'
    _post(request_factory, non_member_user, old_id, EDIT)
    assert mongo_collection.find_one({'_id': old_id})['description'] == 'old description'

    # Added since: the old document does not list them, the current one does.
    old_id2, _ = make_chain(old_members=[test_user.username],
                            head_members=[non_member_user.username])
    _post(request_factory, non_member_user, old_id2, EDIT)
    assert mongo_collection.find_one({'_id': old_id2})['description'] == 'corrected description'


def test_anonymous_user_is_refused(request_factory, make_chain, mongo_collection):
    from django.contrib.auth.models import AnonymousUser
    old_id, _ = make_chain(private='public')
    _post(request_factory, AnonymousUser(), old_id, EDIT)
    assert mongo_collection.find_one({'_id': old_id})['description'] == 'old description'


def test_old_version_of_a_deleted_project_is_not_editable(
        request_factory, test_user, make_chain, mongo_collection):
    old_id, _ = make_chain(head_status='SOFT_DELETED')
    before = mongo_collection.find_one({'_id': old_id})
    request, response = _post(request_factory, test_user, old_id, EDIT)
    assert mongo_collection.find_one({'_id': old_id}) == before
    assert response.status_code == 302
    assert 'Only the current version of this project can be edited.' in _messages_of(request)


def test_current_version_edit_page_is_unchanged(request_factory, test_user, make_chain):
    _, head_id = make_chain()
    _, response = _get(request_factory, test_user, head_id)
    html = response.content.decode()
    assert 'Editing project:' in html and 'Editing an older version' not in html


def test_project_page_offers_edit_on_an_old_version_to_members_only(
        request_factory, test_user, non_member_user, make_chain):
    from caper.views import project_page
    old_id, _ = make_chain(private='public')
    edit_url = f'/project/{old_id}/edit'

    for user, expected in ((test_user, True), (non_member_user, False)):
        request = _with_messages(request_factory.get(f'/project/{old_id}'))
        request.user = user
        html = project_page(request, str(old_id)).content.decode()
        assert (edit_url in html) is expected, user.username
