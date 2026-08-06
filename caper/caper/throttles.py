"""
Rate throttling for the /api/v1/ REST API.

Scope and limits of what this provides
--------------------------------------
This throttles per *client* (API token if presented, otherwise client IP).
That makes it effective against a single runaway script and it gives callers
predictable, documented limits with a proper 429 + Retry-After — which the
Python client and AI agents need in order to back off correctly.

It is **not** a defense against a distributed crawl.  The scraper swarms that
took the site down used thousands of IPs at well under one request per IP per
minute; no per-client limit sees those as abusive.  The defenses that do work
against that shape are the read-amplification fixes in views_apis.py / utils.py
and the WAF rate rule at the edge.  Do not treat this module as covering that.

Why a custom throttle class
---------------------------
The /api/v1/ views authenticate manually via _authenticate_api_request() rather
than through DRF's authentication_classes, because they return their own JSON
error bodies for bad tokens (see tests/test_api_v1.py).  A consequence is that
request.user is still AnonymousUser by the time throttling runs, so DRF's stock
ScopedRateThrottle would key every token-authenticated caller by IP — putting
all users behind one institutional NAT into a single shared bucket.  This class
resolves the token itself so authenticated callers get their own bucket, and an
optionally higher limit.
"""

from django.conf import settings
from django.core.cache import caches
from django.core.cache import cache as default_cache

from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

# Throttle counters live in their own cache so they cannot evict — or be
# evicted by — the chart/GridFS entries in the 'default' cache.  See the
# CACHES block in settings.py for why that separation matters.
THROTTLE_CACHE_ALIAS = 'throttle'

# A view with throttle_scope = 'api_projects' uses the 'api_projects' rate for
# anonymous callers and 'api_projects_auth' for token-authenticated ones, when
# that second key is defined in DEFAULT_THROTTLE_RATES.
AUTHENTICATED_SCOPE_SUFFIX = '_auth'


class ApiScopedRateThrottle(ScopedRateThrottle):
    """ScopedRateThrottle that keys by API token when one is presented."""

    @property
    def THROTTLE_RATES(self):
        """
        Read the rate table live instead of inheriting DRF's snapshot.

        SimpleRateThrottle binds `THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES`
        in the class body, so it captures whichever dict object existed at import
        time.  Anything that later swaps REST_FRAMEWORK -- override_settings in
        the tests, or a settings reload -- is then silently ignored, and the
        limits in force are not the ones configured.
        """
        return api_settings.DEFAULT_THROTTLE_RATES

    def __init__(self):
        super().__init__()
        # Fall back to the default cache if a deployment's local_settings.py
        # replaces CACHES without carrying the throttle alias over.
        if THROTTLE_CACHE_ALIAS in getattr(settings, 'CACHES', {}):
            self.cache = caches[THROTTLE_CACHE_ALIAS]
        else:
            self.cache = default_cache

    def allow_request(self, request, view):
        scope = getattr(view, self.scope_attr, None)
        if not scope:
            return True

        # Resolve identity once, before choosing the rate: whether the caller
        # is authenticated decides both the bucket and which limit applies.
        self._ident = self._resolve_ident(request)
        if self._ident is not None:
            authenticated_scope = f'{scope}{AUTHENTICATED_SCOPE_SUFFIX}'
            if authenticated_scope in self.THROTTLE_RATES:
                scope = authenticated_scope

        self.scope = scope
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

        # Skip ScopedRateThrottle.allow_request, which would redo the scope and
        # rate resolution above using the stock (user-based) logic.
        return SimpleRateThrottle.allow_request(self, request, view)

    def get_rate(self):
        """
        Return None (meaning "no limit") for a scope with no configured rate,
        rather than DRF's ImproperlyConfigured.

        A view whose scope is missing from DEFAULT_THROTTLE_RATES should serve
        traffic unthrottled, not return 500s; this also lets the test suite
        disable throttling by emptying the rate table.
        """
        return self.THROTTLE_RATES.get(self.scope)

    def get_cache_key(self, request, view):
        ident = getattr(self, '_ident', None) or self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}

    @staticmethod
    def _resolve_ident(request):
        """
        Return a stable 'user:<pk>' identity for a token-authenticated caller,
        or None to fall back to IP-based keying.

        An *invalid* token also returns None, so a caller cannot escape a
        per-IP limit by cycling through made-up token values.
        """
        # DRF's initial() runs perform_authentication() before check_throttles(),
        # so request._user is already resolved by the time we get here.  Read the
        # cached value rather than request.user, which would re-enter the
        # authenticators for views that deliberately handle auth themselves.
        user = getattr(request, '_user', None)
        if user is not None and user.is_authenticated:
            return f'user:{user.pk}'
        try:
            result = TokenAuthentication().authenticate(request)
        except AuthenticationFailed:
            return None
        if result and result[0] is not None:
            return f'user:{result[0].pk}'
        return None
