"""Landing gate for the development server.

Why this exists
---------------
`dev.ampliconrepository.org` runs unreleased code against test data, and it was
publicly browsable and publicly indexed.  Measured 2026-09-03: a
`site:dev.ampliconrepository.org` search returned both the dev home page and a
dev project page, and dev served production's own robots.txt -- the one whose
comment reads "Pages stay crawlable for search visibility".  Someone following
an old link or editing the production URL landed in a working-looking
repository built out of test data.

The yellow `SERVER_IDENTIFICATION_BANNER` strip has been on dev for a long time
and says exactly the right thing.  It did not prevent either problem, which is
why this is a gate and not another notice.

Scope, and why it is this narrow
--------------------------------
This is a shibboleth, not a security boundary.  The dev server holds no
sensitive data; the job is to stop a wanderer from mistaking it for the
repository, and to stop crawlers indexing it.

* **The home page, and nothing else.**  Site owner's call 2026-09-03: someone
  who requests a page *behind* the home page reaches it.  That is the rarer
  arrival, and letting it through is what keeps the REST API, the Google and
  Globus callbacks and `weekly-report.py` working with no exemption list to
  maintain -- the part of a whole-site gate that would have needed the most
  machinery and broken the most flows.

* **Crawlers are handled by the header, not the gate.**  A crawler holding an
  indexed deep link never asks for `/`, so the gate alone would not have
  removed the project page found above.  Every response from a gated
  deployment carries `X-Robots-Tag: noindex, nofollow, noarchive`.  robots.txt
  stays crawlable *on purpose* until the existing entries drop out: a
  `Disallow: /` now would stop Google re-fetching the pages and therefore stop
  it ever seeing the noindex, leaving the URLs in the index indefinitely.
  `DEV_ROBOTS_DISALLOW_ALL` is the second stage; see `views.robots`.

* **The passphrase is not a secret.**  It is in README.md, in AGENTS.md and in
  the developer wiki, alongside instructions for asking for a dev account.
  Publishing it is the point: there is no distribution problem, no config
  drift, and no failure mode where the gate is enabled but nobody can get in.

* **Enabled by a setting, never by hostname.**  A hostname test cannot be
  exercised on a laptop and puts a domain name in the source where a
  deployment setting belongs.  `DEV_GATE_ENABLED` is unset everywhere except
  the dev server, so production and an ordinary checkout are untouched.

Position in the stack
---------------------
Second, immediately after `HealthCheckMiddleware` and above `LoadShedMiddleware`
-- so a turned-away visitor costs no session read, no MongoDB query, no
Mezzanine page lookup and no template context processor (`shutdown_mode` alone
would be a Mongo round trip).  The landing page is rendered with
`render_to_string` and no request object, which is what keeps the context
processors out of it.

Two consequences of sitting that high, both deliberate:

* The gate runs above `AuthenticationMiddleware`, so it cannot see
  `request.user` and cannot admit an administrator on the strength of being
  logged in.  The passphrase is the only key, for everyone.
* It runs above `CsrfViewMiddleware`, so the passphrase form is handled here
  rather than by a view.  There is no session and no authenticated state to
  forge against, so there is nothing for CSRF to protect.

Passing the gate deliberately does *not* log anyone in.  Anonymous behaviour --
public project pages, private-project redirects, signup, the OAuth flows -- is
most of what dev exists to test.
"""

import logging

from django.conf import settings
from django.core import signing
from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# Host-only cookie.  Named for the deployment rather than the app so it is
# obvious in a browser inspector that it is not a login.
COOKIE_NAME = 'amprepo_dev_access'

# Namespaces the signature, so a dev pass cannot be made from a signed value
# minted anywhere else in the app off the same SECRET_KEY.
_SALT = 'caper.dev_gate'

# The signed payload carries nothing.  Holding the cookie *is* the claim; there
# is no identity to record, and recording one would make this look like a login.
_PAYLOAD = 'dev'

_DEFAULT_MAX_AGE = 7 * 24 * 3600

_ROBOTS_HEADER = 'noindex, nofollow, noarchive'

_TEMPLATE = 'dev_gate.html'

# Only the site root.  Django normalises the root request path to '/', but an
# empty PATH_INFO is possible from a hand-built request.
_HOME_PATHS = frozenset(('/', ''))


def gate_enabled():
    """Whether this deployment shows the landing gate.

    Read from settings on every call rather than bound at import, so
    `DEV_GATE_ENABLED` in config.sh -- and `override_settings` in a test -- is
    actually in force.
    """
    return bool(getattr(settings, 'DEV_GATE_ENABLED', False))


def passphrase():
    """The configured passphrase, or '' if it is not set."""
    return (getattr(settings, 'DEV_GATE_PASSPHRASE', '') or '').strip()


def max_age():
    """How long a pass is honoured, in seconds."""
    try:
        return int(getattr(settings, 'DEV_GATE_MAX_AGE_SECONDS', _DEFAULT_MAX_AGE))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE


def issue_pass():
    """A signed, timestamped cookie value.

    `django.core.signing` gives signing and expiry off the existing
    `DJANGO_SECRET_KEY`, so the gate adds no new secret to configure and has no
    "enabled but misconfigured" state to explain to a visitor.
    """
    return signing.dumps(_PAYLOAD, salt=_SALT)


def pass_is_valid(value):
    """Whether `value` is a pass this deployment signed and has not expired."""
    if not value:
        return False
    try:
        signing.loads(value, salt=_SALT, max_age=max_age())
    except signing.BadSignature:
        # Covers SignatureExpired as well; a tampered pass and a stale one are
        # the same thing to a visitor -- the landing page.
        return False
    return True


def _matches(submitted):
    """Whether `submitted` is the passphrase.

    Forgiving about whitespace and case: it is copied out of a wiki page and
    typed by hand, and a stray space should not stop a developer.  An unset
    passphrase matches nothing -- a blank setting must not become a blank
    password.
    """
    expected = passphrase()
    if not expected:
        return False
    return (submitted or '').strip().lower() == expected.lower()


def _landing_page(error=False):
    """The standalone landing page.

    Rendered without a request, which is what keeps the template context
    processors -- and therefore MongoDB -- out of a refused request.
    """
    response = HttpResponse(render_to_string(_TEMPLATE, {'error': error}))
    response['Cache-Control'] = 'no-store'
    return response


def _admit(request):
    response = HttpResponseRedirect('/')
    response.set_cookie(
        COOKIE_NAME,
        issue_pass(),
        max_age=max_age(),
        httponly=True,
        # Lax, so the pass survives the top-level redirect back from Google or
        # Globus.  Strict would drop it on exactly that hop.
        samesite='Lax',
        # Host-only: no `domain`, so the cookie never reaches production.
        #
        # `secure` follows the request rather than being hard-coded.  Behind the
        # ALB, with no SECURE_PROXY_SSL_HEADER configured, this evaluates False
        # and the flag is not set -- which is also what makes the gate testable
        # over plain http locally.  Acceptable here and nowhere near a session
        # cookie: the value authorises nothing but seeing the dev home page.
        secure=request.is_secure(),
    )
    return response


def _leave(request):
    response = HttpResponseRedirect('/')
    response.delete_cookie(COOKIE_NAME)
    return response


class DevGateMiddleware:
    """Show the development landing page to visitors without a dev pass."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not gate_enabled():
            return self.get_response(request)

        gate_response = self._handle_gate(request)
        response = gate_response if gate_response is not None else self.get_response(request)

        # Every response from a gated deployment, not only the landing page.
        # The crawler that matters already holds a deep link.
        response['X-Robots-Tag'] = _ROBOTS_HEADER
        return response

    def _handle_gate(self, request):
        """The gate's own response, or None to let the application answer."""
        if request.path not in _HOME_PATHS:
            return None

        admitted = pass_is_valid(request.COOKIES.get(COOKIE_NAME))

        if request.method == 'POST':
            # Reading request.POST here is safe for the pass-through case:
            # Django caches the parsed body, so the view downstream sees it.
            if 'leave' in request.POST:
                return _leave(request)
            if not admitted:
                if _matches(request.POST.get('code')):
                    logger.info('dev gate: passphrase accepted')
                    return _admit(request)
                logger.info('dev gate: passphrase refused')
                return _landing_page(error=True)

        if admitted:
            return None

        return _landing_page()
