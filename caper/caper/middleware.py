"""
Edge-of-process request handling: a cheap health endpoint and a load shedder.

Both exist because of the crawler swarms documented in
docs/handoff-attack-mitigations.md.  They are deliberately the *first* two
entries in settings.MIDDLEWARE so that they run before sessions, auth, the
Mezzanine cache/page middleware and URL resolution -- none of which should
happen for a health check, and none of which is worth spending a worker on for
a request we are about to shed.

Neither one touches the database, the cache or a template.
"""

import ctypes
import logging
import multiprocessing
import os
import re
import socket
import time

from django.core.exceptions import DisallowedHost
from django.http import HttpResponse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------

HEALTH_PATHS = frozenset(('/healthz', '/healthz/'))


class HealthCheckMiddleware:
    """
    Answer the ALB health check without entering Django proper.

    The target group used to health-check /accounts/login/ -- a full render with
    session, DB and allauth work.  Under crawler load that check queued behind
    page traffic and timed out, so the ALB concluded the box was dead while it
    was merely busy and withdrew the only target.  On 2026-08-05 the target went
    unhealthy at 07:50 while users were still getting 200s; visible 5xx did not
    start until 08:07.

    What this still detects: the check is answered by a *worker*, so if every
    worker is genuinely wedged nothing accepts the connection and the target
    goes unhealthy as it should.  It is the load shedder below that keeps a
    worker free during a mere traffic spike, which is what makes the two
    changes a pair.  Ship them together.

    Returning before host validation is deliberate as well: the ALB addresses
    the target by IP, so a health check would otherwise depend on that IP being
    listed in ALLOWED_HOSTS -- a hidden way for an instance replacement to take
    the site down.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in HEALTH_PATHS:
            response = HttpResponse('ok\n', content_type='text/plain')
            response['Cache-Control'] = 'no-store'
            return response
        return self.get_response(request)


# --------------------------------------------------------------------------
# Load shedding
# --------------------------------------------------------------------------

# Governed traffic: GET/HEAD of project pages, sample pages, and file downloads
# reached without a referer.  Everything else -- the API, uploads, edits, logins,
# static files -- passes through untouched.  Keeping the scope this narrow is
# deliberate: the measured attacks were 93-99% sample pages, and a limiter that
# can only ever reject a page view or an unreferred download cannot break an
# upload or an API client.
_PROJECT_PAGE_RE = re.compile(r'^/project/[^/]+/?$')
_SAMPLE_PAGE_RE = re.compile(r'^/project/[^/]+/sample/[^/]+(/feature/[^/]+)?/?$')

# Every web download route under /project/: the project tarball, summary and
# metadata, per-sample downloads, and per-feature file/png/pdf downloads.  The
# REST API's own download endpoints live under /api/ and never match this.
_DOWNLOAD_RE = re.compile(
    r'^/project/[^/]+(/sample/[^/]+)?(/feature/[^/]+)?/download')

# Request classes.
_EXEMPT = 0
_PAGE = 1                 # governed by the general cap only
_PAGE_UNREFERRED = 2      # sample page with no referer; tighter cap
_DOWNLOAD_UNREFERRED = 3  # file download reached directly; its own cap

_WORKERS = int(os.getenv('GUNICORN_WORKERS', '9'))

# Cap on concurrent governed page requests.  Sync workers run one request each
# (gunicorn_config.py sets threads=1), so in-flight page concurrency equals the
# number of workers inside a page view; capping it at roughly two-thirds leaves
# the rest for uploads, the API and the health check.  Requests past the cap get
# an immediate 503, which costs a worker microseconds rather than seconds.
PAGE_CONCURRENCY = int(os.getenv('AMPREPO_PAGE_CONCURRENCY', str(max(1, (_WORKERS * 2) // 3))))

# Much tighter cap for sample pages arriving with no same-origin referer.  Real
# users nearly always reach a sample page by navigating from another page on the
# site; crawlers generally send no Referer at all.  This is the only signal that
# separates both observed attack shapes from real traffic.
UNREFERRED_CONCURRENCY = int(os.getenv('AMPREPO_UNREFERRED_PAGE_CONCURRENCY', '2'))

# Downloads reached directly, with no referer and no page visit first.  Site
# owner's call 2026-08-07: arriving straight at a data file without having been
# on a site page is the shape worth limiting, and it is also the expensive one --
# read amplification through the download path is what saturated the instance's
# inbound bandwidth during the original outages.  API downloads are a separate
# matter and are not touched: they live under /api/ and are exempt by path.
#
# This cap is looser than the sample-page one and separate from it, because
# downloads are slow by nature.  Two concurrent direct downloads are ordinary at
# baseline and would sit in a sample-page-sized allowance for minutes, which
# would break the "only engages under load" property that makes the whole
# mechanism safe.
UNREFERRED_DOWNLOAD_CONCURRENCY = int(
    os.getenv('AMPREPO_UNREFERRED_DOWNLOAD_CONCURRENCY', '3'))

# A slot whose request started longer ago than this is assumed to belong to a
# worker that was killed mid-request (gunicorn SIGKILLs on timeout, which is
# exactly what happens during the incidents this defends against).  Without an
# expiry those slots would leak until the cap reached zero and the site shed
# everything -- worse than the disease.
SLOT_STALE_SECONDS = float(os.getenv('AMPREPO_SLOT_STALE_SECONDS', '120'))

ENABLED = os.getenv('AMPREPO_LOAD_SHED', 'on').lower() not in ('off', 'false', '0')

# Verified-crawler exemption (see _is_verified_crawler).
VERIFY_CRAWLERS = os.getenv('AMPREPO_VERIFY_CRAWLERS', 'on').lower() not in ('off', 'false', '0')

# Whether an arrival from a search results page counts as a real visit for the
# purposes of the referer tier (see _came_from_a_person).
SEARCH_REFERERS = os.getenv('AMPREPO_SEARCH_REFERERS', 'on').lower() not in ('off', 'false', '0')

# Hosts whose referer means "a person clicked a search result".  Unlike the
# crawler exemption below, nothing here is verified -- it does not need to be,
# because a forged entry only buys the general cap, not an exemption.
_SEARCH_REFERER_DOMAINS = (
    'google.com', 'bing.com', 'duckduckgo.com', 'search.yahoo.com', 'yahoo.com',
    'ecosia.org', 'startpage.com', 'baidu.com', 'yandex.com', 'yandex.ru',
    'ncbi.nlm.nih.gov', 'europepmc.org', 'semanticscholar.org',
)

# The big engines also answer on per-country domains (google.co.uk, google.de),
# which no fixed list covers.  A first-label match catches them.  It would also
# match a hostile 'google.example.com', which is why this tier is an allowance
# rather than an exemption.
_SEARCH_HOST_PREFIXES = (
    'google.', 'bing.', 'duckduckgo.', 'ecosia.', 'startpage.', 'yandex.',
    'baidu.', 'search.',
)

_RETRY_AFTER_REFERRED = '10'
_RETRY_AFTER_UNREFERRED = '60'


# --- shared slot table ----------------------------------------------------
#
# The cap has to hold across processes, and with sync workers a process-local
# counter would cap at one request per worker -- i.e. do nothing.  gunicorn runs
# with preload_app = True, so this module is imported in the master and these
# shared-memory arrays are inherited by every forked worker.
#
# If preload_app is ever turned off, each worker gets its own private table and
# the effective cap becomes per-worker: the limiter fails *open*, never closed.
#
# Layout: one row per slot, holding the owning pid, the epoch time its current
# governed request started (0.0 when idle) and that request's class.  Rows are
# claimed once per process and released by pid liveness, so a killed worker's
# row is reclaimed rather than lost.
_SLOT_COUNT = max(16, _WORKERS * 4)
_slot_pids = multiprocessing.Array(ctypes.c_int, _SLOT_COUNT, lock=False)
_slot_started = multiprocessing.Array(ctypes.c_double, _SLOT_COUNT, lock=False)
_slot_class = multiprocessing.Array(ctypes.c_byte, _SLOT_COUNT, lock=False)
_slot_lock = multiprocessing.Lock()

# The lock is only ever held for a few microseconds of arithmetic -- nothing
# inside it blocks -- but a worker SIGKILLed while holding it would leave it
# held forever, and a defense that can itself wedge the whole site is not worth
# having.  Every acquisition is therefore bounded, and a failure to acquire
# fails open: the limiter stops limiting rather than stops serving.
_LOCK_TIMEOUT = 0.02

# Index of this process's row, resolved on first governed request.
_my_slot = None
_my_slot_pid = None


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_slot():
    """Return this process's row index, claiming one if needed.

    None means the table is full, in which case the caller fails open.  With
    four rows per configured worker that should not happen; if it does, serving
    the request is the right failure.
    """
    global _my_slot, _my_slot_pid

    pid = os.getpid()
    if _my_slot is not None and _my_slot_pid == pid:
        return _my_slot

    if not _slot_lock.acquire(timeout=_LOCK_TIMEOUT):
        return None
    try:
        free = None
        for i in range(_SLOT_COUNT):
            owner = _slot_pids[i]
            if owner == pid:
                free = i
                break
            if free is None and (owner == 0 or not _pid_alive(owner)):
                free = i
        if free is None:
            return None
        _slot_pids[free] = pid
        _slot_started[free] = 0.0
        _slot_class[free] = _EXEMPT
    finally:
        _slot_lock.release()

    _my_slot, _my_slot_pid = free, pid
    return free


def _reap_dead_slots(now):
    """Clear rows owned by processes that no longer exist.

    Called only on the shed path, so the common case pays nothing: a request is
    only rejected after this has had a chance to release whatever a killed
    worker was holding.
    """
    if not _slot_lock.acquire(timeout=_LOCK_TIMEOUT):
        return
    try:
        for i in range(_SLOT_COUNT):
            if _slot_started[i] > 0.0 and not _pid_alive(_slot_pids[i]):
                _slot_started[i] = 0.0
                _slot_class[i] = _EXEMPT
    finally:
        _slot_lock.release()


def _inflight(now):
    """(total, unreferred pages, unreferred downloads) currently in flight.

    Caller holds the lock.
    """
    total = pages = downloads = 0
    for i in range(_SLOT_COUNT):
        started = _slot_started[i]
        if started > 0.0 and (now - started) < SLOT_STALE_SECONDS:
            total += 1
            if _slot_class[i] == _PAGE_UNREFERRED:
                pages += 1
            elif _slot_class[i] == _DOWNLOAD_UNREFERRED:
                downloads += 1
    return total, pages, downloads


def _try_admit(slot, request_class, now):
    """Count in flight and take the slot in one critical section.

    Counting and claiming have to be atomic together, or two workers both see
    the last free place and both take it.  Returns (admitted, counts); a lock
    timeout admits, per the fail-open rule above.

    Unreferred downloads count against the general cap as well as their own.
    Without that, a full page cap plus a full download cap could between them
    occupy every worker, and the capacity this whole mechanism exists to reserve
    would be gone.
    """
    if not _slot_lock.acquire(timeout=_LOCK_TIMEOUT):
        return True, (0, 0, 0)
    try:
        counts = _inflight(now)
        total, pages, downloads = counts
        if total >= PAGE_CONCURRENCY:
            return False, counts
        if request_class == _PAGE_UNREFERRED and pages >= UNREFERRED_CONCURRENCY:
            return False, counts
        if (request_class == _DOWNLOAD_UNREFERRED
                and downloads >= UNREFERRED_DOWNLOAD_CONCURRENCY):
            return False, counts
        _slot_started[slot] = now
        _slot_class[slot] = request_class
        return True, counts
    finally:
        _slot_lock.release()


def _release(slot):
    _slot_started[slot] = 0.0
    _slot_class[slot] = _EXEMPT


# --- verified crawler exemption -------------------------------------------

# Search indexers must keep working: they send no Referer, so without this they
# would be shed exactly like a crawler during an attack.  The check is forward-
# confirmed reverse DNS, never the user agent alone -- a UA string is trivially
# spoofed, and an attacker claiming to be Googlebot would otherwise walk
# straight through the tightest cap.
_CRAWLER_UA_HINTS = (
    'googlebot', 'bingbot', 'applebot', 'duckduckbot', 'yandexbot', 'baiduspider',
)
_CRAWLER_DOMAINS = (
    '.googlebot.com', '.google.com', '.search.msn.com', '.applebot.apple.com',
    '.duckduckgo.com', '.yandex.ru', '.yandex.net', '.yandex.com', '.baidu.com',
)

_CRAWLER_CACHE_TTL = 3600.0
_CRAWLER_CACHE_MAX = 2048
_crawler_cache = {}


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        # The ALB appends the real client; take the last hop we can trust least
        # of all, which is still the only one the ALB itself wrote.
        return forwarded.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', '')


def _is_verified_crawler(request):
    """Forward-confirmed reverse DNS on the client IP.

    Only ever called on the shed path, and cached per IP, so a spoofed-UA flood
    cannot turn this into a DNS amplifier against ourselves: the first request
    from each IP costs one lookup and every later one is a dict hit.
    """
    if not VERIFY_CRAWLERS:
        return False

    ua = request.META.get('HTTP_USER_AGENT', '').lower()
    if not any(hint in ua for hint in _CRAWLER_UA_HINTS):
        return False

    ip = _client_ip(request)
    if not ip:
        return False

    now = time.time()
    cached = _crawler_cache.get(ip)
    if cached is not None and (now - cached[1]) < _CRAWLER_CACHE_TTL:
        return cached[0]

    verified = False
    try:
        host = socket.gethostbyaddr(ip)[0].lower()
        if any(host.endswith(domain) for domain in _CRAWLER_DOMAINS):
            _, _, forward = socket.gethostbyname_ex(host)
            verified = ip in forward
    except (socket.herror, socket.gaierror, OSError):
        verified = False

    if len(_crawler_cache) >= _CRAWLER_CACHE_MAX:
        _crawler_cache.clear()
    _crawler_cache[ip] = (verified, now)
    return verified


# --- shed logging ---------------------------------------------------------

_shed_counts = {'page': 0, 'unreferred': 0, 'download': 0}
_shed_last_log = 0.0
_SHED_LOG_INTERVAL = 60.0


def _record_shed(kind, counts):
    """Count sheds, logging a summary at most once a minute.

    Per-request logging would put thousands of lines a minute into the error log
    during exactly the event when the log needs to stay readable.  These counts
    are also the feedback loop for tuning the caps: if the log shows sustained
    sheds outside an attack, a cap is too tight.
    """
    global _shed_last_log

    _shed_counts[kind] += 1
    now = time.time()
    if now - _shed_last_log < _SHED_LOG_INTERVAL:
        return
    _shed_last_log = now
    total, pages, downloads = counts
    logger.warning(
        'load shed since last report: %d page, %d unreferred-sample, '
        '%d unreferred-download (in flight: %d/%d total, %d/%d sample, '
        '%d/%d download)',
        _shed_counts['page'], _shed_counts['unreferred'], _shed_counts['download'],
        total, PAGE_CONCURRENCY, pages, UNREFERRED_CONCURRENCY,
        downloads, UNREFERRED_DOWNLOAD_CONCURRENCY,
    )
    for key in _shed_counts:
        _shed_counts[key] = 0


class LoadShedMiddleware:
    """
    Reserve worker capacity by shedding surplus page and direct-download traffic.

    Three caps:

      * a general cap at roughly two-thirds of the workers, so page traffic can
        never occupy every worker and starve uploads, the API and the health
        check;
      * a much tighter cap for sample pages carrying no same-origin Referer,
        which is where nearly all of the selectivity comes from;
      * a separate, looser cap for file downloads reached with no referer -- a
        request that arrives straight at a data file without having been on a
        site page first.  Downloads reached from a page are not capped at all.

    The limiter only engages under load.  At the ~15 req/min baseline nothing is
    ever rejected, so a sample-page URL cited in a paper works normally; only
    during an active attack might such a visitor need one retry.  That is why
    the response is 503 with Retry-After and never 403: it says "later", which
    is also what keeps search engines from treating it as a reason to deindex.

    Deliberately *not* done here: lowering gunicorn's timeout.  Sync workers do
    not heartbeat during a request, so that value has to cover the longest
    legitimate upload.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_class = self._classify(request)
        if request_class == _EXEMPT or not ENABLED:
            return self.get_response(request)

        slot = _claim_slot()
        if slot is None:
            return self.get_response(request)

        now = time.time()
        admitted, counts = _try_admit(slot, request_class, now)
        if not admitted:
            # Retry once, having first released whatever a killed worker was
            # still holding.  Nothing is rejected until that has happened.
            _reap_dead_slots(now)
            admitted, counts = _try_admit(slot, request_class, now)

        if not admitted:
            if _is_verified_crawler(request):
                return self.get_response(request)
            kind = self._shed_reason(request_class, counts)
            _record_shed(kind, counts)
            return self._busy(kind)

        try:
            return self.get_response(request)
        finally:
            _release(slot)

    @staticmethod
    def _shed_reason(request_class, counts):
        """Which cap turned this away -- for the log line and the Retry-After."""
        _, pages, downloads = counts
        if request_class == _PAGE_UNREFERRED and pages >= UNREFERRED_CONCURRENCY:
            return 'unreferred'
        if (request_class == _DOWNLOAD_UNREFERRED
                and downloads >= UNREFERRED_DOWNLOAD_CONCURRENCY):
            return 'download'
        return 'page'

    @staticmethod
    def _busy(kind):
        response = HttpResponse(
            'Server busy, please retry shortly.\n',
            content_type='text/plain',
            status=503,
        )
        response['Retry-After'] = (
            _RETRY_AFTER_REFERRED if kind == 'page' else _RETRY_AFTER_UNREFERRED
        )
        response['Cache-Control'] = 'no-store'
        return response

    @staticmethod
    def _classify(request):
        if request.method not in ('GET', 'HEAD'):
            return _EXEMPT

        path = request.path
        if _DOWNLOAD_RE.match(path):
            # A download reached from a page on the site is ordinary use and is
            # left alone entirely -- these are slow by nature and capping them
            # would reject legitimate ones.  Arriving straight at a data file is
            # the shape worth limiting.
            return _EXEMPT if _came_from_a_person(request) else _DOWNLOAD_UNREFERRED
        if _SAMPLE_PAGE_RE.match(path):
            return _PAGE if _came_from_a_person(request) else _PAGE_UNREFERRED
        if _PROJECT_PAGE_RE.match(path):
            # Deliberately not referer-tiered.  Arriving at a project page
            # directly is ordinary -- from a paper, a bookmark, a colleague's
            # email -- so a missing referer says nothing there.  It is the
            # *sample* page that a person essentially only reaches by
            # navigating, which is what makes the signal meaningful, and the
            # attacks are 93-99% sample pages anyway.  Project pages still count
            # against the general cap; they are just never singled out.
            return _PAGE
        return _EXEMPT


def _came_from_a_person(request):
    """True when the Referer suggests a human clicked a link to get here.

    Same-origin navigation sends a Referer under every default browser referrer
    policy, so ordinary in-site use is unaffected.  Privacy extensions that strip
    it cost the user one retry, and only while the site is under load.

    Search results count too.  Measured on the prod access log 2026-08-07:
    12,023 sample-page requests arrived with a `https://www.google.com/` referer,
    i.e. readers who found a sample through search and landed on it directly.
    They are exactly the visitors §4f is meant to protect, and they carry no
    same-origin referer.  A crawler could forge this, but forging it only moves
    it from the tight cap to the general one, which still bounds it -- a cheap
    trade for not shedding search arrivals during an attack.

    `www.` is ignored on both sides: the site answers on the apex and the www
    host, both appear heavily in real referers, and a reader who crosses between
    them is not a crawler.
    """
    referer = request.META.get('HTTP_REFERER')
    if not referer:
        return False
    try:
        host = request.get_host()
    except DisallowedHost:
        # A forged Host header cannot establish same origin with anything.
        return False

    # Compare hosts without importing urllib's full parser cost per request.
    remainder = referer.split('//', 1)[-1]
    referer_host = remainder.split('/', 1)[0].split('?', 1)[0].lower()
    bare = _strip_www(referer_host)
    if bare == _strip_www(host.lower()):
        return True
    if not SEARCH_REFERERS:
        return False
    if any(bare == domain or bare.endswith('.' + domain)
           for domain in _SEARCH_REFERER_DOMAINS):
        return True
    return bare.startswith(_SEARCH_HOST_PREFIXES)


def _strip_www(host):
    return host[4:] if host.startswith('www.') else host
