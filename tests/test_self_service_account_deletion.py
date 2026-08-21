"""
Closing your own account from the settings page.

Until now the only way to have an account removed was to email someone and have
them do it from the admin page. This is that, self-service -- which means the
consent step has to carry the weight the administrator's judgement used to:
the page has to say which projects get destroyed, and the destruction must not
be reachable by accident.

The tests go after the ways that can quietly fail:

  * a GET that deletes -- a link-prefetching browser extension would then be
    enough to close someone's account;
  * a POST that deletes without the typed confirmation;
  * a confirmation page whose listing disagrees with what the deletion does,
    which would be consent to the wrong thing;
  * a session that survives its own account.
"""

import uuid

import pytest


CONFIRM_URL = '/accounts/delete/'


@pytest.fixture
def throwaway_user(request):
    """A real Django account, since this is about really deleting one.

    Named with a random suffix so it cannot collide with a member string in the
    shared development database -- an account whose username matches a real
    project member would take that project down with it when deleted, which is
    correct behaviour and a ruinous test fixture.
    """
    from django.contrib.auth import get_user_model

    suffix = uuid.uuid4().hex[:12]
    user = get_user_model().objects.create_user(
        username=f'selfdelete_{suffix}',
        email=f'selfdelete_{suffix}@example.invalid',
        password='not-a-real-password')

    def _cleanup():
        get_user_model().objects.filter(pk=user.pk).delete()
    request.addfinalizer(_cleanup)

    return user


@pytest.fixture
def client():
    from django.test import Client
    # SERVER_NAME because the client's default host, 'testserver', is not in
    # ALLOWED_HOSTS -- every request would come back as a 400 before reaching a view.
    return Client(SERVER_NAME='localhost')


@pytest.fixture
def signed_in(client, throwaway_user):
    client.force_login(throwaway_user)
    return client


def _exists(user):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(pk=user.pk).exists()


# ---------------------------------------------------------------------------
# Reaching the page
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_anonymous_visitor_is_sent_to_the_login_page(client):
    response = client.get(CONFIRM_URL)

    assert response.status_code == 302
    assert '/accounts/login/' in response['Location']


@pytest.mark.integration
def test_the_settings_page_links_to_it(signed_in):
    response = signed_in.get('/accounts/settings/')

    assert response.status_code == 200
    assert CONFIRM_URL in response.content.decode()


@pytest.mark.integration
def test_the_confirmation_page_names_the_account(signed_in, throwaway_user):
    response = signed_in.get(CONFIRM_URL)

    assert response.status_code == 200
    body = response.content.decode()
    assert throwaway_user.username in body
    assert 'cannot be undone' in body.lower()


# ---------------------------------------------------------------------------
# Nothing is destroyed without an unambiguous request
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_get_never_deletes(signed_in, throwaway_user):
    """A prefetching browser or a crawler must not be able to close an account."""
    signed_in.get(CONFIRM_URL)

    assert _exists(throwaway_user)


@pytest.mark.integration
def test_a_post_without_the_typed_username_deletes_nothing(signed_in, throwaway_user):
    response = signed_in.post(CONFIRM_URL, {'confirm_username': ''})

    assert response.status_code == 200
    assert _exists(throwaway_user)


@pytest.mark.integration
def test_a_mistyped_username_deletes_nothing(signed_in, throwaway_user):
    response = signed_in.post(CONFIRM_URL,
                              {'confirm_username': throwaway_user.username.upper()})

    assert response.status_code == 200
    assert _exists(throwaway_user)


@pytest.mark.integration
def test_a_forged_post_is_rejected(throwaway_user):
    """Without CSRF enforcement, any page on the internet could close the account.

    The default test client skips CSRF checks, which is exactly the thing being
    tested here, so this one is built with them switched on.
    """
    from django.test import Client

    client = Client(enforce_csrf_checks=True, SERVER_NAME='localhost')
    client.force_login(throwaway_user)

    response = client.post(CONFIRM_URL,
                           {'confirm_username': throwaway_user.username})

    assert response.status_code == 403
    assert _exists(throwaway_user)


# ---------------------------------------------------------------------------
# The deletion itself
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_confirming_deletes_the_account(signed_in, throwaway_user):
    response = signed_in.post(CONFIRM_URL,
                              {'confirm_username': throwaway_user.username})

    assert response.status_code == 302
    assert not _exists(throwaway_user)


@pytest.mark.integration
def test_the_session_does_not_outlive_the_account(signed_in, throwaway_user):
    """A session pointing at a deleted primary key is a 500 waiting to happen."""
    signed_in.post(CONFIRM_URL, {'confirm_username': throwaway_user.username})

    response = signed_in.get('/accounts/settings/')

    assert response.status_code == 302
    assert '/accounts/login/' in response['Location']


@pytest.mark.integration
def test_the_api_token_goes_with_it(signed_in, throwaway_user):
    from rest_framework.authtoken.models import Token

    key = Token.objects.create(user=throwaway_user).key

    signed_in.post(CONFIRM_URL, {'confirm_username': throwaway_user.username})

    assert not Token.objects.filter(key=key).exists()


# ---------------------------------------------------------------------------
# What the page promises about the projects
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_page_lists_the_projects_it_will_destroy(request, signed_in,
                                                     throwaway_user):
    """The warning has to be specific, or it is consent to something unread."""
    from caper.utils import collection_handle_primary

    marker_private = f'selfdelete-private-{uuid.uuid4().hex[:8]}'
    marker_shared = f'selfdelete-shared-{uuid.uuid4().hex[:8]}'
    names = [marker_private, marker_shared]

    collection_handle_primary.insert_one({
        'project_name': marker_private, 'current': True, 'delete': True,
        'private': 'private', 'project_members': [throwaway_user.username],
    })
    collection_handle_primary.insert_one({
        'project_name': marker_shared, 'current': True, 'delete': True,
        'private': 'private',
        'project_members': [throwaway_user.username, 'colleague'],
    })
    request.addfinalizer(
        lambda: collection_handle_primary.delete_many(
            {'project_name': {'$in': names}}))

    body = signed_in.get(CONFIRM_URL).content.decode()

    assert marker_private in body
    assert marker_shared in body
    # The destructive one has to be under the heading that says so, not merely
    # mentioned somewhere on the page.
    deleted_section = body.split('Deleted permanently', 1)[1]
    assert marker_private in deleted_section.split('Kept', 1)[0]
