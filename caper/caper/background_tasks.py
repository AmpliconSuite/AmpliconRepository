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


class BackgroundTaskTracker:
    """
    Thin wrapper around :class:`~concurrent.futures.ThreadPoolExecutor` that
    records every submitted task in MongoDB so all gunicorn workers can see
    the same status.
    """

    def __init__(self, max_workers: int = 4, thread_name_prefix: str = 'caper_worker'):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._max_workers = max_workers
        self._col = None  # lazy

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

    def submit(self, fn, *args, task_label: str = None, **kwargs):
        """Submit *fn* to the thread pool and record it in MongoDB."""
        if task_label is None:
            task_label = getattr(fn, '__name__', str(fn))

        task_id = uuid.uuid4().hex
        now = datetime.datetime.utcnow()

        # Write to MongoDB first so it's visible immediately
        col = self._collection()
        if col is not None:
            try:
                col.insert_one({
                    '_id': task_id,
                    'label': task_label,
                    'state': 'running',
                    'started_at': now.isoformat(timespec='seconds'),
                    'updated_at': now,
                    'worker_pid': os.getpid(),
                })
            except Exception:
                logging.exception("Failed to insert background task record")

        def _wrapped():
            try:
                fn(*args, **kwargs)
                self._mark_task(task_id, 'completed')
            except Exception as exc:
                self._mark_task(task_id, 'failed')
                raise

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


# ---------------------------------------------------------------------------
# Module-level singleton – import this in views.py and views_apis.py
# ---------------------------------------------------------------------------

_thread_executor = BackgroundTaskTracker(max_workers=4, thread_name_prefix='caper_worker')


def get_background_task_status() -> dict:
    """Return a status dict for the global background task executor.

    Reads from MongoDB so the result is consistent across all gunicorn workers.
    """
    return _thread_executor.get_status()
