"""
Background task tracking for the Caper application.

Provides a tracked ThreadPoolExecutor that stores task status in MongoDB
so that all gunicorn workers share a consistent view of running tasks.
"""

import logging
import threading
import uuid
import datetime
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor


def _get_tasks_collection():
    """Lazily obtain the MongoDB background_tasks collection.

    We import here (not at module level) to avoid circular imports –
    utils.py imports from this module.
    """
    from .utils import get_db_handle, get_collection_handle
    from pymongo import ReadPreference
    db, _ = get_db_handle(
        os.getenv('DB_NAME', default='caper'),
        os.environ['DB_URI_SECRET'],
        read_preference=ReadPreference.PRIMARY,
    )
    return get_collection_handle(db, 'background_tasks')


# How long before a 'running' task is considered stale/dead (seconds).
_STALE_THRESHOLD_SECONDS = 20 * 60  # 20 minutes

# Root directory where project temp dirs are created.
_DEFAULT_TMP_ROOT = os.getenv('CAPER_TMP_ROOT', './tmp')

# Periodic cleanup interval: every 6 hours.
_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

# Minimum dir age before the cleanup daemon will touch it.
# 3× the stale threshold (60 min) gives active tasks a generous safety buffer.
_CLEANUP_MIN_AGE_SECONDS = _STALE_THRESHOLD_SECONDS * 3


def _remove_temp_dir(path: str) -> None:
    """Remove a temp directory if it exists, logging the outcome."""
    if not path:
        return
    try:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            logging.info(f"Removed temp directory: {path}")
    except Exception:
        logging.exception(f"Failed to remove temp directory: {path}")


def cleanup_stale_temp_dirs(
    tmp_root: str = _DEFAULT_TMP_ROOT,
    min_age_seconds: int = _CLEANUP_MIN_AGE_SECONDS,
) -> int:
    """
    Remove directories in tmp_root that have no corresponding running task in
    MongoDB and whose mtime is older than min_age_seconds.

    The mtime-based age check provides a secondary safety net: a directory
    that an active aggregation is still writing to will have a recent mtime
    and will not be removed even if its MongoDB record is temporarily invisible.

    Returns the number of directories removed.
    """
    tmp_root = os.path.abspath(tmp_root)
    if not os.path.isdir(tmp_root):
        return 0

    # Collect the absolute temp_dir paths of all currently-running tasks.
    active_temp_dirs: set[str] = set()
    try:
        col = _get_tasks_collection()
        for doc in col.find({'state': 'running', 'temp_dir': {'$exists': True, '$ne': None}}):
            td = doc.get('temp_dir')
            if td:
                active_temp_dirs.add(os.path.abspath(td))
    except Exception:
        logging.exception("cleanup_stale_temp_dirs: failed to query MongoDB — skipping cleanup")
        return 0

    cutoff = time.time() - min_age_seconds
    removed = 0

    try:
        with os.scandir(tmp_root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                        continue  # Too recent — still potentially active
                    abs_path = os.path.abspath(entry.path)
                    if abs_path in active_temp_dirs:
                        continue  # Claimed by a running task
                    shutil.rmtree(abs_path, ignore_errors=True)
                    logging.info(f"cleanup_stale_temp_dirs: removed orphaned temp dir {abs_path}")
                    removed += 1
                except Exception:
                    logging.exception(f"cleanup_stale_temp_dirs: error processing {entry.path}")
    except OSError:
        logging.exception(f"cleanup_stale_temp_dirs: cannot scan {tmp_root}")

    if removed:
        logging.info(f"cleanup_stale_temp_dirs: removed {removed} orphaned dir(s) from {tmp_root}")
    else:
        logging.debug(f"cleanup_stale_temp_dirs: no orphaned dirs found in {tmp_root}")
    return removed


class BackgroundTaskTracker:
    """
    Thin wrapper around :class:`~concurrent.futures.ThreadPoolExecutor` that
    records every submitted task in MongoDB so all gunicorn workers can see
    the same status.
    """

    def __init__(
        self,
        max_workers: int = 4,
        thread_name_prefix: str = 'caper_worker',
        cleanup_interval_seconds: int = _CLEANUP_INTERVAL_SECONDS,
        tmp_root: str = _DEFAULT_TMP_ROOT,
    ):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._max_workers = max_workers
        self._col = None  # lazy
        self._cleanup_interval = cleanup_interval_seconds
        self._tmp_root = tmp_root
        self._start_cleanup_daemon()

    def _collection(self):
        if self._col is None:
            try:
                self._col = _get_tasks_collection()
                # Ensure TTL index so completed/failed docs are cleaned up automatically
                self._col.create_index('updated_at', expireAfterSeconds=3600)
            except Exception:
                logging.exception("Could not connect to MongoDB for background task tracking")
        return self._col

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def submit(self, fn, *args, task_label: str = None, temp_dir: str = None, **kwargs):
        """Submit *fn* to the thread pool and record it in MongoDB.

        temp_dir: if provided, its absolute path is stored in the task record
        so the periodic cleanup daemon can skip directories that belong to
        running tasks.  The directory is also removed in a finally block after
        fn returns, catching the success path which the existing code does not
        clean up.
        """
        if task_label is None:
            task_label = getattr(fn, '__name__', str(fn))

        task_id = uuid.uuid4().hex
        now = datetime.datetime.utcnow()

        doc = {
            '_id': task_id,
            'label': task_label,
            'state': 'running',
            'started_at': now.isoformat(timespec='seconds'),
            'updated_at': now,
            'worker_pid': os.getpid(),
        }
        if temp_dir is not None:
            doc['temp_dir'] = os.path.abspath(temp_dir)

        # Write to MongoDB first so it's visible immediately
        col = self._collection()
        if col is not None:
            try:
                col.insert_one(doc)
            except Exception:
                logging.exception("Failed to insert background task record")

        abs_temp_dir = os.path.abspath(temp_dir) if temp_dir is not None else None

        def _wrapped():
            try:
                fn(*args, **kwargs)
                self._mark_task(task_id, 'completed')
            except Exception:
                self._mark_task(task_id, 'failed')
                raise
            finally:
                # Belt-and-suspenders: the success path in
                # _process_and_aggregate_files does not call rmtree, so this
                # finally block is the primary cleanup for the happy path.
                # Error paths that already called rmtree are harmless repeats.
                if abs_temp_dir is not None:
                    _remove_temp_dir(abs_temp_dir)

        future = self._executor.submit(_wrapped)
        return future

    def get_status(self) -> dict:
        """Return a serialisable status snapshot from MongoDB."""
        col = self._collection()
        if col is None:
            return {
                'is_busy': False,
                'active_count': 0,
                'max_workers': self._max_workers,
                'tasks': [],
            }

        # Purge stale tasks (tasks stuck as 'running' beyond the threshold)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=_STALE_THRESHOLD_SECONDS)
        try:
            col.update_many(
                {'state': 'running', 'updated_at': {'$lt': cutoff}},
                {'$set': {'state': 'stale', 'updated_at': datetime.datetime.utcnow()}},
            )
        except Exception:
            logging.exception("Failed to purge stale background tasks")

        active = []
        try:
            for doc in col.find({'state': 'running'}):
                active.append({
                    'id': doc['_id'],
                    'label': doc.get('label', ''),
                    'state': doc.get('state', 'running'),
                    'started_at': doc.get('started_at', ''),
                    'worker_pid': doc.get('worker_pid'),
                })
        except Exception:
            logging.exception("Failed to query background tasks")

        return {
            'is_busy': len(active) > 0,
            'active_count': len(active),
            'max_workers': self._max_workers,
            'tasks': active,
        }

    # ------------------------------------------------------------------
    # Delegation to the underlying executor
    # ------------------------------------------------------------------

    def shutdown(self, wait: bool = True):
        return self._executor.shutdown(wait=wait)

    def __getattr__(self, name):
        return getattr(self._executor, name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _mark_task(self, task_id: str, state: str):
        """Update task state in MongoDB."""
        col = self._collection()
        if col is not None:
            try:
                col.update_one(
                    {'_id': task_id},
                    {'$set': {'state': state, 'updated_at': datetime.datetime.utcnow()}},
                )
            except Exception:
                logging.exception(f"Failed to mark background task {task_id} as {state}")

    def _start_cleanup_daemon(self):
        """Start a daemon thread that sweeps tmp_root every cleanup_interval seconds."""
        interval = self._cleanup_interval
        tmp_root = self._tmp_root

        def _loop():
            while True:
                time.sleep(interval)
                try:
                    cleanup_stale_temp_dirs(tmp_root)
                except Exception:
                    logging.exception("Periodic temp dir cleanup failed")

        t = threading.Thread(target=_loop, daemon=True, name='caper_tmp_cleanup')
        t.start()
        logging.info(
            f"Started temp dir cleanup daemon "
            f"(interval={interval}s, root={os.path.abspath(tmp_root)})"
        )


# ---------------------------------------------------------------------------
# Module-level singleton – import this in views.py and views_apis.py
# ---------------------------------------------------------------------------

_thread_executor = BackgroundTaskTracker(max_workers=4, thread_name_prefix='caper_worker')


def get_background_task_status() -> dict:
    """Return a status dict for the global background task executor.

    Reads from MongoDB so the result is consistent across all gunicorn workers.
    """
    return _thread_executor.get_status()
