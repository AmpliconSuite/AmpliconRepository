"""A version a publication used takes a typed project name to delete.

Real documents and the real delete paths, against local Mongo: what matters is
that the document and its files are still there afterwards.
"""
import json
import uuid

import pytest
from bson import ObjectId

from caper import provenance, publication_versions
from caper.project_status import LIVE, SOFT_DELETED, SUPERSEDED, status_flags
from caper.project_version_cleanup import PUBLICATION_TAG_FIELD, build_deleted_version_tombstone
from caper.project_fields import level_of

pytestmark = pytest.mark.integration

FIELD = publication_versions.FIELD
TAG = {'publication': 'PMID 1', 'evidence': 'id in data availability',
       'tagged_by': 'pytest', 'tagged_at': '2026-11-04T00:00:00Z'}


@pytest.fixture
def chain(request, monkeypatch):
    """make(n, tagged=(ordinals), status=...) -> n versions of one project, oldest first."""
    from caper import version_purge
    from caper.utils import collection_handle_primary as projects

    monkeypatch.setattr(version_purge._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: fn(*a, **kw))
    made = []

    def make(n=2, tagged=(), head_status=LIVE, members=('pytest-pubtag',)):
        name = f'pubtag-{uuid.uuid4().hex[:12]}'
        chain_id = ObjectId()
        ids = [ObjectId() for _ in range(n)]
        docs = []
        for i, doc_id in enumerate(ids, 1):
            head = i == n
            doc = {'_id': doc_id, 'linkid': str(doc_id), 'project_name': name, 'description': '',
                   'private': 'private', 'project_members': list(members), 'runs': {},
                   'date': f'2026-11-0{i}', 'FINISHED?': True,
                   'version_chain_id': chain_id, 'version_ordinal': i, 'is_latest': head,
                   'previous_version_id': ids[i - 2] if i > 1 else None,
                   'next_version_id': ids[i] if not head else None,
                   'previous_versions': [{'linkid': str(x), 'date': f'2026-11-0{j}'}
                                         for j, x in enumerate(ids[:i - 1], 1)],
                   **status_flags(head_status if head else SUPERSEDED)}
            if i in tagged:
                doc[FIELD] = dict(TAG)
            docs.append(doc)
        projects.insert_many(docs)
        made.extend(ids)
        return docs

    yield make
    projects.delete_many({'_id': {'$in': made}})


class _NotAdmin:
    username = 'pubtag-member'
    email = 'member@example.com'
    is_authenticated = True
    is_staff = False


def _doc(document):
    from caper.utils import collection_handle_primary
    return collection_handle_primary.find_one({'_id': document['_id']})


def _delete_version(request_factory, user, head, victim, body=None):
    from caper.views import delete_project_version
    req = request_factory.post(f"/project/{head['_id']}/delete_version/{victim['_id']}",
                               data=json.dumps(body or {}), content_type='application/json')
    req.user = user
    return delete_project_version(req, str(head['_id']), str(victim['_id']))


def _tag_view(request_factory, user, project, version, body):
    from caper.views import tag_publication_version
    req = request_factory.post(f"/project/{project['_id']}/publication_tag/{version['_id']}",
                               data=json.dumps(body), content_type='application/json',
                               HTTP_HOST='localhost')
    req.user = user
    return tag_publication_version(req, str(project['_id']), str(version['_id']))


# --- The field ---

def test_the_field_is_version_level_and_the_two_names_agree():
    assert level_of(FIELD) == 'version'
    assert PUBLICATION_TAG_FIELD == FIELD


def test_a_tombstone_keeps_the_record_and_is_no_longer_protected(chain):
    old, head = chain(tagged={1})
    tombstone = build_deleted_version_tombstone(old, head, 'someone', '2026-11-04')
    assert tombstone[FIELD] == TAG
    assert not publication_versions.is_protected(tombstone)


@pytest.mark.parametrize('publication,evidence', [('', 'x'), ('x', ''), ('  ', 'x'), ('x', None),
                                                  ('x' * 1001, 'x'), ('x', 'x' * 2001)])
def test_a_tag_needs_a_publication_and_how_we_know(publication, evidence):
    with pytest.raises(ValueError):
        publication_versions.build_tag(publication, evidence, 'someone')


@pytest.mark.parametrize('typed', [None, '', '   ', 'wrong'])
def test_only_the_project_name_confirms(typed):
    assert not publication_versions.name_confirmed({'project_name': 'name'}, typed)


def test_a_project_with_no_name_cannot_be_confirmed_with_a_blank():
    assert not publication_versions.name_confirmed({}, '')


def test_the_confirmation_does_not_outlive_its_block():
    document = {'_id': 1, 'project_name': 'n', FIELD: TAG, **status_flags(SUPERSEDED)}
    with publication_versions.delete_confirmed():
        publication_versions.refuse_unless_confirmed(document)
    with pytest.raises(publication_versions.PublicationVersionProtected):
        publication_versions.refuse_unless_confirmed(document)


# --- Deleting one version ---

def test_deleting_a_tagged_version_asks_and_changes_nothing(chain, request_factory, admin_user):
    old, head = chain(tagged={1})

    response = _delete_version(request_factory, admin_user, head, old)

    assert response.status_code == 409
    body = json.loads(response.content)
    assert body['project_name'] == old['project_name']
    assert body['publication'] == 'PMID 1'
    after = _doc(old)
    assert after[FIELD] == TAG and not after.get('version_deleted_from_history')


def test_the_wrong_name_changes_nothing(chain, request_factory, admin_user):
    old, head = chain(tagged={1})

    response = _delete_version(request_factory, admin_user, head, old,
                               {'confirm_project_name': 'not the name'})

    assert response.status_code == 409
    assert not _doc(old).get('version_deleted_from_history')


def test_the_right_name_deletes_it_and_the_tombstone_keeps_the_tag(chain, request_factory, admin_user):
    old, head = chain(tagged={1})

    response = _delete_version(request_factory, admin_user, head, old,
                               {'confirm_project_name': old['project_name']})

    assert response.status_code == 200, response.content
    after = _doc(old)
    assert after['version_deleted_from_history'] is True
    assert after[FIELD] == TAG


def test_a_body_that_is_not_json_asks_rather_than_deletes(chain, request_factory, admin_user):
    from caper.views import delete_project_version
    old, head = chain(tagged={1})
    req = request_factory.post('/x', data='not json', content_type='application/json')
    req.user = admin_user

    assert delete_project_version(req, str(head['_id']), str(old['_id'])).status_code == 409


def test_the_current_version_is_protected_too(chain, request_factory, admin_user):
    old, head = chain(tagged={2})

    assert _delete_version(request_factory, admin_user, head, head).status_code == 409
    assert _doc(head)['is_latest'] is True


def test_an_untagged_version_deletes_as_before(chain, request_factory, admin_user):
    old, head = chain(tagged={2})

    response = _delete_version(request_factory, admin_user, head, old)

    assert response.status_code == 200, response.content
    assert _doc(old)['version_deleted_from_history'] is True


# --- Permanent delete ---

def test_the_function_every_permanent_delete_passes_through_refuses(chain):
    from caper.views_admin import admin_permanent_delete_project
    only, = chain(1, tagged={1}, head_status=SOFT_DELETED)

    with pytest.raises(publication_versions.PublicationVersionProtected):
        admin_permanent_delete_project(str(only['_id']), only, only['project_name'])

    assert _doc(only) is not None


def test_a_tagged_older_version_stops_the_delete_before_anything_goes(chain):
    from caper.views_admin import permanently_delete_with_history
    older, head = chain(tagged={1}, head_status=SOFT_DELETED)
    from caper.utils import collection_handle_primary
    collection_handle_primary.update_one({'_id': older['_id']}, {'$set': status_flags(SOFT_DELETED)})

    with pytest.raises(publication_versions.PublicationVersionProtected):
        permanently_delete_with_history(str(head['_id']), _doc(head), head['project_name'])

    assert _doc(older) is not None and _doc(head) is not None


def test_a_tagged_head_stops_the_delete_before_its_older_versions_go(chain):
    """Otherwise the history is gone by the time the head refuses."""
    from caper.views_admin import permanently_delete_with_history
    from caper.utils import collection_handle_primary
    older, head = chain(tagged={2}, head_status=SOFT_DELETED)
    collection_handle_primary.update_one({'_id': older['_id']}, {'$set': status_flags(SOFT_DELETED)})

    with pytest.raises(publication_versions.PublicationVersionProtected):
        permanently_delete_with_history(str(head['_id']), _doc(head), head['project_name'])

    assert _doc(older) is not None and _doc(head) is not None


def test_the_typed_name_lets_the_whole_chain_go(chain):
    from caper.views_admin import permanently_delete_with_history
    from caper.utils import collection_handle_primary
    older, head = chain(tagged={1}, head_status=SOFT_DELETED)
    collection_handle_primary.update_one({'_id': older['_id']}, {'$set': status_flags(SOFT_DELETED)})

    permanently_delete_with_history(str(head['_id']), _doc(head), head['project_name'],
                                    confirm_project_name=head['project_name'])

    assert _doc(older) is None and _doc(head) is None


def test_the_bulk_delete_skips_it_and_carries_on(chain):
    from caper.views_admin import delete_selected_projects
    tagged, = chain(1, tagged={1}, head_status=SOFT_DELETED)
    plain, = chain(1, head_status=SOFT_DELETED)

    message = delete_selected_projects([str(tagged['_id']), str(plain['_id'])])

    assert 'Permanently deleted 1 of 2' in message
    assert 'used in a publication' in message
    assert _doc(tagged) is not None
    assert _doc(plain) is None


def test_an_account_deletion_reports_it_as_an_error(chain):
    from caper.account_deletion import dispose_of_projects
    from caper.utils import collection_handle_primary
    username = f'pubtag-user-{uuid.uuid4().hex[:8]}'
    only, = chain(1, tagged={1}, members=(username,))

    report = dispose_of_projects(username, None, projects_collection=collection_handle_primary)

    assert report['deleted'] == []
    assert report['errors'] == [only['project_name']]
    assert _doc(only) is not None


def _admin_post(request_factory, admin_user, data):
    from caper.views import admin_delete_project
    req = request_factory.post('/admin-delete-project/', data, HTTP_HOST='localhost')
    req.user = admin_user
    return admin_delete_project(req)


def test_the_admin_page_refuses_the_wrong_name(chain, request_factory, admin_user):
    only, = chain(1, tagged={1}, head_status=SOFT_DELETED)

    response = _admin_post(request_factory, admin_user,
                           {'action': 'delete', 'delete': 'on', 'project_id': str(only['_id']),
                            'project_name': only['project_name'], 'confirm_project_name': 'wrong'})

    assert response.status_code == 200
    assert b'Nothing was deleted' in response.content
    assert _doc(only) is not None


def test_the_admin_page_deletes_with_the_right_name(chain, request_factory, admin_user):
    only, = chain(1, tagged={1}, head_status=SOFT_DELETED)

    _admin_post(request_factory, admin_user,
                {'action': 'delete', 'delete': 'on', 'project_id': str(only['_id']),
                 'project_name': only['project_name'],
                 'confirm_project_name': only['project_name']})

    assert _doc(only) is None


def test_the_admin_page_marks_the_project_and_asks_before_posting(chain, request_factory, admin_user):
    only, = chain(1, tagged={1}, head_status=SOFT_DELETED)

    from caper.views import admin_delete_project
    req = request_factory.get('/admin-delete-project/', HTTP_HOST='localhost')
    req.user = admin_user
    response = admin_delete_project(req)

    assert b'1 used in a publication' in response.content
    assert f"'{only['project_name']}', true, 1)".encode() in response.content


# --- Setting the tag ---

def test_an_admin_tags_a_version_and_it_is_recorded(chain, request_factory, admin_user):
    from caper.utils import audit_log_handle
    old, head = chain()

    response = _tag_view(request_factory, admin_user, head, old,
                         {'publication': 'PMID 2', 'evidence': 'its data availability statement'})

    assert response.status_code == 200, response.content
    tag = _doc(old)[FIELD]
    assert tag['publication'] == 'PMID 2'
    assert tag['evidence'] == 'its data availability statement'
    assert tag['tagged_by'] == admin_user.username
    assert FIELD not in _doc(head), 'the tag belongs to one version'
    event = audit_log_handle.find_one(
        {'event_type': provenance.TAG_PUBLICATION_VERSION, 'project_uuid': str(old['_id'])},
        sort=[('_id', -1)])
    assert event['completed'] is True


def test_an_admin_removes_a_tag_and_it_can_then_be_deleted(chain, request_factory, admin_user):
    old, head = chain(tagged={1})

    assert _tag_view(request_factory, admin_user, head, old, {'action': 'remove'}).status_code == 200

    assert FIELD not in _doc(old)
    assert _delete_version(request_factory, admin_user, head, old).status_code == 200


def test_someone_who_is_not_an_admin_cannot_tag(chain, request_factory):
    old, head = chain()

    response = _tag_view(request_factory, _NotAdmin(), head, old,
                         {'publication': 'PMID 3', 'evidence': 'because'})

    assert response.status_code == 302
    assert FIELD not in _doc(old)


def test_someone_who_is_not_an_admin_cannot_remove_a_tag(chain, request_factory):
    old, head = chain(tagged={1})

    assert _tag_view(request_factory, _NotAdmin(), head, old, {'action': 'remove'}).status_code == 302
    assert _doc(old)[FIELD] == TAG


def test_a_tag_is_refused_on_another_projects_version(chain, request_factory, admin_user):
    _, head = chain()
    other, _ = chain()

    response = _tag_view(request_factory, admin_user, head, other,
                         {'publication': 'PMID 4', 'evidence': 'because'})

    assert response.status_code == 404
    assert FIELD not in _doc(other)


def test_a_tag_without_evidence_is_refused(chain, request_factory, admin_user):
    old, head = chain()

    response = _tag_view(request_factory, admin_user, head, old, {'publication': 'PMID 5'})

    assert response.status_code == 400
    assert FIELD not in _doc(old)


# --- The history table ---

def test_the_history_table_reads_the_tag(chain):
    from caper.utils import previous_versions
    old, head = chain(tagged={1})

    entries, _message = previous_versions(_doc(head))

    tags = {entry['linkid']: entry.get('publication_tag') for entry in entries}
    assert tags[str(old['_id'])] == TAG
    assert tags[str(head['_id'])] is None
