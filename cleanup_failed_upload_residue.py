#!/usr/bin/env python
"""Delete GridFS files a failed ingestion left behind. Report-only by default.

``sweep_gridfs_unreferenced.py`` handles residue that carries **no** backlink,
and it deliberately refuses to touch a row that has one -- that guard is what
stops it deleting a file claimed between its survey and its delete. The effect
is that it can never clean up after a failed upload, because since 2026-08-29
(`4931346`) an upload labels its files at ``fs.put()`` time and the wreckage of
a crash is labelled too. This script is the other half.

**The mechanism, measured on prod 2026-09-01.** Files are written to GridFS
*before* the document that names them. TCGA_Sarcoma's upload of 2026-08-14
wrote 3,025 files in 90 seconds, its document recorded only the 330 it managed
before failing, and the remaining 2,695 were orphaned on the spot -- 330 + 2,695
being exactly the count the successful retry recorded. No deletion was involved.
Deletes are not the orphan factory; interrupted uploads are.

**What is deleted, and what is not.** The label comes from
``gridfs_backlinks.classify_file``, the same pure predicate the orphan report
uses -- not a second copy of the rule, which is the defect this codebase
produces most often.

    document-gone                 deleted: the named document no longer exists
    unreferenced-by-its-document  deleted: the named document exists and does
                                  not name this file -- a failed upload, or a
                                  version edit whose old ids were replaced
    live                          never
    unlabelled                    never -- that is the other script's territory
    tombstone-payload             never by default: a tombstone still holding
                                  files is a deletion that did not finish, and
                                  quietly tidying it away hides the bug

**The age guard is load-bearing here, more than anywhere else.** During an
upload every file is written before the document names it, so for the whole
duration of an ingestion its files are *correctly* labelled
``unreferenced-by-its-document``. A large project takes hours.
``--min-age-hours`` (default 24) is the only thing between this script and a
live upload. Do not lower it.

**Ownership is asked twice**, as in the sweeper: once to build the list, then
again per file immediately before its delete, because the document may have
claimed the file in between.

The undo record holds each row's id, length and metadata, so what went is
auditable and identifiable. **The bytes are not recoverable from it** -- only a
cluster snapshot holds those.

Usage:

    cleanup_failed_upload_residue.py --expect-db caper-dev
    cleanup_failed_upload_residue.py --expect-db caper --limit 500 \
        --execute --undo-record /path/to/undo.jsonl
"""

import argparse
import collections
import datetime
import json
import os
import sys

from pymongo import MongoClient, ReadPreference

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.gridfs_backlinks import (                                # noqa: E402
    DOCUMENT_GONE, LIVE_FILE, METADATA_FIELD, PROJECT_ID, TOMBSTONE_PAYLOAD,
    UNLABELLED, UNREFERENCED_BY_ITS_DOCUMENT, as_object_id, classify_file,
    iter_backlinks,
)
from caper.project_version_cleanup import delete_gridfs_file_in_batches  # noqa: E402

#: The only labels this script will ever delete.
DELETABLE = (DOCUMENT_GONE, UNREFERENCED_BY_ITS_DOCUMENT)

#: Reported, never deleted. Each is a different reason to leave a row alone.
RETAINED = (LIVE_FILE, UNLABELLED, TOMBSTONE_PAYLOAD)


def connect(expect_db):
    """Open the database the environment names, having checked it is expected.

    ``--expect-db`` is deliberately not used to select the database. A guard
    that also chooses the target can never fail.
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


def load_documents(projects, progress=None):
    """``{document_id: (document, referenced_ids)}`` for every project document.

    Held in memory deliberately: ``classify_file`` takes the document and the
    ids it names rather than querying, so that it stays pure and testable, and
    re-reading a document per file would be a query per row.
    """
    documents = {}
    for n, document in enumerate(projects.find({}), 1):
        documents[document['_id']] = (
            document, {file_id for file_id, _, _ in iter_backlinks(document)})
        if progress and n % 25 == 0:
            progress(n)
    return documents


def _uploaded_at(row):
    """When this row was written, preferring the recorded date to the id.

    An ObjectId's timestamp is when the id was minted, which for a large file
    can be well before the write completed.
    """
    value = row.get('uploadDate')
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    return row['_id'].generation_time


def label_for(row, documents):
    """Classify one ``fs.files`` row against the loaded documents."""
    metadata = row.get(METADATA_FIELD)
    named = (metadata or {}).get(PROJECT_ID)
    document, referenced = None, set()
    if named is not None:
        entry = documents.get(as_object_id(named)) or documents.get(named)
        if entry is not None:
            document, referenced = entry
    return classify_file(row['_id'], metadata, document, referenced)


def candidates(fs_files, documents, *, min_age_hours):
    """Rows safe to consider, plus the census of everything examined."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=min_age_hours))
    census = collections.Counter()
    bytes_by_label = collections.Counter()
    found, skipped_recent = [], 0
    projection = {'length': 1, 'filename': 1, 'uploadDate': 1, METADATA_FIELD: 1}
    for row in fs_files.find({}, projection):
        label = label_for(row, documents)
        census[label] += 1
        bytes_by_label[label] += row.get('length') or 0
        if label not in DELETABLE:
            continue
        if _uploaded_at(row) > cutoff:
            skipped_recent += 1
            continue
        found.append((row, label))
    return found, skipped_recent, census, bytes_by_label


def still_deletable(fs_files, documents, file_id):
    """Ask again for one file, immediately before deleting it.

    Re-reads the row so that a document claiming the file between the survey and
    now takes it back. *documents* is re-consulted rather than trusted from the
    first pass for the same reason.
    """
    row = fs_files.find_one({'_id': file_id},
                            {METADATA_FIELD: 1, 'length': 1})
    if row is None:
        return False, 'row is gone'
    label = label_for(row, documents)
    if label not in DELETABLE:
        return False, 'now classified %s' % label
    return True, label


def human(num_bytes):
    value = float(num_bytes)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return '%.1f %s' % (value, unit)
        value /= 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True,
                        help='the database this run is for; checked against '
                             'DB_NAME, never used to select it')
    parser.add_argument('--min-age-hours', type=float, default=24.0,
                        help='never touch rows written more recently than this '
                             '(default 24). During an upload every file is '
                             'legitimately unreferenced; this is what protects '
                             'an ingestion in flight. Do not lower it.')
    parser.add_argument('--limit', type=int, help='stop after this many files')
    parser.add_argument('--max-delete', type=int, default=5000, metavar='N',
                        help='refuse to delete anything at all if more than N '
                             'files are eligible (default 5000). For the '
                             'scheduled run: a normal night is a handful of '
                             'files from one failed upload, so a sudden jump '
                             'means something changed and a person should look '
                             'before the bytes go. Raise it deliberately, in '
                             'the invocation, having seen the report.')
    parser.add_argument('--execute', action='store_true',
                        help='actually delete; without this nothing is written')
    parser.add_argument('--undo-record', help='path for the JSONL undo record '
                                              '(required with --execute)')
    args = parser.parse_args(argv)

    if args.execute and not args.undo_record:
        parser.error('--execute requires --undo-record')

    db = connect(args.expect_db)
    projects, fs_files = db['projects'], db['fs.files']
    print('target: %s   %s' % (db.name, 'EXECUTE' if args.execute else 'REPORT-ONLY'))

    documents = load_documents(
        projects, lambda n: print('  %d document(s) loaded' % n))
    print('\n%d project document(s) loaded' % len(documents))

    rows, skipped_recent, census, bytes_by_label = candidates(
        fs_files, documents, min_age_hours=args.min_age_hours)

    print('\n%-32s %10s %12s' % ('label', 'files', 'bytes'))
    for label in DELETABLE + RETAINED:
        print('%-32s %10d %12s   %s'
              % (label, census[label], human(bytes_by_label[label]),
                 'DELETABLE' if label in DELETABLE else 'retained'))

    rows.sort(key=lambda pair: _uploaded_at(pair[0]))
    if args.limit:
        rows = rows[:args.limit]
    total_bytes = sum(row.get('length') or 0 for row, _ in rows)
    print('\neligible after the %g h age guard: %d file(s), %s'
          % (args.min_age_hours, len(rows), human(total_bytes)))
    print('held back as too recent: %d' % skipped_recent)

    if not args.execute:
        print('\nREPORT ONLY -- nothing deleted. Add --execute --undo-record FILE.')
        return 0

    # Checked after the report is printed, so the operator sees what was found
    # before being told it will not be touched.
    if len(rows) > args.max_delete:
        print('\nREFUSING TO DELETE: %d file(s) eligible, --max-delete is %d.'
              % (len(rows), args.max_delete))
        print('A scheduled run expects a handful of files from one failed '
              'upload. This is not that. Look at the report above, then re-run '
              'with a --max-delete you chose on purpose.')
        return 2

    if not rows:
        print('\nNothing to delete.')
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
            'min_age_hours': args.min_age_hours,
            'note': 'ids and metadata are recoverable from this file; the bytes '
                    'are not -- only a cluster snapshot holds those'})

    deleted = deleted_bytes = kept = 0
    by_label = collections.Counter()
    for row, label in rows:
        file_id = row['_id']
        ok, why = still_deletable(fs_files, documents, file_id)
        if not ok:
            kept += 1
            print('  keeping %s: %s' % (file_id, why))
            continue
        record({'_id': str(file_id), 'length': row.get('length'),
                'filename': row.get('filename'),
                'uploadDate': _uploaded_at(row),
                'label': why,
                METADATA_FIELD: row.get(METADATA_FIELD)})
        delete_gridfs_file_in_batches(fs_files, db['fs.chunks'], file_id)
        deleted += 1
        by_label[why] += 1
        deleted_bytes += row.get('length') or 0
        if deleted % 500 == 0:
            print('  %d deleted, %s' % (deleted, human(deleted_bytes)))

    print('\ndeleted %d file(s), %s' % (deleted, human(deleted_bytes)))
    for label, count in by_label.most_common():
        print('  %-32s %d' % (label, count))
    print('kept %d that stopped being deletable between the survey and the delete'
          % kept)
    print('undo record: %s' % args.undo_record)
    print('fs.files now: %d row(s)' % fs_files.count_documents({}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
