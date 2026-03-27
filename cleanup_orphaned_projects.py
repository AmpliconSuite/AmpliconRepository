#!/usr/bin/env python
"""
cleanup_orphaned_projects.py

Safely cleans up orphaned projects that are no longer reachable by the
application through any code path.

Protected projects (NEVER deleted by this script):
  1. Active projects        – current=True  AND delete=False
  2. Soft-deleted projects  – delete=True   AND current=True
     (visible on the admin "permanently delete" page; can be un-deleted)
  3. Previous versions of any protected project – referenced in
     previous_versions[].linkid of a project from group 1 or 2
  4. Any project with delete=False – reachable via get_one_project()
     by direct URL regardless of the 'current' flag

Everything else in the projects collection is considered orphaned and
is cleaned up from:
  - MongoDB (project document)
  - GridFS  (tarfile + per-sample feature files)
  - Local disk (tmp/<project_id>/ directory)
  - S3 (if configured)

After cleaning orphaned project documents the script also scans the
tmp/ directory for UUID-like folders that have no corresponding project
in the database and removes them (and their S3 counterparts).

Usage:
    source caper/config.sh && cd caper && python ../cleanup_orphaned_projects.py --dry-run
    source caper/config.sh && cd caper && python ../cleanup_orphaned_projects.py

Requirements:
    - Environment variables set via  source caper/config.sh
    - Run from the caper/caper/ directory (where manage.py lives)
    - pymongo, gridfs, bson installed
    - boto3 installed for S3 cleanup (optional – skipped if absent)
"""

import os
import re
import sys
import shutil
import logging
import argparse

from bson import ObjectId
from pymongo import MongoClient
import gridfs

# Optional: boto3 for S3 cleanup
try:
    import boto3
    import botocore.exceptions
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# MongoDB ObjectId: exactly 24 hex characters
OBJECTID_RE = re.compile(r'^[0-9a-fA-F]{24}$')
# Python uuid4().hex: exactly 32 hex characters
UUID_HEX_RE = re.compile(r'^[0-9a-fA-F]{32}$')


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def get_db_handle(db_name, host):
    """Connect to MongoDB and return (db_handle, client)."""
    client = MongoClient(host)
    db_handle = client[db_name]
    return db_handle, client


def is_uuid_like(name):
    """True when *name* looks like a MongoDB ObjectId or uuid4 hex string."""
    return bool(OBJECTID_RE.match(name) or UUID_HEX_RE.match(name))


def get_s3_client(aws_profile):
    """Create an S3 client or return None if unavailable."""
    if not HAS_BOTO3:
        logger.warning("boto3 is not installed – S3 cleanup will be skipped.")
        return None
    try:
        session = boto3.Session(profile_name=aws_profile)
        client = session.client('s3')
        client.list_buckets()  # quick connectivity check
        return client
    except Exception as e:
        logger.warning(f"Could not create S3 client (profile={aws_profile!r}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
# Build the set of protected project IDs
# ─────────────────────────────────────────────────────────────────────

def collect_protected_ids(collection):
    """
    Return a set of project _id strings that must NOT be deleted.

    A project is protected if it is reachable through any application
    code path:

      (a) delete=False  – findable by get_one_project() via direct URL
          regardless of the 'current' flag.
      (b) delete=True AND current=True  – shown on the admin
          "permanently delete" page; an admin can un-delete these.
      (c) Any project referenced in previous_versions[].linkid of a
          project that is itself protected by (a) or (b).
    """
    protected = set()

    # ── (a) Every non-deleted project ────────────────────────────────
    for doc in collection.find({'delete': False}, {'_id': 1, 'previous_versions': 1}):
        protected.add(str(doc['_id']))
        # also protect its previous versions  (c)
        for pv in doc.get('previous_versions', []):
            lid = pv.get('linkid')
            if lid:
                protected.add(str(lid))

    # ── (b) Soft-deleted projects on the admin page ──────────────────
    for doc in collection.find({'delete': True, 'current': True},
                               {'_id': 1, 'previous_versions': 1}):
        protected.add(str(doc['_id']))
        # also protect their previous versions  (c)
        for pv in doc.get('previous_versions', []):
            lid = pv.get('linkid')
            if lid:
                protected.add(str(lid))

    return protected


# ─────────────────────────────────────────────────────────────────────
# Deletion helpers
# ─────────────────────────────────────────────────────────────────────

def delete_s3_prefix(s3_client, bucket, prefix, dry_run=False):
    """Delete every S3 object under *prefix*.  Returns count deleted."""
    if s3_client is None:
        return 0

    deleted = 0
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys.append({'Key': obj['Key']})

        if not keys:
            logger.debug(f"  No S3 objects under: {prefix}")
            return 0

        if dry_run:
            logger.info(f"  [DRY RUN] Would delete {len(keys)} S3 object(s) under: {prefix}")
            for k in keys:
                logger.debug(f"    {k['Key']}")
            return len(keys)

        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            resp = s3_client.delete_objects(
                Bucket=bucket, Delete={'Objects': batch}
            )
            deleted += len(resp.get('Deleted', []))
            for err in resp.get('Errors', []):
                logger.error(f"  S3 delete error {err['Key']}: {err['Message']}")

        logger.info(f"  Deleted {deleted} S3 object(s) under: {prefix}")
    except Exception as e:
        logger.error(f"  Error cleaning S3 prefix {prefix}: {e}")
    return deleted


def delete_gridfs_files_for_project(fs_handle, project, dry_run=False):
    """
    Delete GridFS files owned by *project*:
      - the project tarfile
      - per-sample feature files (PNG, PDF, BED, graph, cycles, …)
    Returns total count of files deleted / that would be deleted.
    """
    count = 0

    # ── tarfile ──────────────────────────────────────────────────────
    tar_id = project.get('tarfile')
    if tar_id:
        try:
            if dry_run:
                logger.info(f"  [DRY RUN] Would delete GridFS tarfile: {tar_id}")
            else:
                fs_handle.delete(ObjectId(str(tar_id)))
                logger.debug(f"  Deleted GridFS tarfile: {tar_id}")
            count += 1
        except Exception as e:
            logger.debug(f"  Could not delete GridFS tarfile {tar_id}: {e}")

    # ── per-sample feature files ─────────────────────────────────────
    # Union of keys from admin_permanent_delete_project (views_admin.py)
    # and the newer deletion code path that iterates dicts.
    feature_keys = [
        'AA PNG file', 'AA PDF file', 'Feature BED file', 'CNV BED file',
        'AA directory', 'cnvkit directory',
        'Sample metadata JSON', 'AA graph file', 'AA cycles file',
    ]

    runs = project.get('runs', {})
    try:
        for sample_name, features in runs.items():
            if not isinstance(features, list):
                continue
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                for key in feature_keys:
                    fid = feature.get(key)
                    if fid and fid != 'Not Provided':
                        try:
                            if dry_run:
                                logger.debug(
                                    f"  [DRY RUN] Would delete GridFS: {fid} ({key})")
                            else:
                                fs_handle.delete(ObjectId(str(fid)))
                                logger.debug(f"  Deleted GridFS: {fid} ({key})")
                            count += 1
                        except Exception as e:
                            logger.debug(
                                f"  Could not delete GridFS {fid} ({key}): {e}")
    except Exception as e:
        logger.error(f"  Error walking runs for GridFS cleanup: {e}")

    return count


def delete_local_directory(name, tmp_dir, dry_run=False):
    """Remove tmp/<name> recursively.  Returns True if it existed."""
    target = os.path.join(tmp_dir, str(name))
    if not os.path.exists(target):
        logger.debug(f"  No local directory: {target}")
        return False
    if dry_run:
        logger.info(f"  [DRY RUN] Would delete directory: {target}")
    else:
        try:
            shutil.rmtree(target)
            logger.info(f"  Deleted directory: {target}")
        except Exception as e:
            logger.error(f"  Failed to delete {target}: {e}")
    return True


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Clean up orphaned projects from MongoDB, GridFS, '
                    'local disk, and S3.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show what would be deleted without changing anything.')
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Enable DEBUG-level logging.')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        logger.info("=" * 70)
        logger.info("DRY RUN MODE — no changes will be made")
        logger.info("=" * 70)

    # ─── Configuration from environment ──────────────────────────────
    db_name = os.getenv('DB_NAME', 'caper')
    db_uri = os.getenv('DB_URI_SECRET')
    if not db_uri:
        logger.error("DB_URI_SECRET is not set.  "
                      "Run:  source caper/config.sh")
        sys.exit(1)

    use_s3 = os.getenv('S3_FILE_DOWNLOADS') == 'TRUE'
    aws_profile = os.getenv('AWS_PROFILE_NAME', 'default')
    s3_bucket = 'amprepo-private'
    raw_bp = os.getenv('S3_DOWNLOADS_BUCKET_PATH', '')
    s3_bucket_path = (raw_bp.rstrip('/') + '/') if raw_bp else ''

    # tmp/ lives inside the Django project dir (caper/caper/tmp/).
    # This script sits at the repo root (caper/) so default to caper/tmp/.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_dir = os.path.join(script_dir, 'caper', 'tmp')
    if not os.path.isdir(tmp_dir):
        alt = os.path.join(script_dir, 'tmp')
        if os.path.isdir(alt):
            tmp_dir = alt
        else:
            logger.warning(f"tmp/ not found at {tmp_dir} or {alt}; "
                           "local cleanup may be incomplete.")

    logger.info(f"Database     : {db_name} @ {db_uri}")
    logger.info(f"S3 enabled   : {use_s3}")
    if use_s3:
        logger.info(f"S3 bucket    : {s3_bucket}")
        logger.info(f"S3 path pfx  : '{s3_bucket_path}'")
    logger.info(f"tmp directory: {tmp_dir}")

    # ─── Connect ─────────────────────────────────────────────────────
    db_handle, mongo_client = get_db_handle(db_name, db_uri)
    collection = db_handle['projects']
    fs = gridfs.GridFS(db_handle)

    s3_client = None
    if use_s3:
        s3_client = get_s3_client(aws_profile)
        if s3_client is None:
            logger.warning("S3 cleanup will be skipped.")

    # ═════════════════════════════════════════════════════════════════
    # PHASE 1 — Determine which projects are protected
    # ═════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("PHASE 1: Identifying protected and orphaned projects")
    logger.info("=" * 70)

    protected_ids = collect_protected_ids(collection)
    logger.info(f"  Protected projects (reachable by app): {len(protected_ids)}")

    all_projects = list(collection.find({}))
    all_ids = {str(p['_id']) for p in all_projects}
    logger.info(f"  Total projects in database           : {len(all_ids)}")

    orphaned_ids = all_ids - protected_ids
    logger.info(f"  Orphaned projects to clean up        : {len(orphaned_ids)}")

    # Show breakdown of protected projects
    active_count = collection.count_documents({'current': True, 'delete': False})
    soft_del_count = collection.count_documents({'delete': True, 'current': True})
    logger.info(f"  Breakdown of protected projects:")
    logger.info(f"    Active (current=True, delete=False)       : {active_count}")
    logger.info(f"    Soft-deleted (delete=True, current=True)  : {soft_del_count}")
    logger.info(f"    Previous versions / other reachable       : "
                f"{len(protected_ids) - active_count - soft_del_count}")

    orphaned_lookup = {str(p['_id']): p for p in all_projects
                       if str(p['_id']) in orphaned_ids}

    # ═════════════════════════════════════════════════════════════════
    # PHASE 2 — Clean up orphaned projects
    # ═════════════════════════════════════════════════════════════════
    total_gridfs = total_s3 = total_dirs = total_mongo = 0

    if orphaned_ids:
        logger.info("")
        logger.info("=" * 70)
        logger.info("PHASE 2: Cleaning up orphaned projects")
        logger.info("=" * 70)

        for idx, pid in enumerate(sorted(orphaned_ids), 1):
            project = orphaned_lookup[pid]
            name = project.get('project_name', '<unnamed>')
            cur = project.get('current', 'NOT SET')
            dlt = project.get('delete', 'NOT SET')

            logger.info("")
            logger.info(f"  [{idx}/{len(orphaned_ids)}] {name}")
            logger.info(f"    _id={pid}  current={cur}  delete={dlt}")

            # 2a. GridFS
            g = delete_gridfs_files_for_project(fs, project,
                                                dry_run=args.dry_run)
            total_gridfs += g
            if g:
                logger.info(f"    GridFS files "
                            f"{'to remove' if args.dry_run else 'removed'}: {g}")

            # 2b. Local directory
            if delete_local_directory(pid, tmp_dir, dry_run=args.dry_run):
                total_dirs += 1

            # 2c. S3
            if use_s3 and s3_client:
                pfx = f"{s3_bucket_path}{pid}/"
                total_s3 += delete_s3_prefix(s3_client, s3_bucket, pfx,
                                             dry_run=args.dry_run)

            # 2d. MongoDB document (last, so re-run can catch failures)
            if args.dry_run:
                logger.info("    [DRY RUN] Would delete MongoDB document")
            else:
                try:
                    collection.delete_one({'_id': ObjectId(pid)})
                    logger.info("    Deleted MongoDB document")
                except Exception as e:
                    logger.error(f"    Failed to delete MongoDB document: {e}")
            total_mongo += 1

        verb = "to remove" if args.dry_run else "removed"
        logger.info("")
        logger.info("-" * 70)
        logger.info("  Phase 2 summary:")
        logger.info(f"    MongoDB documents {verb}: {total_mongo}")
        logger.info(f"    GridFS files {verb}     : {total_gridfs}")
        logger.info(f"    Local directories {verb} : {total_dirs}")
        logger.info(f"    S3 objects {verb}        : {total_s3}")
    else:
        logger.info("  No orphaned projects — skipping Phase 2.")

    # ═════════════════════════════════════════════════════════════════
    # PHASE 3 — Orphan tmp/ directories with no project in the DB
    # ═════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("PHASE 3: Scanning tmp/ for orphan directories")
    logger.info("=" * 70)

    remaining = list(collection.find({}, {'_id': 1}))
    valid_ids = {str(p['_id']) for p in remaining}
    logger.info(f"  Valid project IDs in DB: {len(valid_ids)}")

    orphan_dirs = []
    uuid_dir_count = 0
    if os.path.isdir(tmp_dir):
        for entry in sorted(os.listdir(tmp_dir)):
            full = os.path.join(tmp_dir, entry)
            if os.path.isdir(full) and is_uuid_like(entry):
                uuid_dir_count += 1
                if entry not in valid_ids:
                    orphan_dirs.append(entry)

    logger.info(f"  UUID-like directories in tmp/ : {uuid_dir_count}")
    logger.info(f"  Orphan directories (no match): {len(orphan_dirs)}")

    orphan_dirs_deleted = orphan_s3_deleted = 0

    for entry in orphan_dirs:
        logger.info(f"  Orphan: {entry}")

        entry_path = os.path.join(tmp_dir, entry)
        if args.dry_run:
            logger.info(f"    [DRY RUN] Would delete: {entry_path}")
        else:
            try:
                shutil.rmtree(entry_path)
                logger.info(f"    Deleted: {entry_path}")
            except Exception as e:
                logger.error(f"    Failed to delete {entry_path}: {e}")
        orphan_dirs_deleted += 1

        if use_s3 and s3_client:
            pfx = f"{s3_bucket_path}{entry}/"
            orphan_s3_deleted += delete_s3_prefix(
                s3_client, s3_bucket, pfx, dry_run=args.dry_run)

    verb = "to remove" if args.dry_run else "removed"
    logger.info("")
    logger.info("-" * 70)
    logger.info("  Phase 3 summary:")
    logger.info(f"    Orphan directories {verb}: {orphan_dirs_deleted}")
    logger.info(f"    Orphan S3 objects {verb}  : {orphan_s3_deleted}")

    # ═════════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("CLEANUP COMPLETE" + ("  (DRY RUN)" if args.dry_run else ""))
    logger.info("=" * 70)
    logger.info(f"  Protected projects              : {len(protected_ids)}")
    logger.info(f"  Orphaned projects cleaned (Ph 2): {total_mongo}")
    logger.info(f"  Orphan tmp dirs cleaned  (Ph 3) : {orphan_dirs_deleted}")

    if args.dry_run:
        logger.info("")
        logger.info("  This was a DRY RUN — no changes were made.")
        logger.info("  Re-run without --dry-run to perform actual cleanup.")

    mongo_client.close()
    logger.info("")
    logger.info("Done.")


if __name__ == '__main__':
    main()

