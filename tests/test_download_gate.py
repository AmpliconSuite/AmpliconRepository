"""Tests for the download human-verification gate.

The gate is the mitigation for the 2026-08-09 outage, in which an anonymous
crawl of per-sample download URLs wedged every gunicorn worker.  The properties
that matter are all about what happens *before* any expensive work starts, so
these tests drive the decorator directly rather than going through the database.

See caper/download_gate.py for the rationale.
"""

import time

import pytest
from django.http import HttpResponse
from django.test import override_settings

from caper.download_gate import (
    DEFAULT_PASS_SECONDS,
    _INTERNAL_META_KEY,
    _internal_token,
    download_gate,
    grant_download_pass,
    has_download_pass,
    internal_download_headers,
    may_download,
    pass_duration_label,
    pass_seconds,
)


class _AnonUser:
    is_authenticated = False


class _LoggedInUser:
    is_authenticated = True


# django.test.RequestFactory sets REMOTE_ADDR to 127.0.0.1, which the gate
# treats as an internal self-call.  Default these requests to a public address
# so they exercise the external path; the loopback tests opt back in explicitly.
EXTERNAL_ADDR = '203.0.113.9'      # TEST-NET-3


def _make_request(request_factory, path='/project/abc/sample/S1/download',
                  user=None, session=None):
    req = request_factory.get(path, REMOTE_ADDR=EXTERNAL_ADDR)
    req.user = user if user is not None else _AnonUser()
    req.session = {} if session is None else session
    return req


@pytest.fixture
def sentinel_view():
    """A view that records whether it ran, standing in for a download."""
    state = {'ran': False}

    @download_gate
    def view(request, *args, **kwargs):
        state['ran'] = True
        return HttpResponse(b'payload')

    view.state = state
    return view


# ---------------------------------------------------------------------------
# Refusal path — the one that must stay cheap
# ---------------------------------------------------------------------------

def test_anonymous_request_is_redirected_and_view_never_runs(
        request_factory, sentinel_view):
    """The expensive view must not execute for an unverified visitor."""
    req = _make_request(request_factory)
    resp = sentinel_view(req)

    assert resp.status_code == 302, "unverified download should redirect"
    assert resp['Location'] == '/downloads/verify'
    assert sentinel_view.state['ran'] is False, \
        "download view ran despite the gate — no archive work may start here"


def test_refusal_records_where_to_return(request_factory, sentinel_view):
    session = {}
    req = _make_request(request_factory, path='/project/p1/sample/s1/download',
                        session=session)
    sentinel_view(req)

    assert session['download_next'] == '/project/p1/sample/s1/download'


@pytest.mark.parametrize('hostile', [
    '//evil.example.com/x',
    'https://evil.example.com/x',
])
def test_refusal_never_stores_an_offsite_return_target(
        request_factory, sentinel_view, hostile, monkeypatch):
    """An open redirect here would turn the gate into a phishing hop."""
    session = {}
    req = _make_request(request_factory, session=session)
    monkeypatch.setattr(req, 'get_full_path', lambda: hostile)
    sentinel_view(req)

    assert session['download_next'] == '/', \
        f"off-site target {hostile!r} was stored as a redirect destination"


# ---------------------------------------------------------------------------
# Admission paths
# ---------------------------------------------------------------------------

def test_authenticated_user_passes_without_a_captcha(
        request_factory, sentinel_view):
    req = _make_request(request_factory, user=_LoggedInUser())
    resp = sentinel_view(req)

    assert resp.status_code == 200
    assert sentinel_view.state['ran'] is True


def test_session_pass_admits_subsequent_downloads(
        request_factory, sentinel_view):
    """One solve covers later downloads, so a visitor is asked once."""
    session = {}
    granted = _make_request(request_factory, session=session)
    grant_download_pass(granted)

    req = _make_request(request_factory, session=session)
    resp = sentinel_view(req)

    assert resp.status_code == 200
    assert sentinel_view.state['ran'] is True


def test_expired_pass_is_refused(request_factory, sentinel_view):
    session = {'download_pass_until': time.time() - 1}
    req = _make_request(request_factory, session=session)
    resp = sentinel_view(req)

    assert resp.status_code == 302
    assert sentinel_view.state['ran'] is False


def test_pass_is_time_boxed_not_permanent():
    """A harvested session must not be an open door forever."""
    assert 0 < pass_seconds() <= 24 * 3600
    assert 0 < DEFAULT_PASS_SECONDS <= 24 * 3600


# ---------------------------------------------------------------------------
# Configurable pass duration
# ---------------------------------------------------------------------------

def test_configured_duration_is_honoured(request_factory):
    """DOWNLOAD_PASS_SECONDS in config.sh must actually shorten the pass.

    Bound at import instead of read per call, a short local value would look
    configured while the hour-long default stayed in force.
    """
    with override_settings(DOWNLOAD_PASS_SECONDS=60):
        assert pass_seconds() == 60

        session = {}
        grant_download_pass(_make_request(request_factory, session=session))
        granted_for = session['download_pass_until'] - time.time()
        assert 55 < granted_for <= 60


@pytest.mark.parametrize('configured, expected', [
    ('120', 120),          # config.sh exports strings, never ints
    (0, 30),               # clamped up: every download re-challenging is worse
    (-1, 30),              #   for humans than for crawlers
    (10 ** 9, 24 * 3600),  # clamped down: a pass must not outlive the session
    ('banana', DEFAULT_PASS_SECONDS),   # a bad env var must not 500 downloads
    (None, DEFAULT_PASS_SECONDS),
])
def test_duration_is_validated(configured, expected):
    with override_settings(DOWNLOAD_PASS_SECONDS=configured):
        assert pass_seconds() == expected


@pytest.mark.parametrize('configured, label', [
    (3600, '1 hour'),
    (7200, '2 hours'),
    (300, '5 minutes'),
    (60, '1 minute'),
    (45, '45 seconds'),
])
def test_label_matches_what_is_enforced(configured, label):
    """The challenge page must not promise a window that is not applied."""
    with override_settings(DOWNLOAD_PASS_SECONDS=configured):
        assert pass_duration_label() == label


@pytest.mark.parametrize('junk', ['not-a-number', None, {}])
def test_corrupt_session_value_is_refused_not_crashed(request_factory, junk):
    """A malformed cookie should close the gate, never raise."""
    req = _make_request(request_factory, session={'download_pass_until': junk})
    assert has_download_pass(req) is False


def test_missing_session_is_refused_not_crashed(request_factory):
    """Views called directly in tests have no session attached."""
    req = request_factory.get('/project/p/sample/s/download',
                              REMOTE_ADDR=EXTERNAL_ADDR)
    req.user = _AnonUser()
    assert has_download_pass(req) is False
    assert may_download(req) is False


# ---------------------------------------------------------------------------
# Internal loopback exemption
# ---------------------------------------------------------------------------

def test_internal_token_admits_the_self_call(request_factory, sentinel_view):
    """Project re-aggregation refetches the old tarball from its own URL.

    Without this the challenge page is written to disk as download.tar.gz and
    the old project data is silently dropped from the re-aggregated project.
    """
    req = _make_request(request_factory)
    req.META[_INTERNAL_META_KEY] = _internal_token()

    resp = sentinel_view(req)
    assert resp.status_code == 200
    assert sentinel_view.state['ran'] is True


def test_wrong_internal_token_is_refused(request_factory, sentinel_view):
    req = _make_request(request_factory)
    req.META[_INTERNAL_META_KEY] = 'a' * 64

    resp = sentinel_view(req)
    assert resp.status_code == 302
    assert sentinel_view.state['ran'] is False


def test_loopback_address_alone_does_not_admit(request_factory, sentinel_view):
    """Browsing http://localhost:8000 must see the gate, exactly as prod does.

    An address-based exemption would make the gate invisible during local
    development, which is where it gets tested.
    """
    req = _make_request(request_factory)
    req.META['REMOTE_ADDR'] = '127.0.0.1'

    resp = sentinel_view(req)
    assert resp.status_code == 302, \
        "loopback address alone admitted a download — the gate is inactive locally"
    assert sentinel_view.state['ran'] is False


def test_internal_token_is_only_sent_to_this_application():
    """The token must never be attached to a third-party URL."""
    from caper.download_gate import INTERNAL_HEADER_NAME

    local = internal_download_headers('http://localhost:8000/project/x/download')
    assert INTERNAL_HEADER_NAME in local

    for foreign in ('https://evil.example.com/project/x/download',
                    'http://169.254.169.254/latest/meta-data/'):
        assert internal_download_headers(foreign) == {}, \
            f"internal token would leak to {foreign}"


# ---------------------------------------------------------------------------
# Which routes are gated
# ---------------------------------------------------------------------------

def test_expensive_download_routes_are_gated():
    from caper import views
    for name in ('sample_download', 'sample_metadata_download',
                 'project_download', 'project_summary_download',
                 'project_metadata_download', 'feature_download',
                 'pdf_download'):
        view = getattr(views, name)
        assert getattr(view, 'download_gated', False), f"{name} is not gated"


def test_png_download_is_not_gated():
    """The sample page embeds these as <img> sources.

    Gating them would redirect every thumbnail to the challenge page and break
    the rendered page rather than protect it.
    """
    from caper import views
    assert not getattr(views.png_download, 'download_gated', False), \
        "png_download is gated — inline feature images on the sample page will break"


def test_every_project_download_route_is_gated():
    """No download route under /project/ may be reachable unverified.

    Enumerated from the URL conf rather than a hand-written list, so a new
    download route added later fails this test instead of quietly shipping open.
    png_download is the single documented exception.
    """
    from caper import urls

    open_routes = []
    for pattern in urls.urlpatterns:
        callback = getattr(pattern, 'callback', None)
        if callback is None:
            continue
        route = str(pattern.pattern)
        if not route.startswith('project/') or '/download' not in route:
            continue
        if getattr(callback, '__name__', '') == 'png_download':
            continue
        if not getattr(callback, 'download_gated', False):
            open_routes.append(route)

    assert not open_routes, f"ungated download routes: {open_routes}"
