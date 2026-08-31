#!/usr/bin/env python
"""
Back up Django's SQLite database off-host, without its sessions.

`caper.sqlite3` is the only data on this site with no copy anywhere. The
DocumentDB cluster is snapshotted daily with 35-day retention, and that covers
every project, version chain, GridFS file and audit event. It does not cover
this file, because this file is not in DocumentDB. Measured on prod
2026-08-31: 364 user accounts, 339 email addresses, 148 OAuth links, 84 upload
records and the CMS pages. Losing it means every user re-registers and every
project loses its owner link.

The file is 184,610,816 bytes and 500,049 of its rows are expired sessions.
Sessions are the reason a naive nightly copy is 176 MiB of churn that never
compresses well and never hashes the same twice. Dropping them first turns the
backup into roughly a megabyte of things that genuinely change rarely, which is
what makes content-hash change-detection work and what makes the annual cost of
this round to nothing.

Replaces `caper/db_backup.sh`, which never produced a file on either host. Its
five independent defects, each sufficient on its own:

  1. `docker exec -it` under cron -- no TTY, dies with "the input device is not
     a TTY".
  2. `sqlite3` is not installed in the container, on dev or prod.
  3. `aws` is not on cron's PATH. Dev's host has no aws CLI at all.
  4. The weekly retention tier compared a numeric YYYYMMDD string against
     `*"Mon"*`, which can never match.
  5. The S3 path was hardcoded to `prod/` while dev's config sets
     AMPLICON_ENV='dev', so had it ever worked, dev would have been
     overwriting prod's backups.

This script fixes all five by construction: it uses `sqlite3.Connection.backup()`
which is in the standard library rather than the missing CLI binary, it runs
inside the container where both the interpreter and the AWS credentials already
are, it takes the environment from AMPLICON_ENV, and it has no retention tier at
all -- see below.

**There is deliberately no retention sweep.** At ~1 MB a night with
change-detection, keeping everything forever costs cents a year, and Glacier
Instant Retrieval bills a 90-day minimum per object anyway, so early deletion
saves nothing. A retention tier here would be code that can only cause harm.

**Glacier Instant Retrieval, not Flexible.** Near-identical price (~$0.004 vs
~$0.0036 per GB-month) but retrieval is an ordinary GET rather than a
RestoreObject call and a 5-12 hour wait. A backup you cannot read during the
incident is not a backup.

Report-only by default, matching every other script here. --execute writes.

Usage:
    python backup_sqlite.py                              # say what would happen
    python backup_sqlite.py --execute                    # take it and upload
    python backup_sqlite.py --execute --keep-local DIR   # also leave a copy
    python backup_sqlite.py --execute --no-upload        # local only
"""

import argparse
import datetime
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

# Django keeps its sessions here. This is the only table dropped, and it is
# dropped from the copy, never from the original -- see assert_is_not_source().
SESSION_TABLE = 'django_session'

DEFAULT_BUCKET = 'amprepo-backups'
STORAGE_CLASS = 'GLACIER_IR'

# The sha256 of the uncompressed vacuumed file, carried as object metadata so
# the next run can ask "has anything changed?" without downloading anything.
HASH_METADATA_KEY = 'content-sha256'


def default_db_path():
    """Where `caper.sqlite3` lives.

    settings.py sets NAME to the bare filename 'caper.sqlite3', which Django
    resolves against the working directory -- /srv/caper in the container. Do
    not read it from Django here: this script must be runnable when Django
    cannot start, which is exactly when a backup matters most.
    """
    root = os.environ.get('CAPER_ROOT')
    if root:
        return os.path.join(root, 'caper', 'caper.sqlite3')
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'caper', 'caper.sqlite3')


def assert_is_not_source(candidate, source):
    """Refuse to modify the live database.

    This function is the whole safety story of the script. Everything else it
    does is a read or a write to a temporary file; the one destructive
    statement is a DELETE, and it must land on a copy. Comparing realpaths
    rather than strings so a symlink or a relative path cannot slip past.
    """
    if os.path.realpath(candidate) == os.path.realpath(source):
        raise RuntimeError(
            'refusing to modify the live database at %s -- the session delete '
            'must only ever run against a copy' % source)


def snapshot(source, dest):
    """Copy a live SQLite database using the online backup API.

    `sqlite3.Connection.backup()` is safe against a database being written
    concurrently: it copies pages under the same locking discipline the engine
    uses itself, and retries pages that change mid-copy. A plain file copy of a
    live SQLite file is not safe and can produce a corrupt result that only
    shows up when you try to restore it.
    """
    assert_is_not_source(dest, source)
    src = sqlite3.connect('file:%s?mode=ro' % source, uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def strip_sessions(path, source):
    """Delete every session row from *path*, then compact it.

    Returns (rows_before, bytes_before, bytes_after). VACUUM INTO is used
    rather than plain VACUUM because it writes a fresh, fully compacted file
    rather than rewriting in place, which means the result has no free pages
    carried over from the 176 MiB it started as.
    """
    assert_is_not_source(path, source)
    conn = sqlite3.connect(path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if SESSION_TABLE in names:
            rows = conn.execute('SELECT COUNT(*) FROM %s' % SESSION_TABLE).fetchone()[0]
            conn.execute('DELETE FROM %s' % SESSION_TABLE)
            conn.commit()
        else:
            rows = 0
        before = os.path.getsize(path)
        compact = path + '.vacuumed'
        if os.path.exists(compact):
            os.unlink(compact)
        conn.execute("VACUUM INTO ?", (compact,))
    finally:
        conn.close()
    os.replace(compact, path)
    return rows, before, os.path.getsize(path)


def content_hash(path):
    """sha256 of the file's bytes, read in chunks."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def compress(src, dest):
    """gzip *src* to *dest* with a fixed mtime.

    mtime=0 matters: gzip stores the modification time in its header, so
    compressing identical content twice normally yields different bytes. That
    would not break the hash check -- which is taken on the uncompressed file --
    but it would make two backups of an unchanged database look different to
    anything comparing the uploaded objects, which is a trap worth not setting.
    """
    with open(src, 'rb') as fin, open(dest, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)


def latest_uploaded_hash(s3, bucket, prefix):
    """The content hash of the most recent backup already in the bucket.

    Returns None when there is nothing there yet, or when the newest object
    predates this script and so carries no hash metadata. Both cases mean "you
    cannot skip", which is the safe direction.
    """
    pages = s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix)
    newest = None
    for page in pages:
        for obj in page.get('Contents', []):
            if newest is None or obj['LastModified'] > newest['LastModified']:
                newest = obj
    if newest is None:
        return None
    head = s3.head_object(Bucket=bucket, Key=newest['Key'])
    return head.get('Metadata', {}).get(HASH_METADATA_KEY)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=None, help='path to caper.sqlite3')
    ap.add_argument('--env', default=os.environ.get('AMPLICON_ENV'),
                    help="deployment name; becomes the S3 prefix (fixes the "
                         "hardcoded 'prod/' in the old script)")
    ap.add_argument('--bucket', default=DEFAULT_BUCKET)
    ap.add_argument('--execute', action='store_true',
                    help='actually take the backup; without this, report only')
    ap.add_argument('--no-upload', action='store_true',
                    help='take the backup but do not send it to S3')
    ap.add_argument('--keep-local', metavar='DIR', default=None,
                    help='also leave a copy in DIR')
    ap.add_argument('--force', action='store_true',
                    help='upload even when the content is unchanged')
    args = ap.parse_args()

    source = args.db or default_db_path()
    if not os.path.exists(source):
        print('no database at %s' % source)
        return 2
    if not args.env:
        print('AMPLICON_ENV is not set and --env was not given. Refusing to '
              'guess: this is what made the old script overwrite prod backups '
              'from dev.')
        return 2

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    prefix = '%s/sqlite/' % args.env
    key = '%scaper-%s.sqlite3.gz' % (prefix, stamp)

    print('source        : %s (%s bytes)' % (source, os.path.getsize(source)))
    print('destination   : s3://%s/%s' % (args.bucket, key))
    print('storage class : %s' % STORAGE_CLASS)

    if not args.execute:
        print('\nREPORT ONLY -- nothing was copied or uploaded. Add --execute.')
        return 0

    workdir = tempfile.mkdtemp(prefix='caper-backup-')
    try:
        raw = os.path.join(workdir, 'caper.sqlite3')
        snapshot(source, raw)
        rows, before, after = strip_sessions(raw, source)
        digest = content_hash(raw)
        gz = raw + '.gz'
        compress(raw, gz)

        print('sessions removed : %d' % rows)
        print('size             : %d -> %d bytes (gz %d)'
              % (before, after, os.path.getsize(gz)))
        print('sha256           : %s' % digest)

        if args.keep_local:
            os.makedirs(args.keep_local, exist_ok=True)
            local = os.path.join(args.keep_local, os.path.basename(key))
            shutil.copy2(gz, local)
            print('kept locally     : %s' % local)

        if args.no_upload:
            print('\n--no-upload given; not sending to S3.')
            return 0

        import boto3
        s3 = boto3.client('s3')
        previous = latest_uploaded_hash(s3, args.bucket, prefix)
        if previous == digest and not args.force:
            print('\nunchanged since the last backup (%s); nothing uploaded.'
                  % digest[:12])
            return 0

        with open(gz, 'rb') as body:
            s3.put_object(Bucket=args.bucket, Key=key, Body=body,
                          StorageClass=STORAGE_CLASS,
                          Metadata={HASH_METADATA_KEY: digest})
        print('\nuploaded to s3://%s/%s' % (args.bucket, key))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
