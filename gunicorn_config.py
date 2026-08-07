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
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

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

