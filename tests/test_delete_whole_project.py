"""
The Delete button deletes the whole project, and its URLs are remembered.

Until 2026-10-30 it soft-deleted the current version only: an older version
kept answering 200, page and API, to anyone holding its link, until an admin
purged the project.  Now every version closes with the head, restoring the
head reopens them, and each version's URL is recorded with the project's name
so a dead link can say what it was.
"""

import uuid

import pytest
from bson import ObjectId
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.integration

PASSWORD = 'DeleteWholeProject!123'

RUNS = {'run1': [{
    'Sample_name': 'Sample_001', 'Feature_ID': 'Sample_001_amplicon1_ecDNA_1',
    'Feature_BED_file': 'Not Provided', 'Classification': 'ecDNA', 'Oncogenes': [],
    'AA_amplicon_number': 1, 'Location': ['chr1:1-2'], 'Reference_version': 'GRCh38',
    'CNV_BED_file': 'Not Provided', 'AA_PNG_file': 'Not Provided',
    'AA_PDF_file': 'Not Provided', 'Feature_maximum_copy_number': 12.5,
    'Feature_median_copy_number': 8.0}]}


def _user(**extra):
    suffix = uuid.uuid4().hex
    return get_user_model().objects.create_user(
        username=f'delete_whole_{suffix}', email=f'delete_whole_{suffix}@example.com',
        password=PASSWORD, **extra)


@pytest.fixture
def owner():
    user = _user()
    yield user
    user.delete()


@pytest.fixture
def site_admin():
    user = _user(is_staff=True)
    yield user
    user.delete()


@pytest.fixture(autouse=True)
def uncounted(monkeypatch):
    """The fixture projects were never counted into site_statistics or the
    index, so a delete must not uncount them from either."""
    from caper import views
    monkeypatch.setattr(views, 'project_removed', lambda project, visibility: None)


@pytest.fixture
def make_chain(mongo_collection, owner):
    """A two-version project: (old id, head id) as strings."""
    from caper.utils import audit_log_handle, deleted_urls_handle
    made = []

    def make(visibility='public'):
        old, head = ObjectId(), ObjectId()
        name = f'DeleteWhole_{uuid.uuid4().hex[:8]}'
        base = {'project_name': name, 'private': visibility,
                'project_members': [owner.username], 'runs': RUNS, 'sample_count': 1,
                'views': 0, 'downloads': 0, 'date': '2024-01-01', 'FINISHED?': True,
                'version_chain_id': old, 'previous_version_id': None, 'next_version_id': None}
        mongo_collection.insert_many([
            {**base, '_id': old, 'linkid': str(old), 'delete': True, 'current': False,
             'version_ordinal': 1, 'is_latest': False, 'next_version_id': head,
             'previous_versions': []},
            {**base, '_id': head, 'linkid': str(head), 'delete': False, 'current': True,
             'version_ordinal': 2, 'is_latest': True, 'previous_version_id': old,
             'previous_versions': [{'linkid': str(old), 'date': '2024-01-01'}]}])
        made.extend([old, head])
        return str(old), str(head), name

    yield make
    mongo_collection.delete_many({'_id': {'$in': made}})
    deleted_urls_handle.delete_many({'_id': {'$in': made}})
    audit_log_handle.delete_many({'project_uuid': {'$in': [str(i) for i in made]}})


def _signed_in(user):
    client = Client(HTTP_HOST='localhost')
    client.force_login(user)
    return client


def test_deleting_a_project_closes_its_older_versions(make_chain, owner, mongo_collection):
    old, head, name = make_chain()
    anonymous = Client(HTTP_HOST='localhost')
    assert anonymous.get(f'/project/{old}').status_code == 200

    assert _signed_in(owner).post(f'/project/{head}/delete').status_code == 302

    for version in (old, head):
        page = anonymous.get(f'/project/{version}')
        assert page.status_code == 410, version
        assert name in page.content.decode()
        assert 'together with all of its versions' in page.content.decode()
    # The pages under a version go with it.
    assert anonymous.get(f'/project/{old}/sample/Sample_001').status_code == 410

    api = anonymous.get(f'/api/v1/projects/{old}/')
    assert api.status_code == 404
    assert api.json()['code'] == 'not_found'
    assert api.json()['deleted'] is True and api.json()['project_name'] == name

    # The older version's own document was not written to.
    assert mongo_collection.find_one({'_id': ObjectId(old)})['current'] is False


def test_every_version_is_recorded_under_its_name(make_chain, owner):
    from caper.utils import deleted_urls_handle
    old, head, name = make_chain()

    _signed_in(owner).post(f'/project/{head}/delete')

    rows = {str(r['_id']): r for r in deleted_urls_handle.find(
        {'_id': {'$in': [ObjectId(old), ObjectId(head)]}})}
    assert set(rows) == {old, head}
    assert {r['project_name'] for r in rows.values()} == {name}
    assert rows[head]['was_head'] and not rows[old]['was_head']
    assert rows[old]['deleted_by'] == owner.email
    assert rows[old]['history'][-1]['event'] == 'deleted'


def test_restoring_the_project_reopens_every_version(make_chain, owner, site_admin):
    from caper.utils import deleted_urls_handle
    old, head, name = make_chain()
    _signed_in(owner).post(f'/project/{head}/delete')

    _signed_in(site_admin).post('/admin-delete-project/', {
        'project_name': name, 'project_id': head, 'delete': '', 'action': 'un-delete'})

    anonymous = Client(HTTP_HOST='localhost')
    assert anonymous.get(f'/project/{head}').status_code == 200
    assert anonymous.get(f'/project/{old}').status_code == 200
    # The rows stay, marked: the record is permanent.
    row = deleted_urls_handle.find_one({'_id': ObjectId(old)})
    assert row['restored_at'] is not None
    assert row['history'][-1]['event'] == 'restored'


def test_a_private_project_is_not_named_to_a_stranger(make_chain, owner, site_admin):
    old, head, name = make_chain(visibility='private')
    _signed_in(owner).post(f'/project/{head}/delete')

    stranger = Client(HTTP_HOST='localhost').get(f'/project/{old}')
    assert stranger.status_code == 410
    assert name not in stranger.content.decode()
    assert 'project_name' not in Client(HTTP_HOST='localhost').get(f'/api/v1/projects/{old}/').json()

    assert name in _signed_in(site_admin).get(f'/project/{old}').content.decode()


def test_an_edit_retiring_a_version_records_nothing(make_chain, owner):
    """project_delete() is also how an edit retires the version it replaced;
    only the button's route deletes the whole project."""
    from django.test import RequestFactory
    from caper import views
    from caper.utils import deleted_urls_handle
    old, head, name = make_chain()
    request = RequestFactory().get('/')
    request.user = owner

    views.project_delete(request, head)

    assert deleted_urls_handle.count_documents({'head_id': ObjectId(head)}) == 0


def test_a_project_deleted_before_the_record_is_recorded_when_purged(
        make_chain, owner, mongo_collection):
    """A project soft-deleted before 2026-10-30 has no rows; the permanent
    delete writes them first, while the documents still exist to say what
    their URLs were."""
    from caper.deleted_projects import ensure_recorded
    from caper.utils import deleted_urls_handle
    old, head, name = make_chain()
    mongo_collection.update_one({'_id': ObjectId(head)},
                                {'$set': {'delete': True, 'delete_user': 'someone@example.com'}})
    head_doc = mongo_collection.find_one({'_id': ObjectId(head)})

    assert ensure_recorded(mongo_collection, deleted_urls_handle, head_doc) == 2
    assert ensure_recorded(mongo_collection, deleted_urls_handle, head_doc) == 0
    row = deleted_urls_handle.find_one({'_id': ObjectId(old)})
    assert row['recorded_late'] is True and row['deleted_by'] == 'someone@example.com'
    assert row['project_name'] == name


def test_a_live_project_is_not_closed(make_chain, mongo_collection):
    from caper.deleted_projects import chain_is_deleted
    old, head, name = make_chain()
    assert not chain_is_deleted(mongo_collection, mongo_collection.find_one({'_id': ObjectId(old)}))
