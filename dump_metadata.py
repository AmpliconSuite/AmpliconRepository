#!/usr/bin/env python
"""
Dump every collection except the GridFS payload, for an off-AWS copy.

99.5% of this database is blob payload. Measured on prod 2026-08-31:

    fs.chunks              2,270,862 docs   361.90 GiB
    fs.files               1,065,009 docs     0.30 GiB
    projects                     311 docs     0.30 GiB
    project_audit_log            367 docs      ~0
    project_version_chains       223 docs      ~0
    everything else              579 docs      ~0
                                            ---------
    total                                    362.50 GiB
    total excluding fs.chunks                  0.60 GiB

That asymmetry is what makes an off-AWS copy practical at all. The irreplaceable
part -- which project exists, what version it is, who owns it, what happened to
it, and which GridFS ids its samples point at -- is 0.17% of the bytes. It moves
in about two minutes and costs roughly $0.06 in egress. The payload is 17 hours
and about $35, and is already covered by daily cluster snapshots with 35-day
retention.

So this is not a backup of the database. It is a backup of the *shape* of the
site: enough to say exactly what was lost, to restore every document except the
blobs, and to tell which blobs would need to come back from a snapshot. Take it
bi-monthly and after every migration.

`fs.files` is deliberately included even though it is the largest thing here
after the payload. It is the catalogue: without it, a restored `projects`
collection names GridFS ids that nothing can resolve, and the backlink metadata
written since 2026-08-29 (942,279 of 1,065,009 files on prod) is the only record
of which document each blob belongs to.

Extended JSON, not plain JSON, because ObjectIds and datetimes must survive the
round trip -- a dump that silently turns an ObjectId into a string cannot be
restored from.

Read-only. It never writes to the database, and has no mode that does.

Usage:
    python dump_metadata.py --expect-db caper --out ./amprepo-metadata
    python dump_metadata.py --expect-db caper --out DIR --verify-only DUMPDIR
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import sys

from bson import json_util
from pymongo import MongoClient, ReadPreference

# The one collection this tool exists to leave out.
PAYLOAD_COLLECTION = 'fs.chunks'

MANIFEST_NAME = 'manifest.json'


def collections_to_dump(db):
    """Every collection except the payload, in a stable order.

    Discovered rather than listed, so a collection added later is included
    without anyone remembering to update this file. That is the same failure --
    a list maintained in two places -- that this repository keeps finding.
    """
    return sorted(n for n in db.list_collection_names() if n != PAYLOAD_COLLECTION)


def dump_collection(db, name, out_dir):
    """Write one collection as gzipped extended-JSON lines.

    Returns (documents, bytes_on_disk, sha256). The hash is over the compressed
    file, which is what a later verify compares, so a bit-flip in transit or on
    the destination drive is caught rather than discovered at restore time.
    """
    path = os.path.join(out_dir, '%s.jsonl.gz' % name)
    digest = hashlib.sha256()
    count = 0
    with open(path, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz:
            for doc in db[name].find({}, no_cursor_timeout=False):
                line = (json_util.dumps(doc) + '\n').encode('utf-8')
                gz.write(line)
                count += 1
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return count, os.path.getsize(path), digest.hexdigest()


def verify(dump_dir):
    """Re-read a dump and check it against its own manifest.

    Two different failures are checked because they fail differently: a
    corrupted file has the wrong hash, and a truncated one has the right hash
    for fewer lines than it claims. Counting lines means decompressing and
    parsing everything, which is the point -- an unverified dump is the same
    unverified hypothesis as an unrestored snapshot.
    """
    with open(os.path.join(dump_dir, MANIFEST_NAME)) as f:
        manifest = json.load(f)

    problems = []
    for name, expected in sorted(manifest['collections'].items()):
        path = os.path.join(dump_dir, '%s.jsonl.gz' % name)
        if not os.path.exists(path):
            problems.append('%s: file missing' % name)
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected['sha256']:
            problems.append('%s: sha256 mismatch' % name)
            continue
        lines = 0
        with gzip.open(path, 'rb') as f:
            for line in f:
                json_util.loads(line.decode('utf-8'))
                lines += 1
        if lines != expected['documents']:
            problems.append('%s: %d documents on disk, manifest says %d'
                            % (name, lines, expected['documents']))
        print('  %-26s %8d documents  OK' % (name, lines))
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--expect-db', required=True,
                    help='database name this run is meant to read. Asserted '
                         'against the database the environment actually points '
                         'at -- dev and prod share one cluster, so the name is '
                         'never taken as the instruction of where to connect')
    ap.add_argument('--out', help='directory to write the dump into')
    ap.add_argument('--verify-only', metavar='DUMPDIR',
                    help='check an existing dump against its manifest and exit')
    args = ap.parse_args()

    if args.verify_only:
        print('verifying %s' % args.verify_only)
        problems = verify(args.verify_only)
        if problems:
            print('\nFAILED:')
            for p in problems:
                print('  ' + p)
            return 1
        print('\nverified: every file matches its hash and document count.')
        return 0

    if not args.out:
        print('--out is required unless --verify-only is given')
        return 2

    uri = os.environ.get('DB_URI_SECRET')
    if not uri:
        print('DB_URI_SECRET is not set')
        return 2

    # Pinned to the primary: a metadata copy taken from a lagging replica is a
    # copy of a moment that never existed as a whole.
    client = MongoClient(uri, read_preference=ReadPreference.PRIMARY,
                         serverSelectionTimeoutMS=20000)
    # DB_NAME decides where this connects; --expect-db only decides whether
    # that was the intention. Using --expect-db to *select* the database would
    # make the check vacuous -- it would always be reading whatever it was told
    # to read, and would agree with itself. A test holds this.
    configured = os.environ.get('DB_NAME')
    if not configured:
        print('DB_NAME is not set; refusing to guess which database to read')
        return 2
    db = client[configured]
    if db.name != args.expect_db:
        print('connected to %r, but --expect-db says %r. Check which config.sh '
              'is sourced.' % (db.name, args.expect_db))
        return 2

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_dir = os.path.join(args.out, '%s-%s' % (db.name, stamp))
    os.makedirs(out_dir, exist_ok=True)

    print('database : %s' % db.name)
    print('excluding: %s' % PAYLOAD_COLLECTION)
    print('into     : %s\n' % out_dir)

    manifest = {'database': db.name, 'taken': stamp,
                'excluded': [PAYLOAD_COLLECTION], 'collections': {}}
    total_bytes = 0
    for name in collections_to_dump(db):
        count, size, digest = dump_collection(db, name, out_dir)
        manifest['collections'][name] = {'documents': count, 'bytes': size,
                                         'sha256': digest}
        total_bytes += size
        print('  %-26s %8d documents  %10d bytes' % (name, count, size))

    # Recorded but not dumped, so a reader of the manifest can see what the
    # copy is missing rather than having to know.
    try:
        skipped = db.command({'collStats': PAYLOAD_COLLECTION})
        manifest['excluded_stats'] = {
            'collection': PAYLOAD_COLLECTION,
            'documents': skipped.get('count'),
            'size': skipped.get('size'),
        }
    except Exception:
        manifest['excluded_stats'] = {'collection': PAYLOAD_COLLECTION,
                                      'documents': None, 'size': None}

    with open(os.path.join(out_dir, MANIFEST_NAME), 'w') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    print('\ntotal: %d bytes across %d collections'
          % (total_bytes, len(manifest['collections'])))
    ex = manifest['excluded_stats']
    if ex.get('size'):
        print('excluded %s: %s documents, %.2f GiB -- covered by cluster snapshots'
              % (ex['collection'], format(ex['documents'], ','),
                 ex['size'] / 1024 ** 3))
    print('\nverify with: python dump_metadata.py --verify-only %s' % out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
