"""Running the GridFS ownership survey somewhere that is not a web worker.

The survey walks every project document and every ``fs.files`` row it names.
On prod that is minutes of work, and it used to run in a thread inside a
gunicorn worker. Three things were wrong with that, in increasing order of how
much they mattered:

1. It shared a process with request serving. ``preload_app`` means the worker
   is already carrying the whole application; a survey holding a cursor over
   ``fs.files`` in the same address space competes with it for memory and for
   the GIL.
2. It could only be started by loading a page. There was no way to run the same
   measurement from a shell, from cron, or against a database from a box that
   is not serving traffic.
3. Its result was only discoverable by reloading the page. A person who started
   a six-minute walk and closed the tab had no way to learn it had finished.

So the survey is a management command -- ``manage.py ownership_survey`` -- and
the page's button spawns it as a separate process. The command is the only
runner; the button is one way to call it, not a second implementation.

**The child process is not immortal, and the page must not assume it is.** It
is spawned in its own session so a gunicorn reload does not take it down, but
the container going away still does. That case is already handled by age:
``mark_abandoned_surveys`` rewrites a row that has claimed to be running for
longer than any real survey takes. Nothing here changes that, and nothing here
should be built on the assumption that a started survey finishes.

Read-only against ``projects`` and ``fs.files``. The only collection written is
the report collection, and what is written there is a measurement.
"""

import datetime
import logging
import os
import subprocess
import sys
from pathlib import Path

REPORTS_COLLECTION = 'gridfs_ownership_reports'

#: Per-project rows kept in a stored snapshot. The whole list is a few hundred
#: rows today, but a snapshot that grows without bound is a document that one
#: day cannot be written back.
PROJECT_ROWS = 200

#: A survey still claiming to be running after this long has lost its process.
ABANDONED_AFTER = datetime.timedelta(hours=1)

#: ``survey()`` calls back every 25 documents; the report row is updated on
#: every callback so a reader can see the walk advance rather than guess.
PROGRESS_EVERY = 25

MANAGE_PY = Path(__file__).resolve().parents[1] / 'manage.py'
LOG_DIR = Path(__file__).resolve().parents[2] / 'logs'
COMMAND = 'ownership_survey'


def collections():
    """``(reports, projects, fs_files)``.

    The import is function-level on purpose: ``utils`` reaches the view modules
    through its own import chain, and the view modules import this one.
    """
    from .utils import db_handle, db_handle_primary
    return (db_handle_primary[REPORTS_COLLECTION],
            db_handle['projects'], db_handle['fs.files'])


def mark_abandoned_surveys(snapshots, now=None):
    """Rewrite the state of surveys whose process went away.

    The runner records 'failed' when the survey raises, but nothing records
    anything when the process disappears underneath it -- a restart, a kill,
    a deploy. The row keeps saying 'running', which disables the Run button for
    good. Age is the only evidence left of the difference, so age is what this
    reads. Mutates the dicts in place; the stored documents are not touched,
    because the fact being corrected is about this moment, not about them.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - ABANDONED_AFTER
    for snapshot in snapshots:
        started = snapshot.get('started_at')
        if snapshot.get('state') != 'running' or not started:
            continue
        # DocumentDB hands back naive UTC; a caller passing an aware value is
        # left as it is.
        if started.tzinfo is None:
            started = started.replace(tzinfo=datetime.timezone.utc)
        if started < cutoff:
            snapshot['state'] = 'abandoned'
    return snapshots


def open_report(reports, started_by, notify=None):
    """Insert the 'running' row the survey will later fill in. Returns its id."""
    from bson.objectid import ObjectId

    report_id = ObjectId()
    reports.insert_one({
        '_id': report_id,
        'state': 'running',
        'started_at': datetime.datetime.now(datetime.timezone.utc),
        'started_by': started_by,
        'notify': notify,
        'documents_done': 0,
    })
    return report_id


def run_and_store(reports, projects, fs_files, report_id, started_by=None,
                  notify=None):
    """Walk the documents and store one snapshot under *report_id*.

    Returns the stored result, or None if the survey raised -- in which case
    the row is left saying 'failed' with the exception on it, because a failed
    measurement a reader can see beats a row that silently stays 'running'.
    """
    from .gridfs_ownership import survey

    def progress(documents_done, files_seen):
        try:
            reports.update_one({'_id': report_id},
                               {'$set': {'documents_done': documents_done,
                                         'files_seen': files_seen}})
        except Exception:
            # Losing a progress tick is not a reason to abandon the survey.
            logging.exception('Could not record survey progress')

    try:
        result = survey(projects, fs_files, progress=progress)
    except Exception as exc:
        logging.exception('GridFS ownership survey failed')
        reports.update_one({'_id': report_id},
                           {'$set': {'state': 'failed',
                                     'error': f'{type(exc).__name__}: {exc}'}})
        _notify(reports, report_id, notify, failed=f'{type(exc).__name__}: {exc}')
        return None

    result['per_project'] = result['per_project'][:PROJECT_ROWS]
    result['state'] = 'done'
    if started_by:
        result['started_by'] = started_by
    reports.update_one({'_id': report_id}, {'$set': result})
    _notify(reports, report_id, notify, result=result)
    return result


def spawn(report_id, started_by=None, notify=None):
    """Start the command in its own process. Returns the argv, or None.

    The child inherits this process's environment, which is where the database
    URI and the mail credentials live -- they are sourced into the gunicorn
    workers at start-up and are not in PID 1's environment, so inheriting is
    the only way the child gets them.
    """
    argv = [sys.executable, str(MANAGE_PY), COMMAND,
            '--report-id', str(report_id)]
    if started_by:
        argv += ['--started-by', str(started_by)]
    if notify:
        argv += ['--notify', str(notify)]
    try:
        subprocess.Popen(argv, cwd=str(MANAGE_PY.parent),
                         stdin=subprocess.DEVNULL,
                         stdout=_child_log(), stderr=subprocess.STDOUT,
                         start_new_session=True, env=os.environ.copy())
        return argv
    except Exception:
        logging.exception('Could not start the ownership survey process')
        return None


def _child_log():
    """Somewhere for the child's traceback to land, if there is anywhere."""
    try:
        if LOG_DIR.is_dir():
            return open(LOG_DIR / 'ownership_survey.log', 'a')
    except Exception:
        pass
    return subprocess.DEVNULL


def _notify(reports, report_id, address, result=None, failed=None):
    """Tell the person who started it that it is over. Never raises.

    A survey outlives the page that started it, so the result has to reach
    someone who is no longer looking at a browser tab. A mail that does not
    send must not lose the snapshot: the measurement is the point, and the
    message is a convenience.
    """
    if not address:
        return
    try:
        from django.conf import settings
        from django.core.mail import EmailMessage

        site = getattr(settings, 'SITE_TITLE', 'AmpliconRepository')
        url = getattr(settings, 'SITE_URL', '').rstrip('/')
        link = f'{url}/admin-file-ownership/?snapshot={report_id}'
        if failed:
            subject = f'[{site}] GridFS ownership survey failed'
            body = (f'The survey you started did not finish.\n\n{failed}\n\n'
                    f'{link}\n')
        else:
            counts = result.get('counts') or {}
            lines = '\n'.join(f'  {label}: {count:,}'
                              for label, count in counts.items())
            subject = f'[{site}] GridFS ownership survey finished'
            body = (f"{result.get('documents', 0):,} project documents, "
                    f"{result.get('total_files', 0):,} files.\n\n"
                    f'{lines}\n\n{link}\n')
        EmailMessage(subject, body, settings.EMAIL_HOST_USER_SECRET,
                     [address]).send(fail_silently=True)
    except Exception:
        logging.exception('Could not send the ownership survey notification')
