"""Tests for the development-server landing gate (caper/dev_gate.py).

The gate exists so that someone who follows an old link, edits the production
URL, or arrives from a search result does not mistake the development server
for the repository.  Measured 2026-09-03: a `site:dev.ampliconrepository.org`
search returned the dev home page and a dev project page, and dev was serving
production's crawl-permitting robots.txt.

What these tests pin down is mostly the gate's *scope*, because the scope is
the part that is easy to widen by accident:

* it does nothing at all unless DEV_GATE_ENABLED is set, so production and an
  ordinary localhost checkout are untouched;
* it gates the home page and nothing else -- a deep link is deliberately let
  through (site owner's call: the wanderer arrives at `/`, and gating every
  path would mean holding a `next` target and breaking API and OAuth traffic
  for a problem that is not a security boundary);
* admission sets one cookie and no session, so anonymous-user behaviour stays
  testable on dev.

These drive the middleware directly.  Nothing here touches MongoDB: a request
the gate turns away must not reach the database, and a test that needed one
would not be able to prove that.
"""

import pytest
from django.core import signing
from django.http import HttpResponse
from django.test import override_settings

from caper.dev_gate import (
    COOKIE_NAME,
    DevGateMiddleware,
    gate_enabled,
    issue_pass,
    pass_is_valid,
)


HOST = 'localhost'
PASSPHRASE = 'amprepodev'

# Enable the gate with a known passphrase.  Applied per-test rather than
# globally so the "disabled by default" tests exercise the real default.
enabled = override_settings(
    DEV_GATE_ENABLED=True,
    DEV_GATE_PASSPHRASE=PASSPHRASE,
    DEV_GATE_MAX_AGE_SECONDS=7 * 24 * 3600,
)


@pytest.fixture
def downstream():
    """A stand-in for the rest of the application, recording whether it ran."""
    state = {'ran': False}

    def get_response(request):
        state['ran'] = True
        return HttpResponse('APPLICATION BODY')

    get_response.state = state
    return get_response


@pytest.fixture
def middleware(downstream):
    return DevGateMiddleware(downstream), downstream.state


def _get(request_factory, path='/', cookie=None, **extra):
    request = request_factory.get(path, HTTP_HOST=HOST, **extra)
    if cookie is not None:
        request.COOKIES[COOKIE_NAME] = cookie
    return request


def _post(request_factory, code, path='/', cookie=None):
    request = request_factory.post(path, {'code': code}, HTTP_HOST=HOST)
    if cookie is not None:
        request.COOKIES[COOKIE_NAME] = cookie
    return request


# ---------------------------------------------------------------------------
# Disabled by default: production and localhost are unchanged
# ---------------------------------------------------------------------------

def test_the_gate_is_off_unless_it_is_explicitly_enabled():
    """The deployment opts in.  Nothing about the host decides this.

    Keying on DEV_GATE_ENABLED rather than on the dev hostname is deliberate:
    a hostname test cannot be exercised on a laptop, and it puts a domain name
    in the source where a deployment setting belongs.
    """
    assert gate_enabled() is False


def test_a_disabled_gate_passes_the_home_page_through(request_factory, middleware):
    mw, state = middleware
    response = mw(_get(request_factory, '/'))

    assert state['ran'] is True
    assert response.content == b'APPLICATION BODY'


def test_a_disabled_gate_adds_no_crawler_headers(request_factory, middleware):
    """Production must not start advertising noindex because this shipped."""
    mw, _ = middleware
    response = mw(_get(request_factory, '/'))

    assert 'X-Robots-Tag' not in response


# ---------------------------------------------------------------------------
# Turning a visitor away
# ---------------------------------------------------------------------------

@enabled
def test_the_home_page_is_replaced_for_a_visitor_with_no_pass(request_factory, middleware):
    mw, state = middleware
    response = mw(_get(request_factory, '/'))

    assert state['ran'] is False, 'the application must not run for a gated visitor'
    assert response.status_code == 200
    assert b'APPLICATION BODY' not in response.content


@enabled
def test_the_landing_page_sends_the_visitor_to_production(request_factory, middleware):
    mw, _ = middleware
    body = mw(_get(request_factory, '/')).content.decode()

    assert 'https://ampliconrepository.org' in body


@enabled
def test_the_landing_page_links_the_wiki_for_developer_access(request_factory, middleware):
    """The passphrase lives in the wiki, not on the page that asks for it."""
    mw, _ = middleware
    body = mw(_get(request_factory, '/')).content.decode()

    assert 'wiki/Development-Server' in body
    assert PASSPHRASE not in body


@enabled
def test_the_landing_page_is_not_cached(request_factory, middleware):
    mw, _ = middleware
    response = mw(_get(request_factory, '/'))

    assert response['Cache-Control'] == 'no-store'


# ---------------------------------------------------------------------------
# Scope: the home page, and only the home page
# ---------------------------------------------------------------------------

@enabled
@pytest.mark.parametrize('path', [
    '/project/661095ec16263a58ab871814',
    '/project/661095ec16263a58ab871814/sample/S1',
    '/api/v1/projects/',
    '/accounts/login/',
    '/healthz',
])
def test_a_deep_link_is_deliberately_not_gated(path, request_factory, middleware):
    """Site owner's call 2026-09-03: the home page gate is just that.

    Someone who requests a page behind the home page reaches it.  That is the
    rarer arrival and the one worth less friction, and leaving these paths
    alone is what keeps the API, the OAuth callbacks and weekly-report.py
    working with no exemption list to maintain.
    """
    mw, state = middleware
    response = mw(_get(request_factory, path))

    assert state['ran'] is True
    assert response.content == b'APPLICATION BODY'


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

@enabled
def test_the_right_passphrase_admits_and_sets_a_pass(request_factory, middleware):
    mw, state = middleware
    response = mw(_post(request_factory, PASSPHRASE))

    assert state['ran'] is False, 'the POST is answered by a redirect, not the app'
    assert response.status_code == 302
    assert response['Location'] == '/'
    assert pass_is_valid(response.cookies[COOKIE_NAME].value)


@enabled
@pytest.mark.parametrize('typed', [
    '  amprepodev  ',   # copied out of the wiki with whitespace
    'AmpRepoDev',       # typed with the capitalisation people expect
])
def test_the_passphrase_is_forgiving_about_typing(typed, request_factory, middleware):
    """It is a shibboleth, not a credential; a stray space must not stop a dev."""
    mw, _ = middleware
    response = mw(_post(request_factory, typed))

    assert response.status_code == 302


@enabled
def test_a_valid_pass_lets_the_home_page_through(request_factory, middleware):
    mw, state = middleware
    response = mw(_get(request_factory, '/', cookie=issue_pass()))

    assert state['ran'] is True
    assert response.content == b'APPLICATION BODY'


@enabled
def test_admission_creates_no_login_session(request_factory, middleware):
    """Passing the gate must leave request.user anonymous.

    Anonymous behaviour -- public project pages, private-project redirects,
    signup, the OAuth flows -- is most of what dev is used to test, so the gate
    is not allowed to authenticate anyone.  It runs above SessionMiddleware, so
    the only thing it can set is its own cookie; this asserts that stays true.
    """
    mw, _ = middleware
    response = mw(_post(request_factory, PASSPHRASE))

    assert list(response.cookies) == [COOKIE_NAME]


@enabled
def test_the_pass_survives_an_oauth_redirect_back_from_google(request_factory, middleware):
    """SameSite=Lax, so a top-level redirect back from Google or Globus keeps it."""
    mw, _ = middleware
    cookie = mw(_post(request_factory, PASSPHRASE)).cookies[COOKIE_NAME]

    assert cookie['samesite'] == 'Lax'
    assert cookie['httponly'] is True


# ---------------------------------------------------------------------------
# Refusing a bad pass
# ---------------------------------------------------------------------------

@enabled
def test_a_wrong_passphrase_returns_the_landing_page_with_an_error(request_factory, middleware):
    mw, state = middleware
    response = mw(_post(request_factory, 'opensesame'))

    assert state['ran'] is False
    assert response.status_code == 200
    assert COOKIE_NAME not in response.cookies
    assert 'wiki/Development-Server' in response.content.decode()


@enabled
@pytest.mark.parametrize('hostile', [
    'not-signed-at-all',
    '',
    signing.dumps('ok', key='a-different-signing-key'),
])
def test_a_forged_pass_is_refused(hostile, request_factory, middleware):
    mw, state = middleware
    response = mw(_get(request_factory, '/', cookie=hostile))

    assert state['ran'] is False
    assert pass_is_valid(hostile) is False


@enabled
def test_a_pass_expires(request_factory, middleware):
    """A pass is time-boxed so a stale laptop does not stay admitted forever."""
    mw, state = middleware
    cookie = issue_pass()

    with override_settings(DEV_GATE_MAX_AGE_SECONDS=-1):
        assert pass_is_valid(cookie) is False
        assert mw(_get(request_factory, '/', cookie=cookie)).status_code == 200
        assert state['ran'] is False


@enabled
def test_leaving_clears_the_pass(request_factory, middleware):
    """The 'leave' action drops the cookie and shows the landing page again."""
    mw, _ = middleware
    request = request_factory.post('/', {'leave': '1'}, HTTP_HOST=HOST)
    request.COOKIES[COOKIE_NAME] = issue_pass()
    response = mw(request)

    assert response.status_code == 302
    assert response.cookies[COOKIE_NAME].value == ''


# ---------------------------------------------------------------------------
# Incomplete configuration
# ---------------------------------------------------------------------------

@override_settings(DEV_GATE_ENABLED=True, DEV_GATE_PASSPHRASE='')
def test_an_unset_passphrase_admits_nobody(request_factory, middleware):
    """Fail closed: a blank setting must not become a blank password."""
    mw, state = middleware
    response = mw(_post(request_factory, ''))

    assert state['ran'] is False
    assert COOKIE_NAME not in response.cookies


# ---------------------------------------------------------------------------
# Crawlers
# ---------------------------------------------------------------------------

@enabled
@pytest.mark.parametrize('path', ['/', '/project/661095ec16263a58ab871814'])
def test_every_dev_response_tells_crawlers_to_stay_out(path, request_factory, middleware):
    """The header, not the gate, is what keeps dev out of search results.

    A crawler that already holds a dev deep link never asks for `/`, so a
    home-page gate alone would not have removed the indexed project page found
    on 2026-09-03.  This header is stamped on the way out of every response.
    """
    mw, _ = middleware
    response = mw(_get(request_factory, path))

    assert response['X-Robots-Tag'] == 'noindex, nofollow, noarchive'
