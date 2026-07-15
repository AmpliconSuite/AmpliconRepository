from bson import ObjectId


GRIDFS_FILE_KEYS = {
    'tarfile',
    'AA PNG file',
    'AA PDF file',
    'Feature BED file',
    'CNV BED file',
    'AA directory',
    'cnvkit directory',
    'Sample metadata JSON',
    'AA graph file',
    'AA cycles file',
    'AA_PNG_file',
    'AA_PDF_file',
    'Feature_BED_file',
    'CNV_BED_file',
    'AA_directory',
    'cnvkit_directory',
    'Sample_metadata_JSON',
    'AA_graph_file',
    'AA_cycles_file',
}

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
