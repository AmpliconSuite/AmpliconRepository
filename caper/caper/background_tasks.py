"""
Background task tracking for the Caper application.

Provides a tracked ThreadPoolExecutor so the admin UI and API can report
whether any project-create or project-edit operations are currently running.
"""

import threading
import uuid
import datetime
from concurrent.futures import ThreadPoolExecutor


class BackgroundTaskTracker:
    """
    Thin wrapper around :class:`~concurrent.futures.ThreadPoolExecutor` that
    records every submitted task together with a human-readable label and start
    time.  Finished tasks are lazily removed on the next call to
    :meth:`submit` or :meth:`get_status`.
    """

    def __init__(self, max_workers: int = 4, thread_name_prefix: str = 'caper_worker'):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = threading.Lock()
        self._tasks: dict = {}   # task_id -> {label, future, started_at}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def submit(self, fn, *args, task_label: str = None, **kwargs):
        """Submit *fn* to the thread pool.

        ``task_label`` is intercepted by this wrapper and **not** forwarded to
        *fn*.  All other positional and keyword arguments are passed through
        unchanged.
        """
        if task_label is None:
            task_label = getattr(fn, '__name__', str(fn))

        task_id = uuid.uuid4().hex
        started_at = datetime.datetime.now().isoformat(timespec='seconds')

        future = self._executor.submit(fn, *args, **kwargs)

        with self._lock:
            self._purge_done()
            self._tasks[task_id] = {
                'label': task_label,
                'future': future,
                'started_at': started_at,
            }

        return future

    def get_status(self) -> dict:
        """Return a serialisable status snapshot.

        Returns a dict with:

        * ``is_busy``       – ``True`` when at least one task is not yet done
        * ``active_count``  – number of running/pending tasks
        * ``max_workers``   – size of the underlying thread pool
        * ``tasks``         – list of task dicts (id, label, state, started_at)
        """
        with self._lock:
            self._purge_done()

            active = []
            for tid, info in self._tasks.items():
                f = info['future']
                if not f.done():
                    if f.running():
                        state = 'running'
                    elif f.cancelled():
                        state = 'cancelled'
                    else:
                        state = 'pending'
                    active.append({
                        'id': tid,
                        'label': info['label'],
                        'state': state,
                        'started_at': info['started_at'],
                    })

        return {
            'is_busy': len(active) > 0,
            'active_count': len(active),
            'max_workers': self._executor._max_workers,
            'tasks': active,
        }

    # ------------------------------------------------------------------
    # Delegation to the underlying executor
    # ------------------------------------------------------------------

    def shutdown(self, wait: bool = True):
        return self._executor.shutdown(wait=wait)

    def __getattr__(self, name):
        # Delegate any attribute not defined here to the real executor so that
        # code that accesses e.g. _max_workers still works.
        return getattr(self._executor, name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _purge_done(self):
        """Remove finished tasks (must be called with _lock held)."""
        done_keys = [k for k, v in self._tasks.items() if v['future'].done()]
        for k in done_keys:
            del self._tasks[k]


# ---------------------------------------------------------------------------
# Module-level singleton – import this in views.py and views_apis.py
# ---------------------------------------------------------------------------

_thread_executor = BackgroundTaskTracker(max_workers=4, thread_name_prefix='caper_worker')


def get_background_task_status() -> dict:
    """Return a status dict for the global background task executor.

    Safe to call from any thread.  Can be imported without risk of circular
    imports since this module has no Django-app-level dependencies.
    """
    return _thread_executor.get_status()

