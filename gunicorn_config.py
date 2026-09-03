"""
Gunicorn configuration file for the Amplicon Repository Django application.
Optimized for AWS t4g.2xlarge (8 vCPUs, 32GB RAM)
"""
import os

# Server socket
bind = "0.0.0.0:8000"
# Accept-queue depth: connections that have connected but no worker has picked
# up yet.  This is a burst buffer, not capacity -- capacity is `workers`.
# At 2048 the last client in a full queue waited ~4 minutes, long after it had
# given up; the queue simply converted overload into latency collapse and hid
# the problem from the load balancer.  512 was better but still ~2 minutes at
# the *measured* ceiling: not the theoretical ~9 req/s, but the ~200-250 req/min
# at which the box actually collapsed on 2026-08-05, because project aggregation
# threads share the same 8 vCPUs.  64 is roughly 15 seconds' worth at that rate,
# which is as long as a queued client is plausibly still waiting.  The 460s in
# the ALB logs (1,119 on Aug 6, 3,248 on Jul 31) are clients that had already
# hung up, i.e. queue depth that was never doing anyone any good.
#
# Do not ship this without the cheap /healthz endpoint (caper/middleware.py) and
# the target group repointed at it: a shorter queue makes the kernel refuse
# connections sooner, which would also trip a health check that has to queue.
backlog = int(os.getenv("GUNICORN_BACKLOG", "64"))

# Worker processes
# For t4g.2xlarge (8 vCPUs): Using 9 workers as default (CPU * 1 + 1)
# This leaves resources for the OS and Neo4j/other services
# For I/O-bound workloads, you can increase up to 17 (CPU * 2 + 1)
workers = int(os.getenv("GUNICORN_WORKERS", "9"))
worker_class = "sync"
worker_connections = 1000

# Worker lifecycle management
# With 32GB RAM, we can handle more requests before recycling
max_requests = 2000
max_requests_jitter = 100
# Increased timeout for large file uploads and long-running amplicon analysis requests
# Set to 15 minutes to accommodate large uploads (adjust higher if needed)
timeout = 900
keepalive = 5

# Preload application for better memory efficiency across workers
preload_app = True

# Logging
accesslog = "/srv/logs/gunicorn_access.log"
errorlog = "/srv/logs/gunicorn_error.log"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
# %(p)s is the worker pid.  It is here so that a request can be attributed to
# the worker that served it: memory_probe.py samples memory per worker, and
# without the pid on each request line there is no way to say which requests a
# worker was serving while it grew.  Appended rather than inserted, so anything
# reading the existing fields by position still works.
access_log_format = ('%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" '
                     '"%(a)s" %(D)s pid=%(p)s')

# Process naming.  Inert unless `setproctitle` is installed, which it is not, so
# the processes appear under their default name.  Tooling that needs to find
# them (py-spy, wedge-capture) must match on "gunicorn caper.wsgi" instead.
proc_name = "amplicon_gunicorn"

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# Memory and performance optimization for t4g.2xlarge
# Graceful timeout for worker restarts
graceful_timeout = 30

# Thread settings for better concurrency
threads = int(os.getenv("GUNICORN_THREADS", "1"))

# Worker temporary directory (use tmpfs for better performance)
worker_tmp_dir = "/dev/shm"

# SSL (if needed in the future)
# keyfile = None
# certfile = None

# Development/Debug settings
reload = os.getenv("GUNICORN_RELOAD", "false").lower() == "true"
reload_engine = "auto"



# --- returning memory to the kernel after an expensive request --------------
#
# A worker that has once served a large request keeps that footprint for the
# rest of its life. Measured 2026-09-02: three co-amplification analyses took a
# dev worker from 257 MiB to a 1.99 GiB peak, after which it settled at 609 MiB
# and stayed there. Nothing is leaked in the Python sense -- tracemalloc reports
# no growth across repeats -- but glibc keeps the freed pages in its own arenas
# rather than handing them back, so the peak becomes a floor.
#
# malloc_trim(0) is the syscall-level "give it back". In the harness it
# recovered ~39 MiB per iteration and cut the per-run residue from 6.5 MiB to
# 2.1 MiB.
#
# Two things this is NOT:
#   - It does not reduce the *peak*, only what is held afterwards, so it would
#     not by itself have prevented the container OOM of 2026-08-29.
#   - It is not free. Trimming walks the arenas, so it is gated on the worker
#     actually being large rather than run after every request; a worker serving
#     small pages never pays for it.
#
# glibc-only. On any other libc the lookup fails once and the hook disables
# itself, because a hook that raises on every request would be far worse than
# one that does nothing.

_TRIM_ABOVE_KB = int(os.getenv("AMPREPO_TRIM_ABOVE_KB", str(600 * 1024)))
_TRIM_ENABLED = os.getenv("AMPREPO_MALLOC_TRIM", "on").lower() not in (
    "off", "false", "0")
_trim_fn = None
_trim_broken = False


def _resident_kb():
    """This process's resident size, from statm -- one short read, no imports."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, ValueError, IndexError):
        return 0


def post_request(worker, req, environ, resp):
    """After a request, hand back arena pages if this worker has grown large."""
    global _trim_fn, _trim_broken
    if not _TRIM_ENABLED or _trim_broken:
        return
    try:
        if _resident_kb() < _TRIM_ABOVE_KB:
            return
        if _trim_fn is None:
            import ctypes
            _trim_fn = ctypes.CDLL("libc.so.6").malloc_trim
        before = _resident_kb()
        _trim_fn(0)
        after = _resident_kb()
        if before - after > 16 * 1024:      # only worth a line if it did something
            worker.log.info(
                "malloc_trim released %d MiB (%d -> %d MiB) after %s",
                (before - after) // 1024, before // 1024, after // 1024,
                environ.get("PATH_INFO", "?"))
    except Exception:
        # Never let this affect a response that has already been produced.
        _trim_broken = True
        try:
            worker.log.exception("malloc_trim hook disabled after an error")
        except Exception:
            pass


def post_fork(server, worker):
    """Restart any version payload purge that an interruption left unfinished.

    Deleting a version writes its tombstone synchronously and removes the
    GridFS payload afterwards, off the request thread; the ids not yet deleted
    live on the tombstone until they are gone. Both servers also restart on a
    timer and neither checks for running work, so a purge can be cut short by
    an ordinary restart rather than a fault. This is where it is picked back up.

    It runs here rather than in ``AppConfig.ready()`` because ``preload_app``
    is True: ready() runs in the master, and a thread started there does not
    survive the fork. Every worker calls this, and each pending purge is
    claimed atomically so only one of them takes it.

    Never allowed to stop a worker booting -- a site that will not start is a
    far worse outcome than a purge that waits for the next restart.
    """
    try:
        from caper.version_purge import resume_pending
        resumed = resume_pending()
        if resumed:
            server.log.info(
                "Resumed %d interrupted version payload purge(s)", resumed)
    except Exception:
        server.log.exception("Could not resume pending version payload purges")
