"""
CSRF protection on /api/v1/token/.

The endpoint manages the caller's personal API token from a logged-in browser
session. It used to subclass SessionAuthentication with enforce_csrf() stubbed
out, reasoning that a forged cross-origin POST could not read the response and
so gained the attacker nothing. That is true of the response and beside the
point: POST *rotates* the token and DELETE revokes it, so a forged request
silently breaks whatever scripts and pipelines the victim has using the old one.

These tests pin the three behaviours that matter: no session, no access; an
unsafe method without a CSRF token is refused; and the real UI flow still works.
"""

import re
import uuid
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse


pytestmark = pytest.mark.integration

TOKEN_URL = '/api/v1/token/'


@pytest.fixture
def token_user():
    from django.contrib.auth import get_user_model

    suffix = uuid.uuid4().hex[:8]
    user = get_user_model().objects.create_user(
        username=f'csrf_test_{suffix}',
        email=f'csrf_test_{suffix}@example.com',
        password='TokenTest!12345')
    try:
        yield user
    finally:
        user.delete()


@pytest.fixture
def csrf_client():
    """A client that enforces CSRF, i.e. behaves like a real browser."""
    return Client(HTTP_HOST='localhost', enforce_csrf_checks=True)


def _csrf_token(client):
    """Fetch a page that renders {% csrf_token %} and read the resulting cookie."""
    client.get(reverse('user_settings'))
    return client.cookies['csrftoken'].value


# ---------------------------------------------------------------------------
# 1. No session, no token management
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['get', 'post', 'delete'])
def test_unauthenticated_requests_are_refused(csrf_client, method):
    response = getattr(csrf_client, method)(TOKEN_URL)

    assert response.status_code in (401, 403)


def test_unauthenticated_post_creates_no_token(csrf_client):
    from rest_framework.authtoken.models import Token

    before = Token.objects.count()
    csrf_client.post(TOKEN_URL)

    assert Token.objects.count() == before


# ---------------------------------------------------------------------------
# 2. Session-authenticated state changes require a CSRF token
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['post', 'delete'])
def test_state_changing_request_without_csrf_token_is_refused(
        csrf_client, token_user, method):
    csrf_client.force_login(token_user)

    response = getattr(csrf_client, method)(TOKEN_URL)

    assert response.status_code == 403


def test_forged_post_does_not_rotate_an_existing_token(csrf_client, token_user):
    """The concrete damage the old configuration allowed."""
    from rest_framework.authtoken.models import Token

    original = Token.objects.create(user=token_user).key
    csrf_client.force_login(token_user)

    response = csrf_client.post(TOKEN_URL)

    assert response.status_code == 403
    assert Token.objects.get(user=token_user).key == original


def test_forged_delete_does_not_revoke_a_token(csrf_client, token_user):
    from rest_framework.authtoken.models import Token

    Token.objects.create(user=token_user)
    csrf_client.force_login(token_user)

    response = csrf_client.delete(TOKEN_URL)

    assert response.status_code == 403
    assert Token.objects.filter(user=token_user).exists()


def test_get_is_allowed_without_a_csrf_token(csrf_client, token_user):
    """Reading status is a safe method, so it must not need one.

    The settings page fetches this on load, before the user has done anything.
    """
    csrf_client.force_login(token_user)

    response = csrf_client.get(TOKEN_URL)

    assert response.status_code == 200
    assert response.json()['has_token'] is False


# ---------------------------------------------------------------------------
# 3. The real UI flow still works
# ---------------------------------------------------------------------------

def test_generate_then_revoke_with_a_valid_csrf_token(csrf_client, token_user):
    from rest_framework.authtoken.models import Token

    csrf_client.force_login(token_user)
    csrf_token = _csrf_token(csrf_client)

    created = csrf_client.post(TOKEN_URL, HTTP_X_CSRFTOKEN=csrf_token)
    assert created.status_code == 201
    key = created.json()['token']
    assert Token.objects.get(user=token_user).key == key

    status = csrf_client.get(TOKEN_URL)
    assert status.json() == {'has_token': True, 'token_suffix': key[-8:]}

    revoked = csrf_client.delete(TOKEN_URL, HTTP_X_CSRFTOKEN=csrf_token)
    assert revoked.status_code == 200
    assert not Token.objects.filter(user=token_user).exists()


def test_settings_page_supplies_a_csrf_token_to_the_javascript(
        csrf_client, token_user):
    """The JS reads a token from the page; if the page stops rendering one the
    generate and revoke buttons would start 403ing."""
    csrf_client.force_login(token_user)

    body = csrf_client.get(reverse('user_settings')).content.decode()

    assert 'name="csrfmiddlewaretoken"' in body
    assert 'X-CSRFToken' in body


def test_token_javascript_prefers_the_rendered_field_over_the_cookie():
    source = (Path(__file__).resolve().parents[1] / 'caper' / 'templates' /
              'pages' / 'settings.html').read_text()

    getter = re.search(r'function getCsrfToken\(\) \{(.*?)\n    \}', source, re.DOTALL)
    assert getter, 'getCsrfToken() not found'

    body = getter.group(1)
    assert 'csrfmiddlewaretoken' in body
    assert body.index('csrfmiddlewaretoken') < body.index('document.cookie')


# ---------------------------------------------------------------------------
# 4. Token-authenticated API clients are unaffected
# ---------------------------------------------------------------------------

def test_token_authenticated_api_requests_still_work(token_user):
    """The CSRF change applies to session auth; API clients send no cookies.

    /api/v1/projects/ is the read endpoint those clients actually use, and it
    authenticates by token rather than session.
    """
    from rest_framework.authtoken.models import Token

    key = Token.objects.create(user=token_user).key
    client = Client(HTTP_HOST='localhost', enforce_csrf_checks=True)

    response = client.get('/api/v1/projects/', HTTP_AUTHORIZATION=f'Token {key}')

    assert response.status_code == 200


def test_token_auth_cannot_manage_the_token_itself(token_user):
    """Unchanged by this work, and worth keeping that way: a stolen token must
    not be usable to mint its own replacement."""
    from rest_framework.authtoken.models import Token

    key = Token.objects.create(user=token_user).key
    client = Client(HTTP_HOST='localhost', enforce_csrf_checks=True)

    response = client.post(TOKEN_URL, HTTP_AUTHORIZATION=f'Token {key}')

    assert response.status_code in (401, 403)
    assert Token.objects.get(user=token_user).key == key
