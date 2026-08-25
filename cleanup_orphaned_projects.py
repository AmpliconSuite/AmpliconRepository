#!/usr/bin/env python
"""
cleanup_orphaned_projects.py

Safely cleans up orphaned projects that are no longer reachable by the
application through any code path.

Protected projects (NEVER deleted by this script).  Every rule below mirrors a
specific query in caper/utils.py; the line references are load-bearing, because
this list drifting out of step with the resolver is exactly how this script
became dangerous once already:

  1. delete=False – findable by get_one_project() via _id, alias_name or
     project_name, regardless of the 'current' flag   (utils.py:692/703/711)
  2. delete=True AND current=True – shown on the admin "permanently delete"
     page and can be un-deleted by an admin
  3. delete=True AND current=False – STILL RESOLVABLE BY URL.  get_one_project()
     falls back to exactly this query, by _id and again by project_name
     (utils.py:722 and utils.py:736), logging "had to use previous project
     ids!".  These are superseded project versions, and old links to them work
     today.
  4. delete=True with NO 'current' field – not resolvable right now only
     because {'current': False} does not match a missing field.  One routine
     backfill away from being reachable, and on prod these hold real payloads.
     Reported for human review, never deleted.
  5. Previous versions of anything protected above – referenced in
     previous_versions[].linkid
  6. Deleted-version redirect tombstones

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
    source caper/config.sh && python cleanup_orphaned_projects.py
    source caper/config.sh && python cleanup_orphaned_projects.py --execute

    Reporting is the default.  Nothing is deleted without --execute.

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

# The canonical GridFS key list lives with the application so the upload path
# and every delete path share one definition.  Importing it here — rather than
# keeping a second hand-written copy, which is what this script used to do —
# is the only way the two cannot drift.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))
from caper.project_version_cleanup import GRIDFS_FILE_KEYS, iter_gridfs_file_ids

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


def redact_uri(uri):
    """Strip credentials from a Mongo URI so it can be logged.

    The previous version logged DB_URI_SECRET verbatim, which put the database
    password into every log file and every terminal scrollback this script
    touched.
    """
    return re.sub(r'://[^@/]+@', '://<credentials-redacted>@', str(uri))


def is_uuid_like(name):
    """True when *name* looks like a MongoDB ObjectId or uuid4 hex string."""
    return bool(OBJECTID_RE.match(name) or UUID_HEX_RE.match(name))


def previous_version_linkid(previous_version):
    """Return the referenced project id from a previous_versions entry."""
    if isinstance(previous_version, dict):
        return previous_version.get('linkid')
    if previous_version:
        return previous_version
    return None


def get_s3_client(aws_profile, bucket=None):
    """Create an S3 client or return None if unavailable.

    The connectivity check is `head_bucket` on the bucket this script actually
    uses, not `list_buckets`.  `list_buckets` needs s3:ListAllMyBuckets, which
    the prod EC2 role does not have and does not need — so the old check failed
    on prod every run and silently skipped all S3 cleanup.
    """
    if not HAS_BOTO3:
        logger.warning("boto3 is not installed – S3 cleanup will be skipped.")
        return None
    try:
        # An instance role is the normal case on the servers; a named profile
        # only exists on developer machines.
        try:
            session = boto3.Session(profile_name=aws_profile)
        except Exception:
            session = boto3.Session()
        client = session.client('s3')
        if bucket:
            client.head_bucket(Bucket=bucket)
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

    A project is protected if get_one_project() can return it, or an admin can
    reach it.  Each rule below names the query in caper/utils.py it mirrors.

      (a) delete=False                  – utils.py:692 / :703 / :711
      (b) delete=True AND current=True  – admin "permanently delete" page
      (c) delete=True AND current=False – utils.py:722 / :736.  This rule was
          MISSING and its absence made the script delete live URLs.
      (d) previous_versions[].linkid of anything protected above
      (e) deleted-version redirect tombstones
    """
    protected = set()

    def protect(doc):
        protected.add(str(doc['_id']))
        for pv in doc.get('previous_versions', []):      # rule (d)
            lid = previous_version_linkid(pv)
            if lid:
                protected.add(str(lid))

    projection = {'_id': 1, 'previous_versions': 1}

    # ── (a) Anything not soft-deleted ────────────────────────────────
    for doc in collection.find({'delete': False}, projection):
        protect(doc)

    # ── (b) Soft-deleted, still on the admin page ────────────────────
    for doc in collection.find({'delete': True, 'current': True}, projection):
        protect(doc)

    # ── (c) Superseded versions — STILL RESOLVABLE BY URL ────────────
    # get_one_project() falls back to this exact query by _id and by
    # project_name.  Old links to superseded versions resolve today; deleting
    # these documents breaks them and destroys the payload behind them.
    for doc in collection.find({'delete': True, 'current': False}, projection):
        protect(doc)

    # ── (e) Deleted-version redirect tombstones ──────────────────────
    # These keep old UUIDs resolvable after their heavy GridFS payload has
    # been purged and should not be removed as orphan project documents.
    for doc in collection.find(
        {'version_deleted_from_history': True, 'payload_purged': True,
         'redirect_to_project': {'$exists': True}},
        {'_id': 1},
    ):
        protected.add(str(doc['_id']))

    return protected


def is_resolvable_by_url(collection, project_id, project_name=None):
    """
    True if get_one_project() could still return this document.

    Mirrors caper/utils.py:692, :703, :711, :722 and :736 as *queries against
    the live database*, one document at a time.  collect_protected_ids() works
    in bulk for speed; this exists to be asked again immediately before a
    delete, so the two have to disagree before anything reachable is lost.
    """
    try:
        oid = ObjectId(project_id)
    except Exception:
        oid = None

    queries = []
    if oid is not None:
        queries.append({'_id': oid, 'delete': False})                       # :692
        queries.append({'_id': oid, 'current': False, 'delete': True})      # :722
    if project_name:
        queries.append({'alias_name': project_name, 'delete': False})       # :703
        queries.append({'project_name': project_name, 'delete': False})     # :711
        queries.append({'project_name': project_name,
                        'current': False, 'delete': True})                  # :736

    for query in queries:
        try:
            hit = collection.find_one(query, {'_id': 1})
        except Exception:
            # A query that cannot be evaluated must not read as "safe to delete".
            return True
        # The name lookups can match a *different* document that happens to
        # share a name — that document being reachable says nothing about this
        # one. Only a hit on this exact _id means this document is reachable.
        if hit is not None and str(hit['_id']) == str(project_id):
            return True
    return False


def collect_needs_review_ids(collection):
    """
    Documents that are unreachable only because a field is *missing*.

    `{'current': False}` does not match a document with no 'current' field, so
    a soft-deleted document lacking that field escapes rule (c) above on a
    technicality.  Backfilling 'current' — an obvious hygiene action — would
    make every one of them resolvable again.

    On prod this class is 70 documents and most of them still hold a GridFS
    tarfile.  They are reported, never deleted: a human decides.
    """
    return {
        str(doc['_id'])
        for doc in collection.find(
            {'delete': True, 'current': {'$exists': False}}, {'_id': 1})
    }


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
    Delete every GridFS file the *project* document names — the tarfile, the
    per-sample feature files, and the directory-shaped payloads.

    The traversal is the application's own: `iter_gridfs_file_ids` walks the
    whole document rather than a list of key names maintained here.  The
    hand-written list this replaced was missing 8 canonical keys, including
    'Run metadata JSON' (120,726 live values on prod) and
    'Reconstruction directory' (33,758) — every cleanup silently left those
    files behind, which is one of the ways the orphan population grew.

    Returns the count deleted, or the count that would be deleted.
    """
    file_ids = []
    seen = set()
    for file_id in iter_gridfs_file_ids(project):
        if file_id in seen:
            continue
        seen.add(file_id)
        file_ids.append(file_id)

    if dry_run:
        for file_id in file_ids:
            logger.debug(f"  [DRY RUN] Would delete GridFS: {file_id}")
        return len(file_ids)

    deleted = 0
    for file_id in file_ids:
        try:
            fs_handle.delete(file_id)
            deleted += 1
        except Exception as e:
            logger.debug(f"  Could not delete GridFS {file_id}: {e}")
    return deleted


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
        '--execute', action='store_true',
        help='Actually delete. Without this the script only reports.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Deprecated; reporting is now the default and this is a no-op.')
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Enable DEBUG-level logging.')
    args = parser.parse_args()

    # Reporting is the default. This script once deleted 84 documents on prod
    # that the application could still resolve, so the destructive path is
    # opt-in rather than opt-out.
    args.dry_run = not args.execute

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        logger.info("=" * 70)
        logger.info("REPORT MODE — no changes will be made (pass --execute to delete)")
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

    # Never log the URI itself — it carries the password.
    logger.info(f"Database     : {db_name} @ {redact_uri(db_uri)}")
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
        s3_client = get_s3_client(aws_profile, s3_bucket)
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

    # Unreachable only because a field is absent — never deleted automatically.
    review_ids = collect_needs_review_ids(collection) - protected_ids
    if review_ids:
        logger.info("")
        logger.warning(
            f"  {len(review_ids)} document(s) are soft-deleted with NO 'current' "
            f"field.")
        logger.warning(
            "  They escape the URL-resolution rule on a technicality: "
            "{'current': False}")
        logger.warning(
            "  does not match a missing field. Backfilling 'current' would make "
            "them")
        logger.warning(
            "  resolvable again, and most still hold a GridFS payload. "
            "NOT DELETED —")
        logger.warning("  decide about these deliberately:")
        for rid in sorted(review_ids):
            doc = next((p for p in all_projects if str(p['_id']) == rid), {})
            logger.warning(
                f"    {rid}  {str(doc.get('project_name'))[:44]!r}  "
                f"tarfile={'yes' if doc.get('tarfile') else 'no'}")

    orphaned_ids = all_ids - protected_ids - review_ids
    logger.info("")
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

            # 2·0 Last-line guard, independent of the rules above.
            # collect_protected_ids() replicates queries that live in
            # caper/utils.py, and replication is how this script came to delete
            # live URLs. Re-ask the database directly, per document, so a future
            # drift between the two costs nothing.
            if is_resolvable_by_url(collection, pid, name):
                logger.error(
                    f"    REFUSING to delete {pid}: get_one_project() can still "
                    f"resolve it. The protection rules and the resolver have "
                    f"drifted apart — fix collect_protected_ids().")
                continue

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
