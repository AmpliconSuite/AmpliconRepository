"""Human-verification gate for the download routes.

Why this exists
---------------
The download endpoints are the most expensive work the site does: a per-sample
download gathers files into a temporary directory, zips them, and reads the
whole archive into memory before responding.  On 2026-08-09 a dispersed crawl
of roughly 110 req/min -- well under the rate the site handles comfortably --
wedged all nine gunicorn workers by walking those URLs across a 2,471-sample
project.

The load shedder in ``middleware.py`` cannot prevent that.  It releases its slot
when the view returns, and the worker then blocks writing the response body to
the client; a capture during the 2026-08-07 outage found every worker sitting in
``write(gunicorn/http/wsgi.py:346)``.  During the 08-09 collapse the shedder
reported ``in flight: 2/6 total`` -- its cap was never reached, because the
expensive part happens after the accounting ends.

So the fix is to stop anonymous clients reaching this work in bulk, rather than
to limit concurrency once they have.

Design notes
------------
* **The gate is on the endpoint, not the button.**  A crawler never clicks
  anything; graying out a link protects nothing.  A disabled button is the
  cosmetic half of this and lives in the template.

* **Refusal must be cheaper than service.**  An unverified request costs one
  session read and a redirect -- no database query, no GridFS read, no call to
  Google.  A verification only talks to reCAPTCHA when a human submits the form.

* **One solve per session, not per download.**  Someone fetching twenty samples
  should not answer twenty challenges.  The pass is time-boxed rather than
  permanent so a shared or stolen session does not become an open door.

* **Authenticated users skip it entirely.**  They already cleared a signup
  captcha, and an account is a stronger signal than a checkbox.
"""

import hashlib
import hmac
import logging
import time
from functools import wraps
from urllib.parse import urlparse

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)

# How long one solved challenge is honoured.  Long enough that a working
# session is not interrupted, short enough that a pass is not worth harvesting.
DEFAULT_PASS_SECONDS = 3600

# Bounds on the configured value.  The floor keeps a typo (or a 0) from making
# every single download re-challenge, which is worse for humans than for
# crawlers; the ceiling keeps a pass from outliving the session that earned it.
_MIN_PASS_SECONDS = 30
_MAX_PASS_SECONDS = 24 * 3600

_SESSION_KEY = 'download_pass_until'

# Cap on the stored ``next`` target.  Only same-site paths are ever honoured
# (see _safe_next), but the length bound keeps a hostile client from stuffing
# the session cookie.
_MAX_NEXT_LENGTH = 512


def pass_seconds():
    """How long a solved challenge is honoured, in seconds.

    Read from settings on every call rather than bound at import, so
    ``DOWNLOAD_PASS_SECONDS`` in config.sh (or override_settings in a test) is
    actually in force.  Set it to something short locally to exercise expiry
    without waiting an hour::

        export DOWNLOAD_PASS_SECONDS=60

    A missing or unparseable value falls back to the default rather than
    raising: a bad env var should not take the download routes down.
    """
    configured = getattr(settings, 'DOWNLOAD_PASS_SECONDS', DEFAULT_PASS_SECONDS)
    try:
        seconds = int(configured)
    except (TypeError, ValueError):
        logger.warning('DOWNLOAD_PASS_SECONDS=%r is not a number; using %d',
                       configured, DEFAULT_PASS_SECONDS)
        return DEFAULT_PASS_SECONDS
    return max(_MIN_PASS_SECONDS, min(seconds, _MAX_PASS_SECONDS))


def pass_duration_label():
    """How long a solved challenge lasts, phrased for the challenge page.

    Derived from the enforced value so the page cannot promise a window
    different from the one actually applied.
    """
    seconds = pass_seconds()
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return '1 hour' if hours == 1 else f'{hours} hours'
    if seconds % 60 == 0:
        minutes = seconds // 60
        return '1 minute' if minutes == 1 else f'{minutes} minutes'
    return f'{seconds} seconds'


def has_download_pass(request):
    """True when this session solved a challenge recently enough."""
    session = getattr(request, 'session', None)
    if session is None:
        return False
    try:
        return float(session.get(_SESSION_KEY, 0)) > time.time()
    except (TypeError, ValueError):
        return False


def grant_download_pass(request):
    """Record a solved challenge against this session."""
    request.session[_SESSION_KEY] = time.time() + pass_seconds()


INTERNAL_HEADER_NAME = 'X-Amprepo-Internal'
_INTERNAL_META_KEY = 'HTTP_X_AMPREPO_INTERNAL'
_INTERNAL_PURPOSE = b'amprepo-internal-download'

# Hosts that are this application talking to itself.  The internal token is
# only ever attached to requests aimed at these, so a future caller pointing
# download_file() at a third party cannot leak it.
_SELF_HOSTS = ('localhost', '127.0.0.1', '::1')


def _internal_token():
    """A token derived from SECRET_KEY, stable for the life of a deployment.

    Not a session or an expiring credential: it never leaves the host, and its
    only job is to distinguish 'the app fetching its own URL' from 'someone on
    the internet'.  Derived rather than configured so there is no new secret to
    provision, rotate, or leave at a default value.
    """
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        _INTERNAL_PURPOSE,
        hashlib.sha256,
    ).hexdigest()


def internal_download_headers(url):
    """Headers marking a request as this application's own self-call.

    Returns an empty dict for any URL that is not this app, so the token is
    never sent to a third party.
    """
    try:
        host = urlparse(url).hostname or ''
    except ValueError:
        return {}
    if host not in _SELF_HOSTS:
        return {}
    return {INTERNAL_HEADER_NAME: _internal_token()}


def is_internal_call(request):
    """True when this request carries the internal self-call token.

    The project edit/re-aggregation path downloads the *old* project tarball
    from ``http://localhost:8000/project/<id>/download`` and feeds it back into
    aggregation (views.py ``download_url``, views_apis.py ``url``).  Those
    requests carry no session and no user, so without an exemption a gated
    project_download turns re-aggregation into silent data loss: the challenge
    page is written to disk as ``download.tar.gz`` and extraction fails.

    An explicit token rather than a check on REMOTE_ADDR.  An address-based
    exemption would admit *all* loopback traffic, which means the gate would be
    inactive for every developer testing on http://localhost:8000 -- the one
    environment where it most needs to behave as it does in production.
    """
    presented = request.META.get(_INTERNAL_META_KEY)
    if not presented:
        return False
    return hmac.compare_digest(presented, _internal_token())


def may_download(request):
    """True when the request may proceed to a download view.

    Exposed for templates and views that want to render the ungated affordance
    without duplicating the rule.
    """
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return True
    if has_download_pass(request):
        return True
    return is_internal_call(request)


def _safe_next(request):
    """The path to return to after verification, rejecting off-site targets.

    An open redirect here would let the challenge page bounce visitors to an
    attacker's site, so only a root-relative path is ever stored.
    """
    target = request.get_full_path()
    if not target.startswith('/') or target.startswith('//'):
        return '/'
    return target[:_MAX_NEXT_LENGTH]


def _safe_origin(request):
    """The page the visitor was on when they hit a gated link.

    Taken from Referer, and only kept when it points at this site.  Used to
    return them where they were rather than stranding them on the challenge
    page after the file downloads.  Returns None when there is nothing
    trustworthy to go back to.
    """
    referer = request.META.get('HTTP_REFERER') or ''
    if not referer:
        return None
    parsed = urlparse(referer)
    host = request.get_host()
    if parsed.netloc and parsed.netloc != host:
        return None
    path = parsed.path or '/'
    if not path.startswith('/') or path.startswith('//'):
        return None
    if parsed.query:
        path = f'{path}?{parsed.query}'
    return path[:_MAX_NEXT_LENGTH]


def is_download_path(path):
    """True for a root-relative path that looks like one of the gated routes.

    Guards what may be handed back to the browser to fetch automatically after
    verification, so the parameter cannot be turned into a general-purpose
    redirect.
    """
    if not path.startswith('/') or path.startswith('//'):
        return False
    return path.startswith('/project/') and '/download' in path


def download_gate(view):
    """Require an account or a recent captcha before running ``view``.

    Applied to the routes that build archives or stream large files.  Not
    applied to ``png_download``: the sample page embeds those as ``<img>``
    sources, so gating them would break the page rather than protect it.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if may_download(request):
            return view(request, *args, **kwargs)

        session = getattr(request, 'session', None)
        if session is not None:
            session['download_next'] = _safe_next(request)
            origin = _safe_origin(request)
            if origin:
                session['download_origin'] = origin
            else:
                session.pop('download_origin', None)
        return redirect(reverse('download_verify'))

    # Explicit marker.  functools.wraps, login_required and View.as_view() all
    # set __wrapped__, so its presence says nothing about this gate; auditing
    # which routes are protected needs an attribute only this decorator sets.
    _wrapped.download_gated = True
    return _wrapped
