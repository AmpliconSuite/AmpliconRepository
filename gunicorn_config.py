"""
Gunicorn configuration file for the Amplicon Repository Django application.
Optimized for AWS t4g.2xlarge (8 vCPUs, 32GB RAM)
"""
import os

# Server socket
bind = "0.0.0.0:8000"
backlog = 2048

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
timeout = 180  # Increased for potential long-running amplicon analysis requests
keepalive = 5

# Preload application for better memory efficiency across workers
preload_app = True

# Logging
accesslog = "/srv/logs/gunicorn_access.log"
errorlog = "/srv/logs/gunicorn_error.log"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
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

