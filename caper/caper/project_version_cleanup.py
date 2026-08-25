from bson import ObjectId


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


def delete_gridfs_payload_for_project(fs_handle, project, protected_file_ids=None):
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
            fs_handle.delete(file_id)
            deleted += 1
        except Exception:
            pass
    return deleted


def discard_unrecorded_gridfs_files(fs_handle, file_ids):
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

    Returns the number of files actually deleted.
    """
    deleted = 0
    for file_id in (file_ids or ()):
        if file_id is None or not isinstance(file_id, ObjectId):
            continue
        try:
            fs_handle.delete(file_id)
            deleted += 1
        except Exception:
            pass
    return deleted


def build_deleted_version_tombstone(old_project, latest_project, deleter, delete_date):
    tombstone = {
        '_id': old_project['_id'],
        'project_name': old_project.get('project_name', latest_project.get('project_name')),
        'alias_name': old_project.get('alias_name'),
        'date': old_project.get('date'),
        'current': False,
        'delete': True,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(latest_project['_id']),
        'delete_user': deleter,
        'delete_date': delete_date,
        'private': latest_project.get('private', old_project.get('private', 'private')),
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
        {
            'version_deleted_from_history': True,
            'payload_purged': True,
            'redirect_to_project': str(old_latest_id),
        },
        {'$set': {'redirect_to_project': str(new_latest_id)}},
    )
    return getattr(result, 'modified_count', 0)
