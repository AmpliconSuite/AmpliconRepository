"""The two backups an administrator can take off AWS, and a record of when.

Every other copy of this site's data lands in the same AWS account as the thing
it protects: the cluster snapshots, and the nightly ``caper.sqlite3`` upload to
``s3://amprepo-backups``.  These two are the ones small enough to leave it --
the accounts database, which is in no DocumentDB snapshot at all, and the
metadata dump, which is every collection except the GridFS payload.

Neither file is built here.  ``backup_sqlite.py`` and ``dump_metadata.py``
already build them for the nightly job and the six-monthly command, and this
module calls those same functions.  A second implementation of "what belongs in
a backup" is the exact failure this repository keeps finding -- a list
maintained in two places, discovered when the two have already diverged.

What *is* new here is the record.  A copy whose age nobody knows is not a
backup, so every download writes one document naming who took it, when, and
what the totals were at that moment; the page then reports the difference
between those totals and the totals now.
"""

import datetime
import hashlib
import os
import sqlite3
import sys
import tarfile
import tempfile

from django.conf import settings

from .utils import db_handle, db_handle_primary


# backup_sqlite.py and dump_metadata.py sit at the repository root beside
# manage.py's parent, not inside the Django package.  They are imported rather
# than reimplemented, so this insert is deliberate and load-bearing.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import backup_sqlite  # noqa: E402
import dump_metadata  # noqa: E402


SQLITE = 'sqlite'
METADATA = 'metadata'
KINDS = (SQLITE, METADATA)

DOWNLOAD_RECORD_COLLECTION = 'admin_backup_downloads'


# ─────────────────────────────────────────────────────────────────────
# The record of who took a copy away
# ─────────────────────────────────────────────────────────────────────

def _records(primary=False):
    handle = db_handle_primary if primary else db_handle
    return handle[DOWNLOAD_RECORD_COLLECTION]


def record_download(kind, username, totals, size_bytes, digest):
    """Write down that a copy left the building.

    Written *after* the file is built and before it is streamed: a download the
    browser abandons half way still produced a complete file on this side, and
    recording it is the safer error -- it makes the next comparison say "no
    change since", which prompts someone to look, rather than silently claiming
    no copy exists.
    """
    _records(primary=True).insert_one({
        'kind': kind,
        'username': username,
        'downloaded_at': datetime.datetime.now(datetime.timezone.utc),
        'totals': totals,
        'size_bytes': size_bytes,
        'sha256': digest,
    })


def last_download(kind):
    """The most recent download of *kind*, or None."""
    return _records().find_one({'kind': kind}, sort=[('downloaded_at', -1)])


# ─────────────────────────────────────────────────────────────────────
# Totals -- cheap enough to compute on every page load
# ─────────────────────────────────────────────────────────────────────

def sqlite_totals():
    """Row counts per table in caper.sqlite3, excluding the session table.

    Sessions are dropped from every copy, so counting them would make the page
    report a change on every load and mean nothing.  These counts are what
    *survives* into the file, which is what a reader wants compared.
    """
    path = backup_sqlite.default_db_path()
    if not os.path.exists(path):
        return {}
    totals = {}
    connection = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            if name == backup_sqlite.SESSION_TABLE:
                continue
            totals[name] = connection.execute(
                'SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
    finally:
        connection.close()
    return totals


def metadata_totals():
    """Document counts per dumped collection.

    ``estimated_document_count()`` reads collection metadata instead of
    scanning, which matters because ``fs.files`` holds hundreds of thousands of
    rows and this runs on a page load.  The number can lag slightly; it is a
    change indicator and is labelled as one, never a manifest.
    """
    totals = {}
    for name in dump_metadata.collections_to_dump(db_handle):
        try:
            totals[name] = db_handle[name].estimated_document_count()
        except Exception:
            continue
    return totals


def compare(previous, current):
    """(added, removed, changed_collections) between two totals dicts."""
    if not previous:
        return None
    added = removed = 0
    names = set(previous) | set(current)
    changed = []
    for name in sorted(names):
        delta = current.get(name, 0) - previous.get(name, 0)
        if delta > 0:
            added += delta
        elif delta < 0:
            removed += -delta
        if delta:
            changed.append((name, delta))
    return {'added': added, 'removed': removed, 'changed': changed}


# ─────────────────────────────────────────────────────────────────────
# Building the files
# ─────────────────────────────────────────────────────────────────────

def build_sqlite(workdir):
    """The sessionless, gzipped accounts database.  Returns (path, sha256).

    The steps are backup_sqlite.py's own, in its order: snapshot through
    SQLite's online backup API so nothing locks and the live file is never
    touched, drop the sessions from the copy, then gzip.
    """
    source = backup_sqlite.default_db_path()
    snapshot_path = os.path.join(workdir, 'caper.sqlite3')
    backup_sqlite.snapshot(source, snapshot_path)
    backup_sqlite.strip_sessions(snapshot_path, source)
    digest = backup_sqlite.content_hash(snapshot_path)
    compressed = snapshot_path + '.gz'
    backup_sqlite.compress(snapshot_path, compressed)
    return compressed, digest


def build_metadata(workdir):
    """Every collection except the payload, as one .tar.gz.  Returns (path, manifest).

    dump_metadata.py writes a directory of gzipped JSON-lines files plus a
    manifest; a browser wants one file, so the directory is tarred.  The
    per-file hashes in the manifest are over the gzipped members and survive the
    tarring, so ``dump_metadata.py --verify-only`` still checks this download
    once it is unpacked.
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = 'metadata-%s-%s' % (db_handle.name, stamp)
    dump_dir = os.path.join(workdir, name)
    os.makedirs(dump_dir)

    manifest = dump_metadata.write_dump(db_handle, dump_dir)

    archive = os.path.join(workdir, '%s.tar.gz' % name)
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(dump_dir, arcname=name)
    return archive, manifest


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def workspace():
    """A temp directory for one build.  Caller is responsible for cleanup.

    Preferred inside PROJECT_DATA_ROOT, which is on the same volume the site
    already writes uploads to, so a 50 MB dump does not land on whatever
    happens to back /tmp in a container.  That directory is created lazily by
    the code that writes uploads, so it may not exist yet on a fresh
    deployment; falling back to the system temp directory is better than
    refusing to produce a backup.
    """
    root = getattr(settings, 'PROJECT_DATA_ROOT', None)
    if root:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            root = None
    return tempfile.mkdtemp(prefix='admin-backup-', dir=root or tempfile.gettempdir())
