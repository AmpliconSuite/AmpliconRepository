#!/usr/bin/env python
"""Delete GridFS files that no project document references. Report-only default.

``report_gridfs_orphans.py`` answers "how much residue is there?" and refuses to
act on the answer, for a good reason stated in ``gridfs_ownership``: both
production incidents behind this work were traversal bugs inside a count exactly
like that one. This script is the acting half, and it is written so that a wrong
count cannot by itself destroy anything.

**The ownership question is asked twice.** Once in bulk, to build the candidate
list, and again per file immediately before its delete. The second question is
cheap -- it reads that one ``fs.files`` row and checks whether a backlink has
appeared -- and it closes the window between "the survey ran" and "the delete
ran", which on a live site is minutes to hours wide.

**Ownership is computed by walking documents, never from a key list kept here.**
``iter_backlinks`` derives its keys from ``GRIDFS_FILE_KEYS`` in
``project_version_cleanup``, which is the same list ingestion writes. A second
list is the recurring defect in this repository: one copy was 8 spellings behind
and would have marked 80,170 live files as garbage, and the underscore spellings
missing from the hard-delete path stranded 116,480 files before that was fixed.

**Age guard.** A file is written to GridFS *before* the document that names it,
so a file uploaded while this runs looks unowned and is not. ``--min-age-hours``
(default 24) excludes anything recent. Do not lower it below the longest upload
you expect to be in flight; a 4,000-sample project takes hours.

**Reads are pinned to the primary.** The cluster URI carries
``readPreference=secondaryPreferred``, and a replica that has not caught up
reports a file as unreferenced when the document naming it already exists.

The undo record holds each row's metadata so the deletion is auditable and the
ids are recoverable, but **the bytes are not**. A cluster snapshot before an
``--execute`` run is the only route back for content.

Usage:

    sweep_gridfs_unreferenced.py --expect-db caper-dev
    sweep_gridfs_unreferenced.py --expect-db caper-dev --before 2025-01-01
    sweep_gridfs_unreferenced.py --expect-db caper-dev --before 2025-01-01 \
        --limit 100 --execute --undo-record /path/to/undo.jsonl
"""

import argparse
import datetime
import json
import os
import sys

from pymongo import MongoClient, ReadPreference

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.gridfs_backlinks import METADATA_FIELD, PROJECT_ID, iter_backlinks  # noqa: E402
from caper.project_version_cleanup import delete_gridfs_file_in_batches       # noqa: E402



def connect(expect_db):
    """Open the database named by the environment, having checked it is expected.

    ``--expect-db`` is deliberately *not* used to select the database. A guard
    that also chooses the target can never fail: it would simply connect to
    whatever it was told and report success.
    """
    uri = os.environ['DB_URI_SECRET']
    name = os.environ['DB_NAME']
    if name != expect_db:
        sys.exit('DB_NAME is %r but --expect-db says %r; refusing to run.'
                 % (name, expect_db))
    client = MongoClient(uri, read_preference=ReadPreference.PRIMARY,
                         serverSelectionTimeoutMS=30000)
    db = client[name]
    if db.name != expect_db:
        sys.exit('connected to %r, expected %r' % (db.name, expect_db))
    return db


def owned_file_ids(projects, progress=None):
    """Every file id named by any project document, whatever its status.

    Tombstones are included. A tombstone holding files is a deletion that did
    not finish, and that is a bug to fix at the deletion path, not something for
    a sweeper to tidy away -- tidying it here would hide it forever.

    Returns ``(owned_file_ids, seen_document_ids)``. The document ids are kept so
    a later pass can tell which documents are new since this walk.
    """
    owned, seen = set(), set()
    for document in projects.find({}):
        seen.add(document['_id'])
        for file_id, _, _ in iter_backlinks(document):
            owned.add(file_id)
        if progress and len(seen) % 25 == 0:
            progress(len(seen), len(owned))
    return owned, seen


def absorb_new_documents(projects, owned, seen):
    """Fold in any document created since the survey walked the collection.

    The survey is the slow part of this script -- minutes on a large database --
    and a project uploaded while it runs would have its files classified as
    residue, because the walk had already passed the point where the document
    would have appeared.

    Only documents whose ``_id`` was not seen are read, so this is cheap enough
    to run repeatedly. It does **not** catch a file newly referenced by a
    document that already existed; that case is a version edit, whose files were
    uploaded moments earlier, and ``--min-age-hours`` is what covers it.
    """
    added_files = added_docs = 0
    for document in projects.find({'_id': {'$nin': list(seen)}}):
        seen.add(document['_id'])
        added_docs += 1
        for file_id, _, _ in iter_backlinks(document):
            if file_id not in owned:
                owned.add(file_id)
                added_files += 1
    return added_docs, added_files


def _age_cutoff(hours):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)


def _uploaded_at(row):
    """When this file was written, preferring the recorded date to the id.

    ``uploadDate`` is what GridFS sets. The ObjectId's embedded timestamp is the
    fallback, and it is not the same thing: an id can be minted well before the
    write completes for a large file.
    """
    value = row.get('uploadDate')
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value
    return row['_id'].generation_time


def candidates(fs_files, owned, *, min_age_hours, before=None):
    """Rows no document names, old enough to be safe, optionally date-limited."""
    recent_cutoff = _age_cutoff(min_age_hours)
    found, skipped_recent = [], 0
    projection = {'length': 1, 'filename': 1, 'uploadDate': 1, METADATA_FIELD: 1}
    for row in fs_files.find({}, projection):
        if row['_id'] in owned:
            continue
        written = _uploaded_at(row)
        if written > recent_cutoff:
            skipped_recent += 1
            continue
        if before is not None and written >= before:
            continue
        found.append(row)
    return found, skipped_recent


def still_unreferenced(fs_files, owned, file_id):
    """Ask the ownership question again for one file, just before deleting it.

    Re-reading the row catches the two things that can change between the survey
    and this moment: the row being deleted by something else, and a backlink
    appearing because ``backfill_gridfs_backlinks`` or an upload claimed it.

    There is deliberately no "does a document name it" query here. File ids live
    in nested ``runs.<sample>.<key>`` fields, so no single query can find the
    documents naming an arbitrary id -- an earlier draft asked ``projects`` for
    the backlink field, which exists only on ``fs.files``, so it matched nothing
    and the check could never fire. Ownership is answered by walking documents,
    in ``owned_file_ids`` and ``absorb_new_documents``, and ``owned`` is that
    answer.
    """
    if file_id in owned:
        return False, 'a document names it'
    row = fs_files.find_one({'_id': file_id}, {METADATA_FIELD: 1})
    if row is None:
        return False, 'row is gone'
    if (row.get(METADATA_FIELD) or {}).get(PROJECT_ID):
        return False, 'a backlink appeared since the survey'
    return True, None


def human(num_bytes):
    value = float(num_bytes)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return '%.1f %s' % (value, unit)
        value /= 1024


def _age_histogram(rows):
    buckets = {}
    for row in rows:
        buckets.setdefault(_uploaded_at(row).year, [0, 0])
        buckets[_uploaded_at(row).year][0] += 1
        buckets[_uploaded_at(row).year][1] += row.get('length') or 0
    return buckets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True,
                        help='the database this run is for; checked against '
                             'DB_NAME, never used to select it')
    parser.add_argument('--min-age-hours', type=float, default=24.0,
                        help='never touch files written more recently than this '
                             '(default 24); protects uploads still in flight')
    parser.add_argument('--before', metavar='YYYY-MM-DD',
                        help='only files written before this date, so a sweep '
                             'can take the oldest tranche first')
    parser.add_argument('--limit', type=int, help='stop after this many files')
    parser.add_argument('--execute', action='store_true',
                        help='actually delete; without this nothing is written')
    parser.add_argument('--undo-record', help='path for the JSONL undo record '
                                              '(required with --execute)')
    args = parser.parse_args(argv)

    if args.execute and not args.undo_record:
        parser.error('--execute requires --undo-record')

    before = None
    if args.before:
        before = datetime.datetime.strptime(args.before, '%Y-%m-%d').replace(
            tzinfo=datetime.timezone.utc)

    db = connect(args.expect_db)
    projects, fs_files = db['projects'], db['fs.files']
    print('target: %s   %s' % (db.name, 'EXECUTE' if args.execute else 'REPORT-ONLY'))

    def progress(documents, files):
        print('  %d document(s), %d owned file(s) so far' % (documents, files))

    owned, seen = owned_file_ids(projects, progress)
    total = fs_files.count_documents({})
    print('\n%d project document(s) name %d distinct file(s); fs.files has %d row(s)'
          % (len(seen), len(owned), total))

    rows, skipped_recent = candidates(
        fs_files, owned, min_age_hours=args.min_age_hours, before=before)
    rows.sort(key=_uploaded_at)
    if args.limit:
        rows = rows[:args.limit]

    total_bytes = sum(row.get('length') or 0 for row in rows)
    print('unreferenced and eligible: %d file(s), %s' % (len(rows), human(total_bytes)))
    print('held back as too recent (< %g h): %d' % (args.min_age_hours, skipped_recent))
    if rows:
        print('\nby year written:')
        for year, (count, size) in sorted(_age_histogram(rows).items()):
            print('  %d  %8d file(s)  %10s' % (year, count, human(size)))

    if not args.execute:
        print('\nREPORT ONLY -- nothing deleted. Add --execute --undo-record FILE.')
        return 0

    undo = open(args.undo_record, 'a')

    def record(entry):
        """Append and fsync one entry *before* its file is touched."""
        undo.write(json.dumps(entry, default=str) + '\n')
        undo.flush()
        os.fsync(undo.fileno())

    record({'run_header': True, 'database': db.name,
            'started': datetime.datetime.now(datetime.timezone.utc),
            'candidates': len(rows), 'bytes': total_bytes,
            'note': 'ids and metadata are recoverable from this file; the bytes '
                    'are not -- only a cluster snapshot holds those'})

    # Documents uploaded while the survey was walking would otherwise have their
    # files read as residue. Fold them in before deleting anything, and again
    # every 500 files so a long run keeps catching up.
    new_docs, new_files = absorb_new_documents(projects, owned, seen)
    if new_docs:
        print('absorbed %d document(s) created during the survey, %d more owned file(s)'
              % (new_docs, new_files))

    deleted = deleted_bytes = kept = 0
    for row in rows:
        file_id = row['_id']
        if deleted and deleted % 500 == 0:
            absorb_new_documents(projects, owned, seen)
        ok, why = still_unreferenced(fs_files, owned, file_id)
        if not ok:
            kept += 1
            print('  keeping %s: %s' % (file_id, why))
            continue
        record({'_id': str(file_id), 'length': row.get('length'),
                'filename': row.get('filename'),
                'uploadDate': _uploaded_at(row),
                METADATA_FIELD: row.get(METADATA_FIELD)})
        delete_gridfs_file_in_batches(fs_files, db['fs.chunks'], file_id)
        deleted += 1
        deleted_bytes += row.get('length') or 0
        if deleted % 500 == 0:
            print('  %d deleted, %s' % (deleted, human(deleted_bytes)))

    print('\ndeleted %d file(s), %s' % (deleted, human(deleted_bytes)))
    print('kept %d that stopped being unreferenced between the survey and the delete'
          % kept)
    print('undo record: %s' % args.undo_record)
    print('fs.files now: %d row(s)' % fs_files.count_documents({}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
