"""Removing a deleted version's payload, after its tombstone is written.

**Why this exists.** Deleting a version used to purge its GridFS payload first
and write the tombstone afterwards, all inside the web request. A version's
payload can take longer to delete than a request is allowed to live -- about
``96.4 * GiB + 0.035 * files`` seconds, against gunicorn's ``timeout = 900`` --
and when the worker is killed the tombstone is never written. Measured on dev
2026-09-02, deleting version 2 of a five-version PCAWG chain: the event was
recorded at 16:49:30, the worker was killed at 17:04:31 (901 s), **15,272 of
15,733 files were deleted**, and the document was left classified SUPERSEDED
with no tombstone markers and no ``redirect_to_project``. The site presented an
ordinary superseded version whose payload was 97% gone, and nothing looked for
that state: a SIGKILLed worker writes no traceback.

**The order is now inverted.** The tombstone is written first, synchronously,
because it is one document write and it is the part a user is waiting on. The
payload is then removed here, off the request thread, where no worker timeout
applies.

**Which makes the ids the thing to keep.** A tombstone is built fresh and does
not carry the payload keys, so after it is written nothing else knows which
files that version owned. They are therefore recorded on the tombstone itself
under ``PENDING_PAYLOAD_KEY`` and removed from it as they go. An interrupted
purge is then resumable by name rather than a matter for a global orphan sweep
-- which would not have found it anyway, since a sweep only considers files no
document references, and until this field is cleared the tombstone references
them.

**Resumption is per worker, and claimed.** Both servers restart on a timer
(dev 00:15, prod 07:12 UTC) and neither checks for running work, so a purge can
be interrupted by something other than a crash. ``resume_pending()`` runs from
gunicorn's ``post_fork`` hook -- not from ``AppConfig.ready()``, which under
``preload_app = True`` runs in the master, whose threads do not survive the
fork. Every worker calls it, so each pending purge is claimed atomically and
only one worker takes it.
"""

import datetime
import logging

from bson import ObjectId

from . import provenance
from .background_tasks import _thread_executor
from .project_version_cleanup import (
    PENDING_PAYLOAD_KEY,
    PURGE_CLAIM_KEY,
    PURGE_CLAIM_STALE_SECONDS,
    delete_payload_file_ids,
    iter_gridfs_file_ids,
)


def payload_ids_to_purge(victim, protected_file_ids=None):
    """The file ids deleting *victim* should remove, protected ones excluded.

    Read from the document **before** its tombstone replaces it, which is the
    only moment the payload keys are still there.
    """
    protected = {str(file_id) for file_id in (protected_file_ids or set())}
    seen, wanted = set(), []
    for file_id in iter_gridfs_file_ids(victim):
        key = str(file_id)
        if key in seen or key in protected:
            continue
        seen.add(key)
        wanted.append(file_id)
    return wanted


def _clear_pending(victim_id, file_ids):
    """Take *file_ids* off the tombstone's pending list, and the claim with it."""
    try:
        from .utils import collection_handle
        collection_handle.update_one(
            {'_id': ObjectId(str(victim_id))},
            {'$pull': {PENDING_PAYLOAD_KEY: {'$in': list(file_ids)}},
             '$unset': {PURGE_CLAIM_KEY: ''}})
    except Exception:
        logging.exception(
            "Could not clear the pending payload list of %s; the purge itself "
            "may have succeeded, and a later resume will retry the ids that "
            "are already gone, which is harmless.", victim_id)


def _run(victim_id, file_ids, delete_event, outcome, confirm_extra):
    """Delete the payload, clear the pending list, then confirm the event.

    The event is confirmed last and only on the way out, so a purge that never
    finishes leaves ``completed: False`` -- which is the signature that found
    the incident this module was written for.
    """
    from .utils import delete_gridfs_file

    result = delete_payload_file_ids(delete_gridfs_file, file_ids,
                                     owner=victim_id)
    _clear_pending(victim_id, file_ids)
    logging.info("Purged %d GridFS file(s) of deleted version %s",
                 result.deleted, victim_id)

    if delete_event is not None:
        from .utils import audit_log_handle
        provenance.confirm(audit_log_handle, delete_event,
                           outcome=outcome,
                           gridfs_files_purged=result.deleted,
                           **(confirm_extra or {}))
    return result.deleted


def start(victim_id, file_ids, delete_event=None, outcome=None, **confirm_extra):
    """Purge *file_ids* off the request thread. Returns immediately.

    Nothing is submitted for an empty list -- a version with no payload is
    already finished -- but the event is still confirmed, because the deletion
    itself did happen.
    """
    file_ids = list(file_ids or ())
    if not file_ids:
        if delete_event is not None:
            from .utils import audit_log_handle
            provenance.confirm(audit_log_handle, delete_event, outcome=outcome,
                               gridfs_files_purged=0, **confirm_extra)
        return None
    return _thread_executor.submit(
        _run, victim_id, file_ids, delete_event, outcome, confirm_extra,
        task_label=f'Version payload purge: {victim_id}')


def _claim_one(collection, cutoff, now):
    return collection.find_one_and_update(
        {PENDING_PAYLOAD_KEY: {'$exists': True, '$ne': []},
         '$or': [{PURGE_CLAIM_KEY: {'$exists': False}},
                 {PURGE_CLAIM_KEY: {'$lt': cutoff}}]},
        {'$set': {PURGE_CLAIM_KEY: now}},
        projection={PENDING_PAYLOAD_KEY: 1})


def resume_pending(limit=25, collection=None):
    """Restart every payload purge an interruption left unfinished.

    Called by each gunicorn worker as it starts. The claim makes that safe to
    do from all of them: a document is only handed out once, and a claim older
    than an hour is treated as abandoned so a worker killed mid-purge does not
    strand its own work.

    Returns how many purges this caller took on.
    """
    if collection is None:
        from .utils import collection_handle
        collection = collection_handle

    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(seconds=PURGE_CLAIM_STALE_SECONDS)
    resumed = 0
    for _ in range(limit):
        doc = _claim_one(collection, cutoff, now)
        if doc is None:
            break
        pending = doc.get(PENDING_PAYLOAD_KEY) or []
        logging.warning(
            "Resuming an interrupted payload purge for deleted version %s: "
            "%d file(s) were never removed.", doc['_id'], len(pending))
        start(str(doc['_id']), pending)
        resumed += 1
    return resumed
