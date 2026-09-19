"""Building a co-amplification graph off the request worker: once per graph,
at most a few at a time.

A graph build is the most expensive thing this site does on a request.  From
prod's log: 30 s for a small project, 335 s for two large ones (concat, then
Graph construction, then the neo4j import), on one sync gunicorn worker with
a ~2 GiB peak.  Three users starting builds together would take three of
nine workers for five minutes, and three peaks on a 2.3 GiB baseline in an
8 GiB container is the OOM shape of 2026-08-29.  Usage is low (three
visualizer hits since 2026-08-30), so this is a hazard being closed, not an
incident being fixed.

Three properties, each carried by one Mongo document shape:

* **The request returns at once.**  The visualizer records a build and renders
  a waiting page that polls; the build runs as its own process
  (``manage.py coamp_build <key>``), not a thread of the worker.  A thread
  shares the worker's GIL -- the request that started one took 6.8 s to
  render its waiting page on dev -- and leaves the worker holding the
  build's peak memory as its floor (gunicorn_config.py measures that at
  2 GiB).  A process contends with nothing and gives it all back on exit,
  and a worker recycled mid-build does not take the build with it.
* **One build per graph.**  The build document's ``_id`` is the graph's
  ``cache_key``, so two people asking for the same graph share one build; the
  second sees the first's progress.
* **At most ``MAX_CONCURRENT`` builds site-wide.**  A single slot document
  holds the running keys, and a build is promoted by pushing onto that array
  only while its ``MAX_CONCURRENT``-th element does not exist -- one atomic
  update, so no two workers can both see a free slot and both take it.
  Anything beyond the cap waits as ``queued`` and is promoted when a running
  build releases its slot.

A build that dies without releasing -- the worker recycled or the container
restarted under it -- is covered by a lease: its slot entry expires and is
pulled at the next promotion, and its document reads as failed once the lease
is past.  The user is told to try again; nothing is retried on their behalf.
"""

import datetime
import logging
import os
import subprocess
import sys

import pymongo
from pymongo import ReturnDocument

from .utils import db_handle_primary, get_collection_handle

COLLECTION = 'coamp_builds'
SLOTS_ID = '#slots'   # cache keys are hex or ObjectId strings; none starts with '#'
MAX_CONCURRENT = int(os.getenv('COAMP_MAX_CONCURRENT_BUILDS', '2'))
# Longer than any build seen (335 s), by enough that a slow box under load
# does not have a live build declared dead out from under it.
LEASE_SECONDS = int(os.getenv('COAMP_BUILD_LEASE_SECONDS', str(45 * 60)))

QUEUED, RUNNING, DONE, FAILED = 'queued', 'running', 'done', 'failed'
INTERRUPTED_MESSAGE = ('The build was interrupted before it finished. '
                       'Please try again.')

# PRIMARY: the waiting page reads the state a build thread on another worker
# just wrote, and the cluster's default read preference is a replica.
builds_handle = get_collection_handle(db_handle_primary, COLLECTION)


def _now():
    return datetime.datetime.utcnow()


def ensure_indexes():
    builds_handle.create_index('finished_at', expireAfterSeconds=24 * 3600,
                               name='ix_finished_ttl')


def request_build(project_ids, cache_key=None):
    """Record that the graph for ``project_ids`` is wanted, start it if a slot
    is free, and return its build document.

    A build already queued or running for this key is joined, not repeated.
    One that finished, failed, or ran past its lease is replaced: the graph is
    not in neo4j (the caller checked), so whatever that document says is
    history.
    """
    from .neo4j_utils import generate_cache_key
    key = cache_key or generate_cache_key(project_ids)
    now = _now()
    fresh = {
        'state': QUEUED,
        'project_ids': [str(pid) for pid in project_ids],
        'requested_at': now,
        'started_at': None,
        'finished_at': None,
        'lease_until': None,
        'error': None,
    }
    doc = builds_handle.find_one_and_update(
        {'_id': key, '$or': [{'state': {'$in': [DONE, FAILED]}},
                             {'state': RUNNING, 'lease_until': {'$lt': now}}]},
        {'$set': fresh, '$unset': {'worker_pid': ''}},
        return_document=ReturnDocument.AFTER)
    if doc is None:
        try:
            builds_handle.insert_one({'_id': key, **fresh})
            doc = builds_handle.find_one({'_id': key})
        except pymongo.errors.DuplicateKeyError:
            doc = builds_handle.find_one({'_id': key})   # queued or running: join it
    start_pending()
    return builds_handle.find_one({'_id': key}) or doc


def build_status(cache_key):
    """What the waiting page shows.  ``None`` if no build was ever recorded.

    Also the safety net for promotion: if the process that should have
    started the next queued build never did, the next poll does."""
    doc = builds_handle.find_one({'_id': cache_key})
    if doc is None:
        return None
    if doc['state'] == QUEUED:
        start_pending()
        doc = builds_handle.find_one({'_id': cache_key}) or doc
    now = _now()
    state = doc['state']
    error = doc.get('error')
    if state == RUNNING and doc.get('lease_until') and doc['lease_until'] < now:
        state, error = FAILED, INTERRUPTED_MESSAGE
    queued_ahead = 0
    if state == QUEUED:
        queued_ahead = _queue_order().index(cache_key)
    since = doc.get('started_at') or doc.get('requested_at') or now
    return {
        'cache_key': cache_key,
        'state': state,
        'queued_ahead': queued_ahead,
        'running': _running_keys(now),
        'elapsed_seconds': int((now - since).total_seconds()),
        'error': error,
    }


# ---------------------------------------------------------------------------
# Slots and promotion
# ---------------------------------------------------------------------------

def _queue_order():
    """Queued keys, oldest first.  ``_id`` breaks ties: BSON dates are whole
    milliseconds, and two requests in one millisecond happen."""
    return [doc['_id'] for doc in builds_handle.find(
        {'state': QUEUED}, {'_id': 1}, sort=[('requested_at', 1), ('_id', 1)])]


def _slots():
    doc = builds_handle.find_one({'_id': SLOTS_ID})
    if doc is None:
        try:
            builds_handle.insert_one({'_id': SLOTS_ID, 'running': []})
        except pymongo.errors.DuplicateKeyError:
            pass
        doc = builds_handle.find_one({'_id': SLOTS_ID})
    return doc


def _running_keys(now):
    doc = _slots()
    return [entry['key'] for entry in doc.get('running', []) if entry['until'] >= now]


def _take_slot(key, now):
    """Push ``key`` onto the running list if it is short of the cap.  Atomic:
    the filter requires element ``MAX_CONCURRENT - 1`` to be absent, and the
    push and that check are one update."""
    # Expired entries first, so a dead build does not hold its slot forever.
    builds_handle.update_one({'_id': SLOTS_ID},
                             {'$pull': {'running': {'until': {'$lt': now}}}})
    result = builds_handle.update_one(
        {'_id': SLOTS_ID, f'running.{MAX_CONCURRENT - 1}': {'$exists': False}},
        {'$push': {'running': {'key': key,
                               'until': now + datetime.timedelta(seconds=LEASE_SECONDS)}}})
    return result.modified_count == 1


def _release_slot(key):
    builds_handle.update_one({'_id': SLOTS_ID}, {'$pull': {'running': {'key': key}}})


def _launch(cache_key):
    """Start the build process for a claimed slot.  Returns its pid.

    Detached into its own session so that gunicorn recycling a worker does
    not signal it; a container stop still ends it, and the lease covers that.
    stdout and stderr are inherited, so the build's log lines land where the
    workers' do.  S3_STATIC_FILES is unset for it so the app's ready() hook
    does not start a static-file sync per build.
    """
    manage = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'manage.py')
    env = {k: v for k, v in os.environ.items() if k != 'S3_STATIC_FILES'}
    process = subprocess.Popen([sys.executable, manage, 'coamp_build', cache_key],
                               cwd=os.path.dirname(manage), env=env,
                               start_new_session=True)
    return process.pid


def start_pending():
    """Promote queued builds into free slots and launch them.

    Every worker calls this -- after recording a request, on each status
    poll, and a build process calls it as it finishes -- so a queued build is
    picked up by whichever process next has a reason to look, without a
    scheduler.  Returns the keys started.
    """
    started = []
    while True:
        now = _now()
        _slots()
        order = _queue_order()
        if not order:
            break
        key = order[0]
        if not _take_slot(key, now):
            break
        claimed = builds_handle.find_one_and_update(
            {'_id': key, 'state': QUEUED},
            {'$set': {'state': RUNNING, 'started_at': now,
                      'lease_until': now + datetime.timedelta(seconds=LEASE_SECONDS)}},
            return_document=ReturnDocument.AFTER)
        if claimed is None:
            # Another worker claimed it between our find and our update; the
            # slot we took is for a build we are not running.
            _release_slot(key)
            continue
        try:
            pid = _launch(key)
        except Exception as e:
            logging.exception("could not launch the co-amplification build for %s", key)
            builds_handle.update_one({'_id': key}, {'$set': {
                'state': FAILED, 'finished_at': _now(),
                'error': f'The build could not be started: {type(e).__name__}: {e}'}})
            _release_slot(key)
            continue
        builds_handle.update_one({'_id': key}, {'$set': {'build_pid': pid}})
        started.append(key)
    return started


# ---------------------------------------------------------------------------
# The build itself
# ---------------------------------------------------------------------------

def run_build(cache_key, project_ids):
    """concat -> Graph -> neo4j -> edge CSV, then release the slot and start
    whatever is queued.  Runs in the build process (the management command);
    imports are deferred because views imports this module."""
    from .views import concat_projects, _save_coamp_edges
    from .neo4j_utils import load_graph
    started = _now()
    error = None
    try:
        projects_df, _ = concat_projects(project_ids)
        if projects_df.empty:
            error = 'No valid data found in selected projects.'
        else:
            graph = load_graph(projects_df, project_ids=project_ids)
            if not hasattr(graph, 'get_edges_dataframe'):
                # load_graph's own "no nodes" return
                error = 'Graph construction failed: no genes matched the annotation.'
            else:
                _save_coamp_edges(cache_key, graph)
    except Exception as e:
        logging.exception("co-amplification build failed for %s", cache_key)
        error = f'{type(e).__name__}: {e}'
    finally:
        finished = _now()
        builds_handle.update_one(
            {'_id': cache_key},
            {'$set': {'state': FAILED if error else DONE, 'error': error,
                      'finished_at': finished}})
        _release_slot(cache_key)
        logging.info("[PERF] co-amplification build %s for %s in %.1f s (%d projects)",
                     'failed' if error else 'done', cache_key,
                     (finished - started).total_seconds(), len(project_ids))
        try:
            start_pending()
        except Exception:
            logging.exception("could not promote the next co-amplification build")
    return error is None
