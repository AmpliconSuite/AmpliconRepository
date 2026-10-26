"""
A project's visibility applies to every one of its versions.

Each version stores its own ``private``, every access check reads that copy,
and an edit or a new upload used to write only the head, so a change of
visibility left the older versions with the setting they had when they were
superseded: 22 old versions in 11 projects on prod on 2026-10-26. Both
directions are tested, because the rule is that every version follows the
project, whichever way the project moved.
"""

import pytest
from bson import ObjectId


def _chain(collection, visibilities, name):
    """Insert a fully pointered chain, oldest first; the last is the head."""
    chain_id, docs, previous = None, [], None
    for ordinal, vis in enumerate(visibilities, start=1):
        _id = ObjectId()
        chain_id = chain_id or _id
        head = ordinal == len(visibilities)
        docs.append({'_id': _id, 'project_name': name, 'private': vis,
                     'project_members': ['owner'], 'delete': not head, 'current': head,
                     'version_chain_id': chain_id, 'version_ordinal': ordinal,
                     'is_latest': head, 'previous_version_id': previous,
                     'next_version_id': None})
        if previous is not None:
            docs[-2]['next_version_id'] = _id
        previous = _id
    collection.insert_many(docs)
    return [d['_id'] for d in docs]


def _visibility(collection, ids):
    return [collection.find_one({'_id': i}, {'private': 1})['private'] for i in ids]


@pytest.fixture
def chains(mongo_collection):
    made = []
    def make(visibilities, name='ChainVisibilityTest'):
        ids = _chain(mongo_collection, visibilities, name)
        made.extend(ids)
        return ids
    yield make
    mongo_collection.delete_many({'_id': {'$in': made}})


@pytest.mark.integration
def test_opening_a_project_opens_its_old_versions(mongo_collection, chains):
    from caper.lineage import sync_chain_visibility
    ids = chains(['private', 'hidden_public', 'public'])

    assert sync_chain_visibility(mongo_collection, ids[-1]) == 2
    assert _visibility(mongo_collection, ids) == ['public'] * 3


@pytest.mark.integration
def test_making_a_project_private_closes_its_old_versions(mongo_collection, chains):
    from caper.lineage import sync_chain_visibility
    from caper.views_apis import _user_can_access_project
    ids = chains(['public', 'public', 'private'])
    assert _user_can_access_project(mongo_collection.find_one({'_id': ids[0]}), None)

    sync_chain_visibility(mongo_collection, ids[-1])

    assert _visibility(mongo_collection, ids) == ['private'] * 3
    for i in ids:
        assert not _user_can_access_project(mongo_collection.find_one({'_id': i}), None)


@pytest.mark.integration
def test_an_old_version_cannot_overwrite_the_project(mongo_collection, chains):
    from caper.lineage import sync_chain_visibility
    ids = chains(['public', 'private'])

    assert sync_chain_visibility(mongo_collection, ids[0]) == 0
    assert _visibility(mongo_collection, ids) == ['public', 'private']


@pytest.mark.integration
def test_the_value_passed_in_wins_over_a_stale_read(mongo_collection, chains):
    """A caller that just wrote the head passes the value, because the
    default handle may read a replica that has not seen the write yet."""
    from caper.lineage import sync_chain_visibility
    ids = chains(['private', 'private'])

    sync_chain_visibility(mongo_collection, ids[-1], 'public')
    assert _visibility(mongo_collection, ids[:1]) == ['public']


@pytest.mark.integration
def test_other_projects_are_left_alone(mongo_collection, chains):
    from caper.lineage import sync_chain_visibility
    mine = chains(['private', 'public'], name='ChainVisibilityMine')
    other = chains(['private', 'private'], name='ChainVisibilityOther')

    sync_chain_visibility(mongo_collection, mine[-1])
    assert _visibility(mongo_collection, other) == ['private', 'private']


@pytest.mark.integration
def test_an_unpointered_chain_falls_back_to_previous_versions(mongo_collection):
    from caper.lineage import sync_chain_visibility
    old = mongo_collection.insert_one({'project_name': 'ChainVisibilityLegacy',
                                       'private': 'private', 'delete': True,
                                       'current': False}).inserted_id
    head = mongo_collection.insert_one({'project_name': 'ChainVisibilityLegacy',
                                        'private': 'public', 'delete': False, 'current': True,
                                        'previous_versions': [{'linkid': str(old)}]}).inserted_id
    try:
        assert sync_chain_visibility(mongo_collection, head) == 1
        assert _visibility(mongo_collection, [old]) == ['public']
    finally:
        mongo_collection.delete_many({'_id': {'$in': [old, head]}})


# ---------------------------------------------------------------------------
# End to end, through the views, the same five steps reproduced on dev
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.integration
def test_every_version_follows_the_project_through_edits_and_uploads(
        request_factory, test_user, mongo_collection):
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.messages.storage.fallback import FallbackStorage
    from conftest import (_build_create_request, _cleanup_project,
                          _poll_until_finished, _project_id_from_redirect,
                          DATASET_SMALL_TAR)
    from caper import lineage, views

    name = 'ChainVisibilityE2E'
    created = []

    def post(path, data, files=None):
        request = request_factory.post(path, data={**data, **(files or {})}, format='multipart')
        request.user = test_user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def form(vis, mode=None):
        data = {'project_name': name, 'description': 'chain visibility', 'private': vis,
                'publication_link': '', 'project_members': '', 'alias': '',
                'remap_sample_names': 'false', 'accept_license': 'on'}
        if mode:
            data['project_mode'] = mode
        return data

    def members():
        doc = mongo_collection.find_one({'_id': ObjectId(created[0])}, lineage.POINTER_PROJECTION)
        return lineage.chain_members(mongo_collection, doc, dict(lineage.POINTER_PROJECTION, private=1))

    def new_version(vis):
        head = str(lineage.head(members())['_id'])
        # 'replace' with the archive attached: 'reaggregate' fetches the old
        # archive over HTTP from SITE_URL, which needs a running server.
        with open(DATASET_SMALL_TAR, 'rb') as fh:
            new_id = _project_id_from_redirect(views.edit_project_page(
                post(f'/project/{head}/edit', form(vis, 'replace'), {'document': fh}),
                project_name=head))
        assert new_id and new_id not in created, 'the upload did not mint a new version'
        created.append(new_id)
        doc = _poll_until_finished(mongo_collection, new_id)
        assert doc and not doc.get('aggregation_failed'), doc and doc.get('error_message')
        return new_id

    def edit_in_place(pid, vis):
        views.edit_project_page(post(f'/project/{pid}/edit', form(vis)), project_name=pid)

    def assert_all(vis):
        assert [m['private'] for m in members()] == [vis] * len(created)

    try:
        with open(DATASET_SMALL_TAR, 'rb') as fh:
            v1 = _project_id_from_redirect(views.create_project(
                post('/create-project/', form('private'), {'document': fh})))
        created.append(v1)
        assert not _poll_until_finished(mongo_collection, v1).get('aggregation_failed')

        v2 = new_version('private')
        assert_all('private')
        edit_in_place(v2, 'public')
        assert_all('public')
        v3 = new_version('public')
        assert_all('public')
        edit_in_place(v3, 'private')
        assert_all('private')

        request = request_factory.get(f'/project/{v2}')
        request.user = AnonymousUser()
        request.session = {}
        request._messages = FallbackStorage(request)
        response = views.project_page(request, project_name=v2)
        assert response.status_code == 302, 'every version of a private project is private'
    finally:
        for pid in created:
            _cleanup_project(mongo_collection, pid)
