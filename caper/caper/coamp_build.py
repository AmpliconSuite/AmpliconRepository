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
  a waiting page that polls; the build runs on the worker's
  ``BackgroundTaskTracker`` thread pool, where uploads already run.
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
    """What the waiting page shows.  ``None`` if no build was ever recorded."""
    doc = builds_handle.find_one({'_id': cache_key})
    if doc is None:
        return None
    now = _now()
    state = doc['state']
    error = doc.get('error')
    if state == RUNNING and doc.get('lease_until') and doc['lease_until'] < now:
        state, error = FAILED, INTERRUPTED_MESSAGE
    queued_ahead = 0
    if state == QUEUED:
        queued_ahead = builds_handle.count_documents(
            {'state': QUEUED, 'requested_at': {'$lt': doc['requested_at']}})
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


def start_pending():
    """Promote queued builds into free slots and run them here.

    Every worker calls this -- after recording a request, and after finishing
    a build -- so a queued build is picked up by whichever worker next has a
    reason to look, without a scheduler process.  Returns the keys started.
    """
    from .background_tasks import _thread_executor
    started = []
    while True:
        now = _now()
        _slots()
        candidate = builds_handle.find_one({'state': QUEUED}, sort=[('requested_at', 1)])
        if candidate is None:
            break
        key = candidate['_id']
        if not _take_slot(key, now):
            break
        claimed = builds_handle.find_one_and_update(
            {'_id': key, 'state': QUEUED},
            {'$set': {'state': RUNNING, 'started_at': now, 'worker_pid': os.getpid(),
                      'lease_until': now + datetime.timedelta(seconds=LEASE_SECONDS)}},
            return_document=ReturnDocument.AFTER)
        if claimed is None:
            # Another worker claimed it between our find and our update; the
            # slot we took is for a build we are not running.
            _release_slot(key)
            continue
        _thread_executor.submit(run_build, key, claimed['project_ids'],
                                task_label='coamp_graph_build')
        started.append(key)
    return started


# ---------------------------------------------------------------------------
# The build itself
# ---------------------------------------------------------------------------

def run_build(cache_key, project_ids):
    """concat -> Graph -> neo4j -> edge CSV, then release the slot and start
    whatever is queued.  Imports are deferred: views imports this module."""
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
