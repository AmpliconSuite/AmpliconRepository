import logging

from bson import ObjectId

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
    deleted = 0
    seen = set()
    protected_file_ids = {str(file_id) for file_id in (protected_file_ids or set())}
    for file_id in iter_gridfs_file_ids(project):
        if file_id in seen:
            continue
        seen.add(file_id)
        if str(file_id) in protected_file_ids:
            continue
        try:
            delete_file(file_id)
            deleted += 1
        except Exception as delete_error:
            logging.warning(
                f"GridFS delete failed for {file_id} of project "
                f"{project.get('_id')}: {type(delete_error).__name__}: "
                f"{delete_error}. The file is now unreachable and will not be "
                f"collected by any other path."
            )
    return deleted


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


def build_deleted_version_tombstone(old_project, latest_project, deleter, delete_date):
    # Imported here, not at module scope: utils imports this module, so the
    # dependency only runs in one direction.  Copying the four-line normalizer
    # in here instead would be one more place the visibility encoding is
    # decided, which is the failure this whole file is written against.
    from .utils import normalize_visibility_field

    tombstone = {
        '_id': old_project['_id'],
        'project_name': old_project.get('project_name', latest_project.get('project_name')),
        'alias_name': old_project.get('alias_name'),
        'date': old_project.get('date'),
        # The flags that make classify() call this a TOMBSTONE, written from
        # the same table that reads them back.
        **status_flags(TOMBSTONE),
        'redirect_to_project': str(latest_project['_id']),
        'delete_user': deleter,
        'delete_date': delete_date,
        'private': normalize_visibility_field(
            latest_project.get('private', old_project.get('private', 'private'))),
        'project_members': latest_project.get('project_members', old_project.get('project_members', [])),
    }
    for field in VERSION_HISTORY_FIELDS:
        tombstone[field] = old_project.get(field, 'NA')
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
