"""
Tests for the health endpoint and the load shedder (caper/middleware.py).

These cover the properties the mitigation actually depends on, which are mostly
properties about what it *does not* do: never touch the DB on a health check,
never reject an upload or an API call, never hold a slot after the request that
took it has gone away, and never fail closed when its own bookkeeping breaks.

The shared slot table is normally filled by other worker processes.  Here it is
filled directly, which is the only way to stage a given concurrency level from
inside one process.
"""

import os
import time

import pytest


# RequestFactory's default 'testserver' is not in ALLOWED_HOSTS, and
# request.get_host() has to succeed for the same-origin referer check to mean
# anything.
HOST = 'localhost'


@pytest.fixture
def middleware_module():
    """The module with its slot table cleared and its caps at known values."""
    from caper import middleware

    _reset(middleware)
    yield middleware
    _reset(middleware)


def _reset(middleware):
    for i in range(middleware._SLOT_COUNT):
        middleware._slot_pids[i] = 0
        middleware._slot_started[i] = 0.0
        middleware._slot_class[i] = middleware._EXEMPT
    middleware._my_slot = None
    middleware._my_slot_pid = None
    middleware._crawler_cache.clear()


def _live_foreign_pid():
    """A pid that exists in this process's namespace and is not this process.

    `os.getppid()` used to be it, and that is wrong inside a container: under
    `docker exec` the parent lives outside the container's PID namespace and
    `getppid()` returns **0**.  A slot owned by pid 0 is a slot owned by nobody,
    so the shed path's dead-worker reaper reclaims every staged row before it
    can refuse anything, and each of these tests sees a cheerful 200 where it
    expects a 503.  That was 11 of the 60 failures on the dev server on
    2026-08-31, and none of them reproduced on a laptop, where the parent is a
    real visible shell.

    PID 1 always exists and is never this process, in a container or out of one.
    """
    if os.getpid() != 1 and os.path.exists('/proc/1'):
        return 1
    parent = os.getppid()
    if parent and os.path.exists(f'/proc/{parent}'):
        return parent
    raise RuntimeError('no live foreign pid available to own a staged slot')


def _occupy(middleware, count, request_class, *, pid=None, age=0.0):
    """Stage `count` in-flight requests owned by some other (live) worker.

    The owner has to be a pid that really exists, or the shed path reaps the
    rows as belonging to a killed worker before rejecting anything -- which is
    the behaviour test_slots_of_dead_workers_are_reclaimed... covers.
    """
    pid = _live_foreign_pid() if pid is None else pid
    now = time.time()
    filled = 0
    for i in range(middleware._SLOT_COUNT):
        if filled == count:
            break
        if middleware._slot_pids[i] != 0:
            continue
        middleware._slot_pids[i] = pid
        middleware._slot_started[i] = now - age
        middleware._slot_class[i] = request_class
        filled += 1
    assert filled == count


def _shedder(middleware, downstream=None):
    calls = []

    def get_response(request):
        calls.append(request)
        from django.http import HttpResponse
        return (downstream or HttpResponse)('served')

    return middleware.LoadShedMiddleware(get_response), calls


def _sample_request(request_factory, *, referer=None, **extra):
    headers = {'HTTP_HOST': HOST}
    headers.update(extra)
    if referer is not None:
        headers['HTTP_REFERER'] = referer
    return request_factory.get('/project/abc123/sample/SAMPLE_1', **headers)


# --------------------------------------------------------------------------
# Health endpoint
# --------------------------------------------------------------------------

def test_health_check_answers_without_calling_the_rest_of_the_stack(request_factory):
    """The whole point: no session, no DB, no template, no URL resolution.

    If this ever starts calling get_response, the check regains the property
    that made it useless -- queueing behind page traffic and timing out while
    the box is merely busy.
    """
    from caper.middleware import HealthCheckMiddleware

    def get_response(request):  # pragma: no cover - must not run
        raise AssertionError('health check fell through to the application')

    response = HealthCheckMiddleware(get_response)(request_factory.get('/healthz'))

    assert response.status_code == 200
    assert response['Content-Type'] == 'text/plain'
    assert response['Cache-Control'] == 'no-store'
    assert response.content == b'ok\n'


def test_health_check_passes_other_paths_through(request_factory):
    from caper.middleware import HealthCheckMiddleware
    from django.http import HttpResponse

    seen = []

    def get_response(request):
        seen.append(request.path)
        return HttpResponse('app')

    HealthCheckMiddleware(get_response)(request_factory.get('/project/abc123'))
    assert seen == ['/project/abc123']


def test_health_check_is_never_shed(middleware_module, request_factory):
    """It has to answer while the box is saturated -- that is its entire job."""
    _occupy(middleware_module, middleware_module.PAGE_CONCURRENCY, middleware_module._PAGE)

    shed, _ = _shedder(middleware_module)
    response = middleware_module.HealthCheckMiddleware(shed)(
        request_factory.get('/healthz'))

    assert response.status_code == 200


# --------------------------------------------------------------------------
# What is governed
# --------------------------------------------------------------------------

@pytest.mark.parametrize('path', [
    '/project/abc123',
    '/project/abc123/sample/S1',
    '/project/abc123/sample/S1/feature/F1',
])
def test_page_views_are_governed(middleware_module, request_factory, path):
    assert middleware_module.LoadShedMiddleware._classify(
        request_factory.get(path)) != middleware_module._EXEMPT


@pytest.mark.parametrize('path', [
    '/',
    '/api/v1/projects/',
    '/api/v1/projects/abc123/samples/',
    '/api/v1/projects/abc123/download/',
    '/upload_api/',
    '/project/abc123/edit',
    '/accounts/login/',
    '/static/css/site.css',
    '/robots.txt',
])
def test_everything_else_is_exempt(middleware_module, request_factory, path):
    """A limiter that can only reject a page view or a direct download cannot
    break an upload or an API client.

    The API's own download endpoint is included deliberately: it lives under
    /api/ and must stay exempt however the web download rules change.
    """
    assert middleware_module.LoadShedMiddleware._classify(
        request_factory.get(path)) == middleware_module._EXEMPT


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

DOWNLOAD_PATHS = [
    '/project/abc123/download',
    '/project/abc123/download_summary',
    '/project/abc123/download_metadata',
    '/project/abc123/sample/S1/download',
    '/project/abc123/sample/S1/download_metadata',
    '/project/abc123/sample/S1/feature/F1/download/123',
    '/project/abc123/sample/S1/feature/F1/download/png/123',
    '/project/abc123/sample/S1/feature/F1/download/pdf/123',
]


@pytest.mark.parametrize('path', DOWNLOAD_PATHS)
def test_a_download_reached_directly_is_capped(
        middleware_module, request_factory, path):
    """Arriving straight at a data file, having never been on a site page.

    This is both the suspicious shape and the expensive one -- read
    amplification through the download path is what saturated the instance's
    inbound bandwidth in the original outages.
    """
    assert middleware_module.LoadShedMiddleware._classify(
        request_factory.get(path, HTTP_HOST=HOST)
    ) == middleware_module._DOWNLOAD_UNREFERRED


@pytest.mark.parametrize('path', DOWNLOAD_PATHS)
def test_a_download_reached_from_a_page_is_not_capped(
        middleware_module, request_factory, path):
    """Ordinary use, and slow by nature -- capping it would reject real work."""
    request = request_factory.get(
        path, HTTP_HOST=HOST, HTTP_REFERER='https://localhost/project/abc123')

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._EXEMPT


def test_direct_downloads_have_their_own_allowance(
        middleware_module, request_factory, monkeypatch):
    """Not the sample-page cap.

    Downloads run for minutes, so sharing the sample-page allowance would leave
    it occupied at idle and break the "only engages under load" property that
    makes the whole mechanism safe.
    """
    monkeypatch.setattr(middleware_module, 'UNREFERRED_CONCURRENCY', 2)
    monkeypatch.setattr(middleware_module, 'UNREFERRED_DOWNLOAD_CONCURRENCY', 3)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 2, middleware_module._PAGE_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(request_factory.get('/project/abc123/download', HTTP_HOST=HOST))

    assert response.status_code == 200
    assert len(calls) == 1


def test_direct_downloads_shed_at_their_own_cap(
        middleware_module, request_factory, monkeypatch):
    monkeypatch.setattr(middleware_module, 'UNREFERRED_DOWNLOAD_CONCURRENCY', 3)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 3, middleware_module._DOWNLOAD_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(request_factory.get('/project/abc123/download', HTTP_HOST=HOST))

    assert response.status_code == 503
    assert response['Retry-After'] == '60'
    assert calls == []


def test_a_referred_download_still_gets_through_a_full_download_cap(
        middleware_module, request_factory, monkeypatch):
    monkeypatch.setattr(middleware_module, 'UNREFERRED_DOWNLOAD_CONCURRENCY', 1)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 1, middleware_module._DOWNLOAD_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(request_factory.get(
        '/project/abc123/download', HTTP_HOST=HOST,
        HTTP_REFERER='https://localhost/project/abc123'))

    assert response.status_code == 200
    assert len(calls) == 1


def test_direct_downloads_count_against_the_general_cap_too(
        middleware_module, request_factory, monkeypatch):
    """Otherwise a full page cap plus a full download cap occupy every worker.

    The capacity this whole mechanism exists to reserve would be gone.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 3)
    monkeypatch.setattr(middleware_module, 'UNREFERRED_DOWNLOAD_CONCURRENCY', 3)
    _occupy(middleware_module, 3, middleware_module._PAGE)

    shed, calls = _shedder(middleware_module)
    response = shed(request_factory.get('/project/abc123/download', HTTP_HOST=HOST))

    assert response.status_code == 503
    assert calls == []


def test_writes_are_exempt(middleware_module, request_factory):
    assert middleware_module.LoadShedMiddleware._classify(
        request_factory.post('/project/abc123')) == middleware_module._EXEMPT


def test_same_origin_referer_earns_the_larger_allowance(middleware_module, request_factory):
    classify = middleware_module.LoadShedMiddleware._classify
    request = _sample_request(request_factory, referer='https://localhost/project/abc123')

    assert classify(request) == middleware_module._PAGE


@pytest.mark.parametrize('referer', [
    None,
    'https://scholar.example.edu/paper',
    'https://localhost.evil.example/',
])
def test_missing_or_offsite_referer_gets_the_tight_allowance(
        middleware_module, request_factory, referer):
    request = _sample_request(request_factory, referer=referer)

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._PAGE_UNREFERRED


@pytest.mark.parametrize('referer', [
    'https://LOCALHOST/project/abc123',
    'https://localhost/project/abc123?q=1',
])
def test_same_origin_matching_is_case_and_query_insensitive(
        middleware_module, request_factory, referer):
    """3,091 real referers on prod were spelled 'AmpliconRepository.org'."""
    request = _sample_request(request_factory, referer=referer)

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._PAGE


def test_www_and_apex_are_the_same_origin(middleware_module, request_factory):
    """The site answers on both and both appear heavily in real referers.

    A reader crossing between them is not a crawler, and should not be treated
    as one.
    """
    request = request_factory.get(
        '/project/abc123/sample/S1',
        HTTP_HOST='ampliconrepository.org',
        HTTP_REFERER='https://www.ampliconrepository.org/project/abc123')

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._PAGE


@pytest.mark.parametrize('referer', [
    'https://www.google.com/',
    'https://google.co.uk/search?q=ecdna',
    'https://scholar.google.com/',
    'https://duckduckgo.com/',
    'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/',
])
def test_search_arrivals_get_the_larger_allowance(
        middleware_module, request_factory, referer):
    """Measured on prod 2026-08-07: 12,023 sample-page arrivals from Google.

    Readers who find a sample through search land on it directly and carry no
    same-origin referer -- they are precisely who the referer tier is meant to
    protect, so they must not fall into the tight cap with the crawlers.
    """
    request = _sample_request(request_factory, referer=referer)

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._PAGE


@pytest.mark.parametrize('referer', [None, 'https://scholar.example.edu/paper'])
def test_project_pages_are_never_referer_tiered(
        middleware_module, request_factory, referer):
    """Landing on a project page directly is ordinary and must not be penalised.

    A missing referer only means something on a *sample* page, which a person
    essentially only reaches by navigating.  Project pages still count against
    the general cap; they are just never singled out.
    """
    headers = {'HTTP_HOST': HOST}
    if referer is not None:
        headers['HTTP_REFERER'] = referer

    assert middleware_module.LoadShedMiddleware._classify(
        request_factory.get('/project/abc123', **headers)) == middleware_module._PAGE


def test_a_direct_project_page_visit_survives_an_exhausted_tight_cap(
        middleware_module, request_factory, monkeypatch):
    """The behaviour that matters: shedding sample pages must not shed these."""
    monkeypatch.setattr(middleware_module, 'UNREFERRED_CONCURRENCY', 1)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 1, middleware_module._PAGE_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(request_factory.get('/project/abc123', HTTP_HOST=HOST))

    assert response.status_code == 200
    assert len(calls) == 1


def test_search_referer_allowance_can_be_turned_off(
        middleware_module, request_factory, monkeypatch):
    monkeypatch.setattr(middleware_module, 'SEARCH_REFERERS', False)
    request = _sample_request(request_factory, referer='https://www.google.com/')

    assert middleware_module.LoadShedMiddleware._classify(
        request) == middleware_module._PAGE_UNREFERRED


def test_a_forged_search_referer_still_meets_the_general_cap(
        middleware_module, request_factory, monkeypatch):
    """This tier is an allowance, not an exemption.

    Nothing verifies a search referer, so a crawler can claim one -- and gains
    only the move from the tight cap to the general one, which still bounds it.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(request_factory, referer='https://www.google.com/'))

    assert response.status_code == 503
    assert calls == []


# --------------------------------------------------------------------------
# Shedding
# --------------------------------------------------------------------------

def test_nothing_is_rejected_at_baseline(middleware_module, request_factory):
    """The limiter only engages under load.

    This is the safety property behind using it at all: a sample-page URL cited
    in a paper works normally, referer or not, whenever the site is not busy.
    """
    shed, calls = _shedder(middleware_module)

    response = shed(_sample_request(request_factory))

    assert response.status_code == 200
    assert len(calls) == 1


def test_unreferred_sample_pages_shed_at_their_own_cap(
        middleware_module, request_factory, monkeypatch):
    """The tight cap engages while the general cap still has room."""
    monkeypatch.setattr(middleware_module, 'UNREFERRED_CONCURRENCY', 2)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 2, middleware_module._PAGE_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(request_factory))

    assert response.status_code == 503
    assert response['Retry-After'] == '60'
    assert calls == []


def test_referred_traffic_still_gets_in_while_unreferred_is_shedding(
        middleware_module, request_factory, monkeypatch):
    """Where all the selectivity comes from: real navigation is unaffected."""
    monkeypatch.setattr(middleware_module, 'UNREFERRED_CONCURRENCY', 2)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 6)
    _occupy(middleware_module, 2, middleware_module._PAGE_UNREFERRED)

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory, referer='https://localhost/project/abc123'))

    assert response.status_code == 200
    assert len(calls) == 1


def test_general_cap_sheds_referred_pages_too(
        middleware_module, request_factory, monkeypatch):
    """Page traffic must not be able to occupy every worker, whatever its shape."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 3)
    _occupy(middleware_module, 3, middleware_module._PAGE)

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory, referer='https://localhost/project/abc123'))

    assert response.status_code == 503
    assert response['Retry-After'] == '10'
    assert calls == []


def test_shed_response_says_later_not_no(middleware_module, request_factory, monkeypatch):
    """503 + Retry-After, never 403.

    A 403 tells a search engine the page is forbidden; a 503 tells it to come
    back.  Under an attack the difference is between one retry and deindexing.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    shed, _ = _shedder(middleware_module)
    response = shed(_sample_request(request_factory))

    assert response.status_code == 503
    assert response['Cache-Control'] == 'no-store'
    assert int(response['Retry-After']) > 0


def test_exempt_traffic_is_served_while_pages_shed(
        middleware_module, request_factory, monkeypatch):
    """Capacity reservation, stated as a test: the API keeps working."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 503
    assert shed(request_factory.get('/api/v1/projects/')).status_code == 200
    assert len(calls) == 1


def test_kill_switch_disables_shedding(middleware_module, request_factory, monkeypatch):
    monkeypatch.setattr(middleware_module, 'ENABLED', False)
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 200
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Slot bookkeeping
# --------------------------------------------------------------------------

def test_slot_is_released_after_the_response(middleware_module, request_factory):
    shed, _ = _shedder(middleware_module)
    shed(_sample_request(request_factory))

    assert middleware_module._inflight(time.time()) == (0, 0, 0)


def test_slot_is_released_when_the_view_raises(middleware_module, request_factory):
    """An exception inside the app must not permanently consume an allowance."""
    def boom(request):
        raise RuntimeError('view exploded')

    shed = middleware_module.LoadShedMiddleware(boom)

    with pytest.raises(RuntimeError):
        shed(_sample_request(request_factory))

    assert middleware_module._inflight(time.time()) == (0, 0, 0)


def test_slot_is_held_for_the_duration_of_the_request(middleware_module, request_factory):
    observed = []

    def get_response(request):
        from django.http import HttpResponse
        observed.append(middleware_module._inflight(time.time()))
        return HttpResponse('served')

    middleware_module.LoadShedMiddleware(get_response)(_sample_request(request_factory))

    assert observed == [(1, 1, 0)]


def test_stale_slots_stop_counting(middleware_module, request_factory, monkeypatch):
    """A worker SIGKILLed mid-request must not spend its allowance forever.

    Without expiry the caps would ratchet down to zero over successive incidents
    and the site would shed everything -- worse than the attack.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    monkeypatch.setattr(middleware_module, 'SLOT_STALE_SECONDS', 120.0)
    _occupy(middleware_module, 1, middleware_module._PAGE, age=600.0)

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 200
    assert len(calls) == 1


def test_slots_of_dead_workers_are_reclaimed_before_anything_is_rejected(
        middleware_module, request_factory, monkeypatch):
    """The reap runs on the shed path, so a dead worker costs no rejections."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    # pid 2**31-1 cannot exist: above /proc/sys/kernel/pid_max on any Linux.
    _occupy(middleware_module, 1, middleware_module._PAGE, pid=2 ** 31 - 1)

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 200
    assert len(calls) == 1


def test_full_slot_table_fails_open(middleware_module, request_factory, monkeypatch):
    """Every failure of our own bookkeeping has to serve the request, not refuse it."""
    monkeypatch.setattr(middleware_module, '_claim_slot', lambda: None)

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 200
    assert len(calls) == 1


def test_a_stuck_lock_disables_the_limiter_rather_than_the_site(
        middleware_module, request_factory, monkeypatch):
    """A worker killed while holding the lock would leave it held forever.

    Bounded acquisition turns that from a site-wide wedge into a limiter that
    has stopped limiting.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    class StuckLock:
        def acquire(self, timeout=None):
            return False

        def release(self):  # pragma: no cover - never acquired
            raise AssertionError('released a lock that was never acquired')

    monkeypatch.setattr(middleware_module, '_slot_lock', StuckLock())

    shed, calls = _shedder(middleware_module)

    assert shed(_sample_request(request_factory)).status_code == 200
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Verified crawlers
# --------------------------------------------------------------------------

GOOGLEBOT_UA = ('Mozilla/5.0 (compatible; Googlebot/2.1; '
                '+http://www.google.com/bot.html)')


def test_verified_googlebot_is_admitted_over_the_cap(
        middleware_module, request_factory, monkeypatch):
    """Search indexers send no Referer, so they meet the tight cap head-on."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)
    monkeypatch.setattr(middleware_module.socket, 'gethostbyaddr',
                        lambda ip: ('crawl-66-249-66-1.googlebot.com', [], [ip]))
    monkeypatch.setattr(middleware_module.socket, 'gethostbyname_ex',
                        lambda host: (host, [], ['66.249.66.1']))

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory, HTTP_USER_AGENT=GOOGLEBOT_UA, REMOTE_ADDR='66.249.66.1'))

    assert response.status_code == 200
    assert len(calls) == 1


def test_a_spoofed_googlebot_user_agent_is_shed(
        middleware_module, request_factory, monkeypatch):
    """The UA alone must never be enough -- it is trivially forged.

    The measured attacks already impersonate a browser; a UA-only exemption
    would hand them a way through the tightest cap.
    """
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)
    monkeypatch.setattr(middleware_module.socket, 'gethostbyaddr',
                        lambda ip: ('ecs-43-172-0-1.compute.example.com', [], [ip]))

    shed, calls = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory, HTTP_USER_AGENT=GOOGLEBOT_UA, REMOTE_ADDR='43.172.0.1'))

    assert response.status_code == 503
    assert calls == []


def test_forward_confirmation_is_required(
        middleware_module, request_factory, monkeypatch):
    """Reverse DNS on its own is attacker-controlled; the forward lookup is not."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)
    monkeypatch.setattr(middleware_module.socket, 'gethostbyaddr',
                        lambda ip: ('crawl-1-2-3-4.googlebot.com', [], [ip]))
    monkeypatch.setattr(middleware_module.socket, 'gethostbyname_ex',
                        lambda host: (host, [], ['66.249.66.1']))

    shed, _ = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory, HTTP_USER_AGENT=GOOGLEBOT_UA, REMOTE_ADDR='1.2.3.4'))

    assert response.status_code == 503


def test_crawler_verification_is_cached_per_ip(
        middleware_module, request_factory, monkeypatch):
    """A spoofed-UA flood must not turn this into a DNS attack on ourselves."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    lookups = []

    def counting_lookup(ip):
        lookups.append(ip)
        raise middleware_module.socket.herror('no PTR')

    monkeypatch.setattr(middleware_module.socket, 'gethostbyaddr', counting_lookup)

    shed, _ = _shedder(middleware_module)
    for _ in range(5):
        shed(_sample_request(
            request_factory, HTTP_USER_AGENT=GOOGLEBOT_UA, REMOTE_ADDR='43.172.0.1'))

    assert lookups == ['43.172.0.1']


def test_ordinary_traffic_never_triggers_a_lookup(
        middleware_module, request_factory, monkeypatch):
    """No DNS in the request path for anyone not claiming to be a search engine."""
    monkeypatch.setattr(middleware_module, 'PAGE_CONCURRENCY', 1)
    _occupy(middleware_module, 1, middleware_module._PAGE)

    def forbidden(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError('resolved DNS for a request with a browser UA')

    monkeypatch.setattr(middleware_module.socket, 'gethostbyaddr', forbidden)

    shed, _ = _shedder(middleware_module)
    response = shed(_sample_request(
        request_factory,
        HTTP_USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120'))

    assert response.status_code == 503


def test_client_ip_comes_from_the_load_balancer_header(
        middleware_module, request_factory):
    """Behind the ALB, REMOTE_ADDR is the balancer, not the client."""
    request = request_factory.get(
        '/project/abc123/sample/S1',
        HTTP_X_FORWARDED_FOR='203.0.113.9, 66.249.66.1',
        REMOTE_ADDR='10.0.1.5')

    assert middleware_module._client_ip(request) == '66.249.66.1'
