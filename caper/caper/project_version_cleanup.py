import collections
import logging
import time

from bson import ObjectId

from .download_totals import DATED_COUNTERS
from .project_status import STATUS_QUERIES, TOMBSTONE, combine, status_flags


# Per-feature keys that hold a GridFS file id, in the order ingestion writes
# them.  This is the single source of truth: views.py iterates this list when
# uploading, and the cleanup paths below derive their key set from it, so the
# two cannot drift apart.  A key missing here is a file the site uploads and
# then never deletes — that is how the existing orphan population accumulated.
#
# Aggregator <=6 emitted the AA-prefixed image/text keys.  7.0 emits distinct
# graph/cycles artifacts.  Both schemas are live, so both are listed.
FEATURE_FILE_KEYS = (
    'Feature BED file',
    'CNV BED file',
    'AA PDF file',
    'AA PNG file',
    'Graph PNG file',
    'Graph PDF file',
    'Cycles PNG file',
    'Cycles PDF file',
    'AA graph file',
    'AA cycles file',
    'Graph file',
    'Cycles file',
    'Run metadata JSON',
    'Sample metadata JSON',
)

# Directory-shaped payloads, tarred into GridFS by the same code path.
DIRECTORY_FILE_KEYS = (
    'Reconstruction directory',
    'AA directory',
    'cnvkit directory',
)

# Project-level keys.
PROJECT_FILE_KEYS = ('tarfile',)


def _with_underscore_variants(keys):
    """Documents are stored with spaces in keys replaced by underscores in some
    code paths and left as-is in others, so both spellings must be recognised."""
    variants = set()
    for key in keys:
        variants.add(key)
        variants.add(key.replace(' ', '_'))
    return variants


GRIDFS_FILE_KEYS = _with_underscore_variants(
    FEATURE_FILE_KEYS + DIRECTORY_FILE_KEYS + PROJECT_FILE_KEYS
)

VERSION_HISTORY_FIELDS = (
    'AA_version',
    'AC_version',
    'ASP_version',
    'aggregator_version',
)


GRIDFS_DELETE_BATCH = 200
"""Chunks removed per delete command by ``delete_gridfs_file_in_batches()``.

At the 255 KiB default chunk size this is about 50 MiB of deletes per command,
which leaves a wide margin under the 120 s socket timeout."""


def delete_gridfs_file_in_batches(files_collection, chunks_collection, file_id,
                                  batch_size=GRIDFS_DELETE_BATCH):
    """Delete one GridFS file, removing its chunks a batch at a time.

    ``gridfs.GridFS.delete()`` removes every chunk in a single ``delete_many``.
    For a multi-gigabyte tarfile that one command runs past the driver's 120 s
    socket timeout, so the driver raises ``NetworkTimeout`` while the server
    goes on deleting: the caller is told the delete failed for work that in
    fact succeeded.  Measured on dev 2026-08-27, a 2.25 GiB tarfile is 9,270
    chunks, and the largest project on the admin delete page timed out this way.

    Deleting in batches keeps every command well under the timeout and makes
    the progress durable: a batch that fails leaves only the chunks it never
    reached, so calling again resumes instead of starting over.

    The collections are arguments rather than module state because the site and
    the standalone cleanup scripts open their own connections.  Every one of
    them reached for ``GridFS.delete()`` separately, so the bug was in all of
    them at once; taking the collections here is what lets them share one fix.

    Returns the number of chunks removed.
    """
    oid = file_id if isinstance(file_id, ObjectId) else ObjectId(str(file_id))

    # The file document goes first, the same order gridfs itself uses, so a
    # partly deleted file can never be opened and read as though it were whole.
    files_collection.delete_one({'_id': oid})

    removed = 0
    while True:
        batch = [c['_id'] for c in
                 chunks_collection.find({'files_id': oid}, {'_id': 1}).limit(batch_size)]
        if not batch:
            return removed
        removed += chunks_collection.delete_many({'_id': {'$in': batch}}).deleted_count


def object_id_from_gridfs_value(value):
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return None


def iter_gridfs_file_ids(value, parent_key=None):
    if parent_key in GRIDFS_FILE_KEYS:
        oid = object_id_from_gridfs_value(value)
        if oid is not None:
            yield oid
        return

    if isinstance(value, dict):
        for key, child in value.items():
            yield from iter_gridfs_file_ids(child, key)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from iter_gridfs_file_ids(child, parent_key)


def delete_gridfs_payload_for_project(delete_file, project, protected_file_ids=None):
    """
    Delete every GridFS file this project references, except the protected ones.

    ``delete_file`` is a callable taking one file id, not a GridFS handle.  The
    payload deleted here includes the project tarfile, which on this site runs
    to gigabytes, and ``gridfs.GridFS.delete()`` removes a file's chunks in a
    single ``delete_many`` that exceeds the driver's socket timeout at that
    size.  Callers pass utils.delete_gridfs_file(), which batches; the parameter
    is a callable so the batching cannot be bypassed by reaching for the handle.

    A file that will not delete is logged rather than raised: the caller is
    partway through promoting a version, and abandoning that leaves the chain
    inconsistent, which is worse than leaking the bytes.  The log line is what
    makes those bytes findable afterwards -- silence is how the existing orphan
    population stopped being countable.

    Returns the number of files deleted.
    """
    return delete_payload_within_deadline(
        delete_file, project, protected_file_ids).deleted


PayloadDeletion = collections.namedtuple('PayloadDeletion', 'deleted remaining')
"""What one payload pass achieved: files removed, and file ids not reached."""


def delete_payload_within_deadline(delete_file, project, protected_file_ids=None,
                                   deadline=None, now=time.monotonic):
    """``delete_gridfs_payload_for_project`` with a stopping time.

    A project's payload can take longer to delete than the web request is
    allowed to live.  Measured on prod 2026-09-01 over three deletes, a project
    costs about ``96.4 * GiB + 0.035 * files`` seconds; gunicorn kills a sync
    worker at 900 s.  The four largest soft-deleted projects that day were
    predicted at 1,590-2,189 s, so no browser request could finish one.

    Stopping partway is safe here because the deletion is ordered to be
    resumable: ``delete_gridfs_file`` removes the ``fs.files`` row before its
    chunks, and the caller removes the project document only after the payload
    is gone.  A pass that stops early therefore leaves a project that still
    names every file it has left, which is what makes a second pass a
    continuation rather than a fresh guess.  Nothing is stranded, because
    nothing that would name the remaining files has been removed.

    Deleting a *version* inverts that order -- the tombstone is written first,
    and it cannot name the files because a tombstone is built fresh without the
    payload keys.  That path keeps the same property by a different means: the
    ids it has not reached are recorded on the tombstone under
    ``PENDING_PAYLOAD_KEY``.  See ``version_purge``.

    *deadline* is a ``now()`` reading, not a duration.  It is checked before
    each file and never before the first, so a pass always makes progress and
    an already-expired deadline cannot turn the work into a no-op.

    Returns ``PayloadDeletion(deleted, remaining)`` where *remaining* holds the
    ids this pass did not reach.  An empty *remaining* means the payload is
    fully gone; a failed delete is logged, counted as reached, and left out of
    it, because retrying it is not what unblocks the project.
    """
    protected_file_ids = {str(file_id) for file_id in (protected_file_ids or set())}
    wanted = (file_id for file_id in iter_gridfs_file_ids(project)
              if str(file_id) not in protected_file_ids)
    return delete_payload_file_ids(delete_file, wanted, deadline=deadline,
                                   now=now, owner=project.get('_id'))


def delete_payload_file_ids(delete_file, file_ids, deadline=None,
                            now=time.monotonic, owner='?'):
    """Delete an explicit list of GridFS ids, stopping at *deadline*.

    The loop both payload deleters run.  One starts from a project document and
    works out which ids that means; the other starts from the ids a tombstone
    still carries, because by then the document no longer names them.  They
    share this so that *how* a payload is removed has one definition -- the
    same rule the rest of this module is written to, and the one that a second
    copy of the key list broke by falling eight spellings behind.

    *owner* only names the thing being emptied in the log lines.
    """
    deleted = 0
    seen = set()
    remaining = []
    for file_id in file_ids:
        if file_id in seen:
            continue
        seen.add(file_id)
        if deadline is not None and deleted and now() >= deadline:
            remaining.append(file_id)
            continue
        try:
            delete_file(file_id)
            deleted += 1
        except Exception as delete_error:
            logging.warning(
                f"GridFS delete failed for {file_id} of project "
                f"{owner}: {type(delete_error).__name__}: "
                f"{delete_error}. The file is now unreachable and will not be "
                f"collected by any other path."
            )
    if remaining:
        logging.warning(
            f"Payload delete for project {owner} stopped at its "
            f"deadline with {deleted} file(s) removed and {len(remaining)} "
            f"left; the ids it did not reach were kept so the rest can be "
            f"deleted by running it again."
        )
    return PayloadDeletion(deleted, remaining)


def discard_unrecorded_gridfs_files(delete_file, file_ids):
    """
    Delete GridFS files that were written but will never be referenced.

    Ingestion writes each artifact to GridFS *before* the project document that
    names it is updated.  If anything fails in between, those bytes become
    unreachable: no document points at them, so no deletion path will ever
    collect them, and they are billed forever.  That window is where the
    existing orphan population came from.

    Callers accumulate ids as they write them and pass them here on failure.
    Best-effort by design — a file that cannot be deleted must not mask the
    original error, which is the thing the operator actually needs to see.

    ``delete_file`` is a callable taking one file id, not a GridFS handle, for
    the same reason as delete_gridfs_payload_for_project(): a stranded upload
    can be a whole directory tarball, large enough that an unbatched delete
    times out.

    Returns the number of files actually deleted.
    """
    deleted = 0
    for file_id in (file_ids or ()):
        if file_id is None or not isinstance(file_id, ObjectId):
            continue
        try:
            delete_file(file_id)
            deleted += 1
        except Exception as delete_error:
            logging.warning(
                f"GridFS delete failed discarding unrecorded file {file_id}: "
                f"{type(delete_error).__name__}: {delete_error}"
            )
    return deleted


#: Written on a tombstone whose payload has not finished being removed: the
#: GridFS ids this deletion has still to delete.  A tombstone is built fresh
#: and does not carry the payload keys, so once it is written nothing else
#: knows which files belonged to that version -- this field is what makes an
#: interrupted purge resumable instead of a matter for a global orphan sweep.
PENDING_PAYLOAD_KEY = 'pending_payload_file_ids'

#: When a worker last took responsibility for the pending purge above.  Every
#: gunicorn worker checks for interrupted purges when it starts, so the claim
#: is what stops eight of them running the same one.  A claim older than
#: PURGE_CLAIM_STALE_SECONDS is treated as abandoned.
PURGE_CLAIM_KEY = 'payload_purge_claimed_at'

PURGE_CLAIM_STALE_SECONDS = 3600


def build_deleted_version_tombstone(old_project, latest_project, deleter,
                                    delete_date, is_latest=None,
                                    pending_payload_ids=None):
    """The one tombstone-creation routine.  Every deletion path calls it.

    That is invariant I18, and it is stated as an invariant because the fifth
    path -- deleting a project's only version -- used to spell the marker by
    hand and produced a different document: no GridFS purge, no
    ``payload_purged``, no ``redirect_to_project``, and a log line reading
    "project fully removed" over a document that was still resolvable with its
    whole payload still billed.

    ``latest_project`` is None for the terminal deletions (spec transitions T7
    and T8), where every other version is already a tombstone.  There is then
    nowhere to redirect to and no surviving version to inherit membership and
    visibility from, so those come from the deleted document itself and
    ``redirect_to_project`` is absent.  ``classify()`` does not require it --
    a tombstone with no redirect resolves to the empty-project shell rather
    than forwarding, which is the difference between a project whose versions
    are all deleted and a project that is gone.

    ``is_latest`` overrides the flag carried over from *old_project*: the
    caller has a chain in front of it and this function does not.  The pointer
    fields are carried over precisely because this is a ``replace_one`` --
    dropping them is how the two tombstones on production ended up in chains of
    their own, invisible to every pointer read.
    """
    # Imported here, not at module scope: utils imports this module, so the
    # dependency only runs in one direction.  Copying the four-line normalizer
    # in here instead would be one more place the visibility encoding is
    # decided, which is the failure this whole file is written against.
    from .utils import normalize_visibility_field
    from .lineage import pointer_fields

    inherit_from = latest_project if latest_project is not None else old_project

    tombstone = {
        '_id': old_project['_id'],
        'project_name': old_project.get('project_name', inherit_from.get('project_name')),
        'alias_name': old_project.get('alias_name'),
        'date': old_project.get('date'),
        # The flags that make classify() call this a TOMBSTONE, written from
        # the same table that reads them back.
        **status_flags(TOMBSTONE),
        'delete_user': deleter,
        'delete_date': delete_date,
        'private': normalize_visibility_field(
            inherit_from.get('private', old_project.get('private', 'private'))),
        'project_members': inherit_from.get(
            'project_members', old_project.get('project_members', [])),
        **pointer_fields(old_project, is_latest=is_latest),
    }
    if latest_project is not None:
        tombstone['redirect_to_project'] = str(latest_project['_id'])
    for field in VERSION_HISTORY_FIELDS:
        tombstone[field] = old_project.get(field, 'NA')

    # The downloads this version served happened, and deleting the version does
    # not unhappen them. Because a tombstone is written with replace_one, a
    # counter left off this dict is destroyed rather than merely hidden -- and
    # a project's displayed history would shrink every time an old version was
    # tidied away. Measured on prod 2026-08-29: exactly one of the tombstones
    # there still carried a nonzero counter, so every earlier deletion did
    # throw its share away. Only the dated counters are carried: `views` and
    # `downloads` are cumulative and already copied onto the promoted version,
    # so keeping them here as well would double-count. See download_totals.
    for field in DATED_COUNTERS:
        if old_project.get(field):
            tombstone[field] = old_project[field]

    # The payload is removed after this document is written, not before, so the
    # tombstone has to carry the ids until they are gone. See PENDING_PAYLOAD_KEY.
    if pending_payload_ids:
        tombstone[PENDING_PAYLOAD_KEY] = list(pending_payload_ids)
    return tombstone


def retarget_deleted_version_tombstones(collection, old_latest_id, new_latest_id):
    """
    Point deleted-version tombstones at the newest surviving project version.

    Tombstones are lightweight documents retained so old deleted-version URLs can
    redirect somewhere useful. When a later edit creates a new current version,
    tombstones that previously redirected to the old current version need their
    redirect target advanced to the new current version.
    """
    result = collection.update_many(
        combine(STATUS_QUERIES[TOMBSTONE],
                redirect_to_project=str(old_latest_id)),
        {'$set': {'redirect_to_project': str(new_latest_id)}},
    )
    return getattr(result, 'modified_count', 0)
