#!/usr/bin/env python
"""Record ``metadata.sha256`` on files stored before uploads recorded it.

A new version reuses an identical file its predecessor names instead of storing
it again, and it finds the file by that hash (see version_file_sharing). Files
stored before #718 have none, so without this the next version of every project
stores all of its files once more, and sharing starts only with the version
after that.

**Scope: the files the current versions name, less the project archives.** A
version is only ever made from the current version of its project, so a file
only an older version names is never matched, and hashing it would buy nothing.
The project archive (``tarfile``) is stored by its own path and never matched.
Measured read-only on prod 2026-11-03: 89 current versions name 228,153
per-sample files, 15.65 GiB, of which none carried a hash.

**The hash is of the stored bytes, read back whole.** A file is hashed only when
the bytes read equal the row's ``length``; anything else (a missing chunk, a
short read, a read error) is counted and left without a hash, so it is never
matched. A wrong hash is the one thing this could do harm with: a later version
would name a file whose content is not what it uploaded.

**Reads go to a replica, writes and the "still unhashed?" check to the
primary.** The bytes are the bulk of the work and a stored file never changes,
so the primary is spared them. A file too new for the replica reads short and
is skipped; nothing in scope is that new, since a current version's files were
stored when it was made.

**Only rows without a hash are written,** and the write repeats that condition,
so a second run, or a run beside an upload that hashes its own files, changes
nothing it should not. Report-only unless ``--execute``; it then reads no file
contents and only counts.
"""

import argparse
import hashlib
import os
import sys
import time

from pymongo import ASCENDING, UpdateOne
from pymongo.errors import PyMongoError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from bson.objectid import ObjectId                      # noqa: E402
from caper.gridfs_backlinks import METADATA_FIELD, iter_backlinks  # noqa: E402
from caper.project_status import LIVE, STATUS_QUERIES   # noqa: E402
from caper.project_version_cleanup import PROJECT_FILE_KEYS  # noqa: E402
from caper.version_file_sharing import SHA256, field_name  # noqa: E402

HASH_FIELD = f'{METADATA_FIELD}.{SHA256}'
READ_BLOCK = 1 << 20


def connect(db_name, expect_host):
    """(primary database, replica-preferred database)."""
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    from pymongo import MongoClient, ReadPreference
    client = MongoClient(uri)
    return (client.get_database(db_name, read_preference=ReadPreference.PRIMARY),
            client.get_database(db_name, read_preference=ReadPreference.SECONDARY_PREFERRED))


def files_in_scope(document):
    """The GridFS ids *document* names, less its project archive."""
    project_keys = {field_name(key) for key in PROJECT_FILE_KEYS}
    return {file_id for file_id, _sample, key in iter_backlinks(document)
            if field_name(key) not in project_keys}


def digest_stored(gridfs_handle, file_id, length):
    """sha256 hex of the stored file, or None unless exactly *length* bytes read back."""
    digest, read = hashlib.sha256(), 0
    try:
        with gridfs_handle.get(file_id) as stored:
            while True:
                block = stored.read(READ_BLOCK)
                if not block:
                    break
                digest.update(block)
                read += len(block)
    except Exception as exc:                       # noqa: BLE001 -- counted, never hashed
        print(f'  unreadable {file_id}: {type(exc).__name__}: {exc}')
        return None
    if read != length:
        print(f'  short read {file_id}: {read} of {length} bytes')
        return None
    return digest.hexdigest()


def rollback(fs_files, path, batch, execute):
    """Remove the hashes a previous run wrote, from that run's --undo file.

    Every id in the file carried no hash before that run, so the inverse is an
    ``$unset`` of the hash alone. A version that already reused one of these
    files keeps it: the hash only decides what the next version may match.
    """
    with open(path) as handle:
        ids = [ObjectId(line.strip()) for line in handle if line.strip()]
    print(f'{len(ids)} id(s) recorded in {path}')
    if not execute:
        print('report only. Re-run with --execute to unset them.')
        return 0
    cleared = 0
    for start in range(0, len(ids), batch):
        result = fs_files.update_many({'_id': {'$in': ids[start:start + batch]}},
                                      {'$unset': {HASH_FIELD: ''}})
        cleared += result.modified_count
    print(f'{cleared} row(s) cleared')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='read and hash the files and write. Without it this '
                             'counts what is in scope and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='process at most N current versions, for staging')
    parser.add_argument('--project', action='append', default=[], metavar='ID',
                        help='only this current version (repeatable)')
    parser.add_argument('--batch', type=int, default=500, metavar='N',
                        help='writes per bulk write (default 500)')
    parser.add_argument('--max-mib-per-s', type=float, metavar='R',
                        help='pause between files to read no faster than this')
    parser.add_argument('--undo', metavar='PATH',
                        help='append the id of every row written, one per line, '
                             'before writing it. Required with --execute.')
    parser.add_argument('--rollback', metavar='PATH',
                        help='unset the hash on every id in a previous run\'s '
                             '--undo file. Needs --execute. Does nothing else.')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    if args.execute and not (args.undo or args.rollback):
        sys.exit('--execute needs --undo PATH: a write with no recorded '
                 'inverse is not one this repo makes.')
    primary, replica = connect(db_name, args.expect_host)
    assert primary.name == db_name
    fs_files = primary['fs.files']
    print(f'target: {db_name} ({args.expect_host})')

    if args.rollback:
        return rollback(fs_files, args.rollback, args.batch, args.execute)

    import gridfs
    stored = gridfs.GridFS(replica)
    undo = open(args.undo, 'a') if args.execute else None

    query = dict(STATUS_QUERIES[LIVE])
    if args.project:
        query = {'$and': [query, {'_id': {'$in': [ObjectId(p) for p in args.project]}}]}
    cursor = primary['projects'].find(query).sort('_id', ASCENDING)
    if args.limit:
        cursor = cursor.limit(args.limit)

    started = time.time()
    documents = in_scope = already = to_hash = bytes_to_hash = 0
    hashed = bytes_hashed = unreadable = written = 0
    pending = []

    def flush():
        """Record the inverse, then write. In that order, fsynced between."""
        nonlocal written, pending
        if not pending:
            return
        for file_id, _ in pending:
            undo.write(f'{file_id}\n')
        undo.flush()
        os.fsync(undo.fileno())
        try:
            result = fs_files.bulk_write([op for _, op in pending], ordered=False)
            written += result.modified_count
        except PyMongoError as exc:
            print(f'  bulk_write failed on {len(pending)} op(s): {exc}')
        pending = []

    for document in cursor:
        documents += 1
        ids = list(files_in_scope(document))
        in_scope += len(ids)
        rows = []
        for start in range(0, len(ids), 2000):
            for row in fs_files.find({'_id': {'$in': ids[start:start + 2000]}},
                                     {'length': 1, HASH_FIELD: 1}):
                if (row.get(METADATA_FIELD) or {}).get(SHA256):
                    already += 1
                else:
                    rows.append(row)
        to_hash += len(rows)
        doc_bytes = sum(row.get('length') or 0 for row in rows)
        bytes_to_hash += doc_bytes
        print(f'{document["_id"]} {document.get("project_name", "")!r}: '
              f'{len(rows)} file(s), {doc_bytes / 2**20:,.1f} MiB to hash')
        if not args.execute:
            continue

        for row in rows:
            length = row.get('length') or 0
            began = time.time()
            sha256 = digest_stored(stored, row['_id'], length)
            if sha256 is None:
                unreadable += 1
                continue
            hashed += 1
            bytes_hashed += length
            pending.append((row['_id'], UpdateOne(
                {'_id': row['_id'], HASH_FIELD: {'$exists': False}},
                {'$set': {HASH_FIELD: sha256}})))
            if len(pending) >= args.batch:
                flush()
            if args.max_mib_per_s:
                pause = length / 2**20 / args.max_mib_per_s - (time.time() - began)
                if pause > 0:
                    time.sleep(pause)
        flush()

    if undo is not None:
        undo.close()
    elapsed = time.time() - started
    print(f'\n{documents} current version(s) in {elapsed:.1f}s')
    print(f'  files in scope      : {in_scope}')
    print(f'  already hashed      : {already}')
    print(f'  to hash             : {to_hash} ({bytes_to_hash / 2**30:,.2f} GiB)')
    if args.execute:
        rate = bytes_hashed / 2**20 / elapsed if elapsed else 0
        print(f'  hashed              : {hashed} ({bytes_hashed / 2**30:,.2f} GiB, {rate:,.1f} MiB/s)')
        print(f'  unreadable or short : {unreadable}')
        print(f'  rows written        : {written}')
    else:
        print('\nreport only. Re-run with --execute --undo PATH to hash and write.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
