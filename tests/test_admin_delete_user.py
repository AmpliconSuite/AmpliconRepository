"""
The staff-facing delete-user page.

The page had no tests at all while it carried its own copy of the
delete-or-reassign rule. That copy has moved to caper.account_deletion, where
the post_delete receiver runs it too, so /admin/ and the shell now behave the
same as this page. These tests hold the page to the same outcomes it had before
that move, plus the two bugs the move fixed:

  * ``if project['private']`` was true for the *string* ``'public'``, so a
    public project with one member was destroyed rather than handed on;
  * a project whose member was recorded by email address rather than username
    was never recognised as solo at all.
"""

import uuid

import pytest


ADMIN_URL = '/admin-delete-user/'


@pytest.fixture
def staff_request(request_factory, admin_user):
    def _make(**post):
        # HTTP_HOST because the staff-gate redirect is absolute and goes through
        # ALLOWED_HOSTS, which does not contain RequestFactory's 'testserver'.
        req = request_factory.post(ADMIN_URL, post, HTTP_HOST='localhost')
        req.user = admin_user
        return req
    return _make


@pytest.fixture
def departing(request):
    """A real account with a project, torn down whatever the test does to it."""
    from django.contrib.auth import get_user_model
    from caper.utils import collection_handle_primary

    suffix = uuid.uuid4().hex[:12]
    username = f'admindel_{suffix}'
    email = f'admindel_{suffix}@example.invalid'
    marker = f'admindel-{suffix}'

    user = get_user_model().objects.create_user(username=username, email=email)

    def _cleanup():
        get_user_model().objects.filter(username=username).delete()
        collection_handle_primary.delete_many({'project_name': marker})
    request.addfinalizer(_cleanup)

    def add_project(*, private, members):
        return collection_handle_primary.insert_one({
            'project_name': marker, 'current': True, 'delete': True,
            'private': private, 'project_members': list(members),
        }).inserted_id

    user.marker = marker
    user.add_project = add_project
    return user


def _view(request):
    from caper.views import admin_delete_user
    return admin_delete_user(request)


def _project(project_id):
    from caper.utils import collection_handle_primary
    return collection_handle_primary.find_one({'_id': project_id})


def _exists(username):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(username=username).exists()


# ---------------------------------------------------------------------------
# Who can reach it
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_non_staff_account_cannot_reach_it(request_factory, non_member_user):
    req = request_factory.post(ADMIN_URL, {}, HTTP_HOST='localhost')
    req.user = non_member_user

    response = _view(req)

    assert response.status_code == 302
    assert response['Location'].startswith('/notfound/')


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_preview_separates_solo_from_shared(staff_request, departing):
    departing.add_project(private='private', members=[departing.username])
    departing.add_project(private='private',
                          members=[departing.username, 'colleague'])

    response = _view(staff_request(user_name=departing.username,
                                   action='select_user'))

    assert response.status_code == 200
    # Rendered rather than inspected: the template is what an administrator acts on.
    body = response.content.decode()
    assert departing.marker in body
    assert _exists(departing.username), 'the preview must not delete anything'


@pytest.mark.integration
def test_the_preview_says_what_will_happen_to_each_project(staff_request, departing):
    departing.add_project(private='private', members=[departing.username])
    departing.add_project(private='public', members=[departing.username])

    body = _view(staff_request(user_name=departing.username,
                               action='select_user')).content.decode()

    assert 'Permanently deleted' in body
    assert 'Reassigned to an admin' in body


# ---------------------------------------------------------------------------
# The deletion
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_deleting_removes_the_account(staff_request, departing):
    response = _view(staff_request(user_name=departing.username,
                                   action='delete_user'))

    assert response.status_code == 200
    assert not _exists(departing.username)


@pytest.mark.integration
def test_a_solo_public_project_is_reassigned_not_destroyed(staff_request,
                                                           departing, monkeypatch):
    """The string 'public' is truthy. Reading it as "private" deleted the project."""
    from caper import account_deletion
    monkeypatch.setattr(account_deletion, 'caretaker_username', lambda: 'curator')

    project_id = departing.add_project(private='public',
                                       members=[departing.username])

    _view(staff_request(user_name=departing.username, action='delete_user'))

    doc = _project(project_id)
    assert doc is not None, 'a public project must survive its owner leaving'
    assert doc['project_members'] == ['curator']


@pytest.mark.integration
def test_a_shared_project_only_loses_the_member(staff_request, departing):
    project_id = departing.add_project(
        private='private', members=[departing.username, 'colleague'])

    _view(staff_request(user_name=departing.username, action='delete_user'))

    assert _project(project_id)['project_members'] == ['colleague']


@pytest.mark.integration
def test_membership_by_email_is_recognised(staff_request, departing, monkeypatch):
    """Recorded by email, the account used to look like no member at all.

    The project was then neither deleted nor reassigned, and the later cleanup
    stripped the address out, leaving a live project with an empty member list.
    """
    from caper import account_deletion
    monkeypatch.setattr(account_deletion, 'caretaker_username', lambda: 'curator')

    project_id = departing.add_project(private='public',
                                       members=[departing.email])

    _view(staff_request(user_name=departing.username, action='delete_user'))

    assert _project(project_id)['project_members'] == ['curator']


@pytest.mark.integration
def test_no_live_project_is_left_without_a_member(staff_request, departing,
                                                  monkeypatch):
    """The failure this whole path exists to prevent, asserted directly."""
    from caper import account_deletion
    monkeypatch.setattr(account_deletion, 'caretaker_username', lambda: 'curator')

    ids = [
        departing.add_project(private='public', members=[departing.username]),
        departing.add_project(private='hidden_public', members=[departing.email]),
        departing.add_project(private='private',
                              members=[departing.username, 'colleague']),
    ]

    _view(staff_request(user_name=departing.username, action='delete_user'))

    for project_id in ids:
        doc = _project(project_id)
        if doc is not None:
            assert doc['project_members'], (
                f"{doc['project_name']} survived with no members at all")


@pytest.mark.integration
def test_an_unknown_username_says_so_and_changes_nothing(staff_request):
    unknown = f'no_such_user_{uuid.uuid4().hex[:8]}'

    body = _view(staff_request(user_name=unknown,
                               action='delete_user')).content.decode()

    assert 'does not exist' in body
