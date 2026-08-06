"""
Tests for /api/v1/ rate throttling (caper/throttles.py).

Every test here is marked `throttled` so the autouse fixture in conftest.py
leaves throttling enabled, and then sets the exact rates it wants via
override_settings.  The real cache backend is used throughout — the counters
are the behaviour under test, so mocking them would test nothing.
"""

import pytest
from unittest.mock import MagicMock, patch

from django.test import override_settings
from rest_framework.test import APIRequestFactory

pytestmark = pytest.mark.throttled


def _rates(**overrides):
    """REST_FRAMEWORK settings with tight, explicit limits for testing."""
    rates = {
        'api_read': '3/min',
        'api_read_auth': '5/min',
        'api_download': '2/min',
        'api_batch': '2/min',
        'api_token': '2/min',
    }
    rates.update(overrides)
    return {
        'NUM_PROXIES': 1,
        'DEFAULT_THROTTLE_CLASSES': [],
        'DEFAULT_THROTTLE_RATES': rates,
    }


# ---------------------------------------------------------------------------
# Core limit behaviour
# ---------------------------------------------------------------------------

class TestReadThrottle:
    def setup_method(self):
        from caper.views_apis import ProjectListView
        self.view = ProjectListView.as_view()
        self.rf = APIRequestFactory()

    @override_settings(REST_FRAMEWORK=_rates())
    def test_requests_under_limit_all_succeed(self):
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            codes = [self.view(self.rf.get('/api/v1/projects/')).status_code
                     for _ in range(3)]
        assert codes == [200, 200, 200]

    @override_settings(REST_FRAMEWORK=_rates())
    def test_request_over_limit_returns_429(self):
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            for _ in range(3):
                self.view(self.rf.get('/api/v1/projects/'))
            resp = self.view(self.rf.get('/api/v1/projects/'))
        assert resp.status_code == 429

    @override_settings(REST_FRAMEWORK=_rates())
    def test_429_carries_retry_after_header(self):
        """The pip client and AI agents back off on Retry-After; it must be set."""
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            for _ in range(3):
                self.view(self.rf.get('/api/v1/projects/'))
            resp = self.view(self.rf.get('/api/v1/projects/'))
        assert resp.status_code == 429
        assert 'Retry-After' in resp
        assert int(resp['Retry-After']) > 0

    @override_settings(REST_FRAMEWORK=_rates())
    def test_distinct_ips_get_distinct_buckets(self):
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            for _ in range(3):
                self.view(self.rf.get('/api/v1/projects/', REMOTE_ADDR='10.0.0.1'))
            blocked = self.view(self.rf.get('/api/v1/projects/', REMOTE_ADDR='10.0.0.1'))
            other   = self.view(self.rf.get('/api/v1/projects/', REMOTE_ADDR='10.0.0.2'))
        assert blocked.status_code == 429
        assert other.status_code == 200


class TestScopesAreIndependent:
    """Exhausting one scope must not throttle a different endpoint."""

    @override_settings(REST_FRAMEWORK=_rates())
    def test_download_limit_does_not_affect_list(self):
        from caper.views_apis import ProjectListView, ProjectDownloadView
        rf = APIRequestFactory()
        list_view = ProjectListView.as_view()
        dl_view = ProjectDownloadView.as_view()

        with patch('caper.views_apis.get_one_project_sans_runs') as gop:
            gop.return_value = None  # 404 — routing only; the throttle runs first
            for _ in range(2):
                dl_view(rf.get('/api/v1/projects/x/download/'), project_id='x')
            blocked = dl_view(rf.get('/api/v1/projects/x/download/'), project_id='x')
        assert blocked.status_code == 429

        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            assert list_view(rf.get('/api/v1/projects/')).status_code == 200


# ---------------------------------------------------------------------------
# Identity: token callers get their own bucket, not a shared per-IP one
# ---------------------------------------------------------------------------

class TestTokenIdentity:
    """
    The /api/v1/ views authenticate manually, so request.user is anonymous when
    throttling runs.  Without ApiScopedRateThrottle._resolve_ident, every token
    caller behind one NAT would share a single IP bucket.
    """

    def setup_method(self):
        from caper.views_apis import ProjectListView
        self.view = ProjectListView.as_view()
        self.rf = APIRequestFactory()

    def _user(self, pk):
        u = MagicMock()
        u.pk = pk
        u.is_authenticated = True
        u.username = f'user{pk}'
        u.email = f'user{pk}@example.com'
        return u

    @override_settings(REST_FRAMEWORK=_rates())
    def test_two_tokens_from_one_ip_do_not_share_a_bucket(self):
        users = {'Token aaa': self._user(1), 'Token bbb': self._user(2)}

        def fake_auth(request):
            hdr = request.META.get('HTTP_AUTHORIZATION', '')
            return (users[hdr], None) if hdr in users else None

        with patch('caper.views_apis.collection_handle') as col, \
             patch('caper.throttles.TokenAuthentication') as ThrottleTA, \
             patch('caper.views_apis.TokenAuthentication') as ViewTA:
            col.find.return_value = []
            ThrottleTA.return_value.authenticate.side_effect = fake_auth
            ViewTA.return_value.authenticate.side_effect = fake_auth

            # api_read_auth is 5/min; exhaust it for the first token only.
            for _ in range(5):
                r = self.view(self.rf.get('/api/v1/projects/',
                                          HTTP_AUTHORIZATION='Token aaa',
                                          REMOTE_ADDR='10.1.1.1'))
                assert r.status_code == 200
            blocked = self.view(self.rf.get('/api/v1/projects/',
                                            HTTP_AUTHORIZATION='Token aaa',
                                            REMOTE_ADDR='10.1.1.1'))
            other = self.view(self.rf.get('/api/v1/projects/',
                                          HTTP_AUTHORIZATION='Token bbb',
                                          REMOTE_ADDR='10.1.1.1'))

        assert blocked.status_code == 429
        assert other.status_code == 200, \
            "second token shares the first token's bucket — identity keying is broken"

    @override_settings(REST_FRAMEWORK=_rates())
    def test_authenticated_callers_get_the_higher_limit(self):
        user = self._user(7)

        def fake_auth(request):
            return (user, None) if request.META.get('HTTP_AUTHORIZATION') else None

        with patch('caper.views_apis.collection_handle') as col, \
             patch('caper.throttles.TokenAuthentication') as ThrottleTA, \
             patch('caper.views_apis.TokenAuthentication') as ViewTA:
            col.find.return_value = []
            ThrottleTA.return_value.authenticate.side_effect = fake_auth
            ViewTA.return_value.authenticate.side_effect = fake_auth

            # Anonymous limit is 3/min; the token limit is 5/min.
            codes = [self.view(self.rf.get('/api/v1/projects/',
                                           HTTP_AUTHORIZATION='Token aaa')).status_code
                     for _ in range(5)]
        assert codes == [200] * 5, \
            "token caller was held to the anonymous rate"

    @override_settings(REST_FRAMEWORK=_rates())
    def test_invalid_token_falls_back_to_ip_bucket(self):
        """
        A bad token must not mint a fresh bucket, or cycling made-up tokens
        would bypass the per-IP limit entirely.
        """
        from rest_framework.exceptions import AuthenticationFailed

        with patch('caper.views_apis.collection_handle') as col, \
             patch('caper.throttles.TokenAuthentication') as ThrottleTA, \
             patch('caper.views_apis.TokenAuthentication') as ViewTA:
            col.find.return_value = []
            ThrottleTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            ViewTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')

            codes = [self.view(self.rf.get('/api/v1/projects/',
                                           HTTP_AUTHORIZATION=f'Token bad{i}',
                                           REMOTE_ADDR='10.2.2.2')).status_code
                     for i in range(4)]

        # First three are the view's own 401 for a bad token; the fourth is
        # throttled, proving all four shared one IP-keyed bucket.
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429


# ---------------------------------------------------------------------------
# X-Forwarded-For handling
# ---------------------------------------------------------------------------

class TestForwardedForIdentity:
    """
    NUM_PROXIES = 1 makes DRF take the *last* XFF entry — the one the ALB
    appends.  Without it DRF keys on the whole client-supplied chain, and a
    caller can mint unlimited identities by prepending junk.
    """

    def setup_method(self):
        from caper.views_apis import ProjectListView
        self.view = ProjectListView.as_view()
        self.rf = APIRequestFactory()

    @override_settings(REST_FRAMEWORK=_rates())
    def test_spoofed_leading_xff_entries_cannot_evade_the_limit(self):
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            codes = []
            for i in range(4):
                # Attacker varies the part of the chain they control; the ALB
                # always appends the same real client IP last.
                codes.append(self.view(self.rf.get(
                    '/api/v1/projects/',
                    HTTP_X_FORWARDED_FOR=f'10.9.9.{i}, 203.0.113.5',
                    REMOTE_ADDR='172.16.0.1',
                )).status_code)
        assert codes == [200, 200, 200, 429], \
            "varying the spoofable part of X-Forwarded-For created new buckets"

    @override_settings(REST_FRAMEWORK=_rates())
    def test_distinct_real_clients_behind_the_alb_are_separate(self):
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            for _ in range(3):
                self.view(self.rf.get('/api/v1/projects/',
                                      HTTP_X_FORWARDED_FOR='203.0.113.5',
                                      REMOTE_ADDR='172.16.0.1'))
            blocked = self.view(self.rf.get('/api/v1/projects/',
                                            HTTP_X_FORWARDED_FOR='203.0.113.5',
                                            REMOTE_ADDR='172.16.0.1'))
            other = self.view(self.rf.get('/api/v1/projects/',
                                          HTTP_X_FORWARDED_FOR='203.0.113.9',
                                          REMOTE_ADDR='172.16.0.1'))
        assert blocked.status_code == 429
        assert other.status_code == 200


# ---------------------------------------------------------------------------
# Configuration safety
# ---------------------------------------------------------------------------

class TestConfiguration:
    @override_settings(REST_FRAMEWORK={'NUM_PROXIES': 1,
                                       'DEFAULT_THROTTLE_CLASSES': [],
                                       'DEFAULT_THROTTLE_RATES': {}})
    def test_missing_rate_serves_traffic_instead_of_500(self):
        """An unconfigured scope must degrade to 'no limit', not ImproperlyConfigured."""
        from caper.views_apis import ProjectListView
        view = ProjectListView.as_view()
        rf = APIRequestFactory()
        with patch('caper.views_apis.collection_handle') as col:
            col.find.return_value = []
            codes = [view(rf.get('/api/v1/projects/')).status_code for _ in range(10)]
        assert codes == [200] * 10

    def test_throttle_uses_its_own_cache_not_default(self):
        """
        Throttle counters must not share the 'default' cache, whose MAX_ENTRIES
        is 1000 — API bursts would evict the chart/GridFS entries and vice versa.
        """
        from django.core.cache import caches
        from caper.throttles import ApiScopedRateThrottle

        throttle = ApiScopedRateThrottle()
        assert throttle.cache is caches['throttle']
        assert throttle.cache is not caches['default']

    def test_every_v1_view_declares_a_configured_scope(self):
        """
        Guard against a new endpoint landing unthrottled, or naming a scope
        that has no rate in settings.
        """
        from django.conf import settings
        from caper import views_apis
        from caper.throttles import ApiScopedRateThrottle

        expected = {
            'ProjectListView':          'api_read',
            'ProjectDetailView':        'api_read',
            'ProjectSamplesView':       'api_read',
            'ProjectDownloadView':      'api_download',
            'ProjectBatchDownloadView': 'api_batch',
            'ApiTokenView':             'api_token',
        }
        rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        for name, scope in expected.items():
            view = getattr(views_apis, name)
            assert getattr(view, 'throttle_scope', None) == scope, name
            assert ApiScopedRateThrottle in view.throttle_classes, name
            assert scope in rates, f'{scope} missing from DEFAULT_THROTTLE_RATES'
