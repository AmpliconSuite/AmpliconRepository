#!/usr/bin/env python
"""
Local MongoDB cleanup utilities for CAPER.

Default behavior is a dry run. Pass --execute to delete data.

Examples:
    # Report unreferenced GridFS files for the configured DB_NAME.
    source caper/config.sh && python purge-local-db.py --smart-gridfs

    # Delete unreferenced GridFS files for caper-dev.
    source caper/config.sh && python purge-local-db.py --smart-gridfs --execute

    # Legacy-style full project/GridFS purge, but explicit.
    python purge-local-db.py --all-project-data --db caper-dev --execute
"""

import argparse
from collections import defaultdict
import os
import shutil

import gridfs
from bson import ObjectId
from pymongo import MongoClient

from cleanup_orphaned_projects import collect_protected_ids


GRIDFS_COLLECTIONS = ('fs.files', 'fs.chunks')
APP_GRIDFS_KEYS = {
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


def get_db_handle(db_name, host):
    client = MongoClient(host)
    db_handle = client[db_name]
    return db_handle, client


def get_collection_handle(db_handle, collection_name):
    return db_handle[collection_name]


def object_id_string(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return str(ObjectId(value))
    return None


def collect_object_id_strings(value):
    """Recursively collect ObjectId-like values from a document tree."""
    found = set()
    oid = object_id_string(value)
    if oid:
        found.add(oid)
    elif isinstance(value, dict):
        for child in value.values():
            found.update(collect_object_id_strings(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.update(collect_object_id_strings(child))
    return found


def collect_app_gridfs_ids(value, parent_key=None):
    """Collect ObjectIds only from fields known to hold GridFS file ids."""
    found = set()
    if parent_key in APP_GRIDFS_KEYS:
        oid = object_id_string(value)
        if oid:
            found.add(oid)
            return found

    if isinstance(value, dict):
        for key, child in value.items():
            found.update(collect_app_gridfs_ids(child, parent_key=key))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.update(collect_app_gridfs_ids(child, parent_key=parent_key))
    return found


def collect_gridfs_references_by_path(value, path=''):
    found = []
    oid = object_id_string(value)
    if oid:
        found.append((oid, path))
        return found

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.extend(collect_gridfs_references_by_path(child, child_path))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.extend(collect_gridfs_references_by_path(child, f"{path}[]"))
    return found


def reference_bucket(path):
    key = path.rsplit('.', 1)[-1]
    if key == 'tarfile':
        return 'project tarfiles'
    if key in APP_GRIDFS_KEYS:
        return f'feature files: {key}'
    if key == '_id':
        return 'project document ids'
    if key in {'linkid', 'previous_versions[]'} or path.endswith('.linkid'):
        return 'version references'
    return f'other/stale references: {key}'


def project_matches_scope(project, protected_ids, scope):
    project_id = str(project['_id'])
    if scope == 'all':
        return True
    if scope == 'reachable':
        return project_id in protected_ids
    if scope == 'active':
        return project.get('current') is True and project.get('delete') is False
    if scope == 'current':
        return project.get('current') is True
    if scope == 'not-deleted':
        return project.get('delete') is False
    raise ValueError(f"Unknown reference scope: {scope}")


def iter_projects_for_scope(projects_collection, scope):
    protected_ids = collect_protected_ids(projects_collection)
    for project in projects_collection.find({}):
        if project_matches_scope(project, protected_ids, scope):
            yield project


def collect_project_referenced_ids(projects_collection, scope='reachable', strategy='recursive'):
    collector = collect_app_gridfs_ids if strategy == 'app-fields' else collect_object_id_strings
    referenced = set()
    for project in iter_projects_for_scope(projects_collection, scope):
        referenced.update(collector(project))
    return referenced


def find_unreferenced_gridfs_files(db_handle, referenced_ids):
    unreferenced = []
    for grid_file in db_handle['fs.files'].find({}, {'_id': 1, 'length': 1, 'filename': 1}):
        file_id = str(grid_file['_id'])
        if file_id not in referenced_ids:
            unreferenced.append({
                '_id': grid_file['_id'],
                'length': grid_file.get('length', 0),
                'filename': grid_file.get('filename', ''),
            })
    return unreferenced


def summarize_files(files):
    total_bytes = sum(f.get('length', 0) for f in files)
    return len(files), total_bytes


def smart_purge_gridfs(db_handle, execute=False, limit=None, scope='reachable', strategy='recursive'):
    projects = get_collection_handle(db_handle, 'projects')
    referenced_ids = collect_project_referenced_ids(projects, scope=scope, strategy=strategy)
    unreferenced = find_unreferenced_gridfs_files(db_handle, referenced_ids)
    if limit is not None:
        unreferenced = unreferenced[:limit]

    count, total_bytes = summarize_files(unreferenced)
    print(f"Reference scope: {scope}")
    print(f"Reference strategy: {strategy}")
    print(f"Project-referenced ObjectIds: {len(referenced_ids)}")
    print(f"Unreferenced GridFS files: {count}")
    print(f"Unreferenced GridFS bytes: {total_bytes} ({total_bytes / 1024 / 1024 / 1024:.2f} GiB)")

    if not unreferenced:
        return count, total_bytes

    print("Largest unreferenced GridFS files:")
    for grid_file in sorted(unreferenced, key=lambda f: f.get('length', 0), reverse=True)[:20]:
        mib = grid_file.get('length', 0) / 1024 / 1024
        print(f"  {mib:9.1f} MiB  {grid_file['_id']}  {grid_file.get('filename', '')}")

    if not execute:
        print("DRY RUN: pass --execute to delete these GridFS files.")
        return count, total_bytes

    fs_handle = gridfs.GridFS(db_handle)
    deleted = 0
    for grid_file in unreferenced:
        fs_handle.delete(grid_file['_id'])
        deleted += 1
        if deleted % 1000 == 0:
            print(f"Deleted {deleted}/{count} GridFS files...")

    print(f"Deleted {deleted} unreferenced GridFS files.")
    return count, total_bytes


def gridfs_file_lengths(db_handle):
    return {
        str(f['_id']): f.get('length', 0)
        for f in db_handle['fs.files'].find({}, {'_id': 1, 'length': 1})
    }


def project_reference_usage(project, file_lengths, strategy='recursive'):
    collector = collect_app_gridfs_ids if strategy == 'app-fields' else collect_object_id_strings
    ids = collector(project)
    total = sum(file_lengths.get(file_id, 0) for file_id in ids)
    return ids, total


def report_gridfs_usage_by_project(db_handle, scope='reachable', strategy='recursive', limit=50):
    projects = get_collection_handle(db_handle, 'projects')
    lengths = gridfs_file_lengths(db_handle)
    rows = []
    referenced = set()
    for project in iter_projects_for_scope(projects, scope):
        ids, total = project_reference_usage(project, lengths, strategy=strategy)
        referenced.update(ids)
        rows.append((
            total,
            len(ids),
            str(project['_id']),
            project.get('project_name', '<unnamed>'),
            project.get('current', 'NOT SET'),
            project.get('delete', 'NOT SET'),
        ))

    print(f"Reference scope: {scope}")
    print(f"Reference strategy: {strategy}")
    print(f"Projects reported: {len(rows)}")
    print(f"Total referenced GridFS bytes in scope: {sum(r[0] for r in rows)}")
    print("")
    print("GiB\tfiles\tcurrent\tdelete\tproject_id\tproject_name")
    for total, count, project_id, name, current, delete in sorted(rows, reverse=True)[:limit]:
        print(f"{total / 1024 / 1024 / 1024:.2f}\t{count}\t{current}\t{delete}\t{project_id}\t{name}")

    referenced_existing = referenced & set(lengths)
    unreferenced_bytes = sum(
        length
        for file_id, length in lengths.items()
        if file_id not in referenced_existing
    )
    print("")
    print(f"GridFS files total: {len(lengths)}")
    print(f"GridFS files referenced in scope: {len(referenced_existing)}")
    print(f"GridFS bytes unreferenced in scope: {unreferenced_bytes} ({unreferenced_bytes / 1024 / 1024 / 1024:.2f} GiB)")


def report_gridfs_usage_by_reference_type(db_handle, scope='reachable', limit=50):
    projects = get_collection_handle(db_handle, 'projects')
    lengths = gridfs_file_lengths(db_handle)
    buckets = {}
    owners = {}
    for project in iter_projects_for_scope(projects, scope):
        project_id = str(project['_id'])
        project_name = project.get('project_name', '<unnamed>')
        for file_id, path in collect_gridfs_references_by_path(project):
            if file_id not in lengths:
                continue
            bucket = reference_bucket(path)
            previous = owners.get(file_id)
            if previous is not None and previous[0] == bucket:
                continue
            owners[file_id] = (bucket, project_id, project_name, path)

    bucket_totals = defaultdict(lambda: {'bytes': 0, 'files': 0})
    project_tarfiles = []
    for file_id, (bucket, project_id, project_name, path) in owners.items():
        length = lengths[file_id]
        bucket_totals[bucket]['bytes'] += length
        bucket_totals[bucket]['files'] += 1
        if bucket == 'project tarfiles':
            project_tarfiles.append((length, file_id, project_id, project_name))

    referenced_ids = set(owners)
    unreferenced = [
        (length, file_id)
        for file_id, length in lengths.items()
        if file_id not in referenced_ids
    ]
    unreferenced_bytes = sum(length for length, _ in unreferenced)
    bucket_totals['unreferenced GridFS files']['bytes'] = unreferenced_bytes
    bucket_totals['unreferenced GridFS files']['files'] = len(unreferenced)

    print(f"Reference scope: {scope}")
    print("")
    print("GiB\tfiles\treference_type")
    for bucket, data in sorted(bucket_totals.items(), key=lambda item: item[1]['bytes'], reverse=True):
        print(f"{data['bytes'] / 1024 / 1024 / 1024:.2f}\t{data['files']}\t{bucket}")

    print("")
    print("Largest project tarfiles:")
    print("GiB\tfile_id\tproject_id\tproject_name")
    for length, file_id, project_id, project_name in sorted(project_tarfiles, reverse=True)[:limit]:
        print(f"{length / 1024 / 1024 / 1024:.2f}\t{file_id}\t{project_id}\t{project_name}")

    print("")
    print("Largest unreferenced GridFS files:")
    print("GiB\tfile_id")
    for length, file_id in sorted(unreferenced, reverse=True)[:limit]:
        print(f"{length / 1024 / 1024 / 1024:.2f}\t{file_id}")


def report_tarfile_references(db_handle, limit=50):
    projects = get_collection_handle(db_handle, 'projects')
    protected_ids = collect_protected_ids(projects)
    fs_files = {
        str(f['_id']): f
        for f in db_handle['fs.files'].find(
            {},
            {'_id': 1, 'length': 1, 'filename': 1, 'uploadDate': 1},
        )
    }
    existing = []
    missing = []
    without_tarfile = []

    for project in projects.find({}, {'_id': 1, 'project_name': 1, 'current': 1, 'delete': 1, 'tarfile': 1}):
        project_id = str(project['_id'])
        scope = 'reachable' if project_id in protected_ids else 'orphan'
        tarfile_id = project.get('tarfile')
        if not tarfile_id:
            without_tarfile.append((scope, project_id, project.get('project_name', '<unnamed>')))
            continue

        tarfile_id = str(tarfile_id)
        grid_file = fs_files.get(tarfile_id)
        if grid_file:
            existing.append((
                scope,
                grid_file.get('length', 0),
                tarfile_id,
                project_id,
                project.get('project_name', '<unnamed>'),
                project.get('current', 'NOT SET'),
                project.get('delete', 'NOT SET'),
                grid_file.get('filename', ''),
            ))
        else:
            missing.append((
                scope,
                tarfile_id,
                project_id,
                project.get('project_name', '<unnamed>'),
                project.get('current', 'NOT SET'),
                project.get('delete', 'NOT SET'),
            ))

    totals = defaultdict(lambda: {'files': 0, 'bytes': 0})
    for scope, length, *_ in existing:
        totals[scope]['files'] += 1
        totals[scope]['bytes'] += length

    print("Existing referenced tarfiles by scope:")
    for scope, data in sorted(totals.items()):
        print(f"  {scope}: {data['files']} tarfiles, {data['bytes'] / 1024 / 1024 / 1024:.2f} GiB")
    print(f"Missing tarfile references: {len(missing)}")
    print(f"Projects without tarfile field/value: {len(without_tarfile)}")

    if missing:
        print("")
        print("Missing tarfile references:")
        print("scope\ttarfile_id\tproject_id\tcurrent\tdelete\tproject_name")
        for scope, tarfile_id, project_id, name, current, delete in missing[:limit]:
            print(f"{scope}\t{tarfile_id}\t{project_id}\t{current}\t{delete}\t{name}")

    print("")
    print("Largest existing reachable tarfiles:")
    print("GiB\ttarfile_id\tproject_id\tcurrent\tdelete\tproject_name")
    reachable = [row for row in existing if row[0] == 'reachable']
    for scope, length, tarfile_id, project_id, name, current, delete, filename in sorted(reachable, reverse=True)[:limit]:
        print(f"{length / 1024 / 1024 / 1024:.2f}\t{tarfile_id}\t{project_id}\t{current}\t{delete}\t{name}")


def purge_project_data(db_handle, execute=False):
    collections = ['projects', *GRIDFS_COLLECTIONS]
    if not execute:
        print("DRY RUN: would drop collections: " + ', '.join(collections))
        return

    for collection_name in collections:
        get_collection_handle(db_handle, collection_name).drop()
        print(f"Dropped {collection_name}")


def clear_tmp(folder, execute=False):
    if not os.path.isdir(folder):
        print(f"tmp folder not found: {folder}")
        return

    entries = [os.path.join(folder, filename) for filename in os.listdir(folder)]
    if not execute:
        print(f"DRY RUN: would remove {len(entries)} entries from {folder}")
        return

    for file_path in entries:
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (file_path, e))


def parse_args():
    parser = argparse.ArgumentParser(description='Purge local CAPER MongoDB data safely.')
    parser.add_argument(
        '--uri',
        default=os.getenv('DB_URI_SECRET', 'mongodb://localhost:27017'),
        help='MongoDB URI. Defaults to DB_URI_SECRET or mongodb://localhost:27017.',
    )
    parser.add_argument(
        '--db',
        action='append',
        default=[],
        help='Database name. May be repeated. Defaults to DB_NAME or caper-dev.',
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually delete data. Without this flag all actions are dry runs.',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Limit smart GridFS deletion/reporting to N items where supported.',
    )
    parser.add_argument(
        '--reference-scope',
        choices=['reachable', 'active', 'current', 'not-deleted', 'all'],
        default='reachable',
        help='Which projects protect GridFS references. Defaults to reachable app-visible projects.',
    )
    parser.add_argument(
        '--reference-strategy',
        choices=['recursive', 'app-fields'],
        default='recursive',
        help='How to collect protected GridFS ids. recursive is conservative; app-fields is more aggressive.',
    )
    parser.add_argument(
        '--tmp-folder',
        default='tmp',
        help='tmp folder to clear when using --all-project-data.',
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        '--smart-gridfs',
        action='store_true',
        help='Delete GridFS files not referenced anywhere in projects documents.',
    )
    mode.add_argument(
        '--all-project-data',
        action='store_true',
        help='Drop projects, fs.files, and fs.chunks collections, then clear tmp.',
    )
    mode.add_argument(
        '--gridfs-usage-by-project',
        action='store_true',
        help='Report GridFS usage grouped by project under the selected scope/strategy.',
    )
    mode.add_argument(
        '--gridfs-usage-by-type',
        action='store_true',
        help='Report GridFS usage grouped by project reference type, including tarfiles.',
    )
    mode.add_argument(
        '--tarfile-report',
        action='store_true',
        help='Report project tarfile references and whether their GridFS files exist.',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db_names = args.db or [os.getenv('DB_NAME', 'caper-dev')]

    for db_name in db_names:
        print(f"{'Purging' if args.execute else 'Inspecting'} {db_name}")
        db_handle, mongo_client = get_db_handle(db_name, args.uri)
        try:
            if args.smart_gridfs:
                smart_purge_gridfs(
                    db_handle,
                    execute=args.execute,
                    limit=args.limit,
                    scope=args.reference_scope,
                    strategy=args.reference_strategy,
                )
            elif args.all_project_data:
                purge_project_data(db_handle, execute=args.execute)
                clear_tmp(args.tmp_folder, execute=args.execute)
            elif args.gridfs_usage_by_project:
                report_gridfs_usage_by_project(
                    db_handle,
                    scope=args.reference_scope,
                    strategy=args.reference_strategy,
                    limit=args.limit or 50,
                )
            elif args.gridfs_usage_by_type:
                report_gridfs_usage_by_reference_type(
                    db_handle,
                    scope=args.reference_scope,
                    limit=args.limit or 50,
                )
            elif args.tarfile_report:
                report_tarfile_references(db_handle, limit=args.limit or 50)
        finally:
            mongo_client.close()


if __name__ == '__main__':
    main()
