#!/usr/bin/env python
"""Write ``fs.files.metadata`` backlinks for files stored before they were written.

``put_with_backlink()`` labels every file the site stores from now on. This
labels the ones already there: on prod, 942,279 files named by 311 documents,
measured 2026-08-29.

**It iterates documents, not files.** Building a map of every file id first
would mean holding ~950,000 ObjectIds and their context in memory at once; a
document at a time holds one project's worth, issues one bulk write, and moves
on. That also makes progress meaningful -- a document is either done or not --
which is what the checkpoint records.

**Authority is documents -> files.** Every backlink written here is derived from
a document that names the file. Nothing is deleted, no project document is
touched, and a file no document names is left exactly as it is: unlabelled, and
therefore visible to the orphan report as such.

**Idempotent.** A row whose backlink already names the right project is skipped
rather than rewritten, so a second run is nearly free and ``written_at`` does
not churn. Safe to interrupt: re-run with ``--resume``.

Report-only unless ``--execute``.
"""

import argparse
import json
import os
import sys
import time

from pymongo import ASCENDING, UpdateOne
from pymongo.errors import PyMongoError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from bson.objectid import ObjectId                      # noqa: E402
from caper.gridfs_backlinks import (                    # noqa: E402
    METADATA_FIELD, PROJECT_ID, build_metadata, iter_backlinks,
)

DEFAULT_CHECKPOINT = 'gridfs-backlink-checkpoint.json'


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    from pymongo import MongoClient
    return MongoClient(uri)[db_name]


def read_checkpoint(path):
    try:
        with open(path) as handle:
            return json.load(handle).get('last_project_id')
    except (OSError, ValueError):
        return None


def write_checkpoint(path, project_id, done, labelled):
    with open(path, 'w') as handle:
        json.dump({'last_project_id': str(project_id),
                   'documents_done': done,
                   'files_labelled': labelled}, handle)


def plan_for_document(fs_files, document, repair=False):
    """(operations, already_labelled, missing_rows) for one project document.

    By default this only labels rows that carry **no** backlink. A row that
    already names some document is left alone, because rewriting it is not
    idempotent when two documents name the same file: each run would hand the
    row to whichever document it saw last, and the backfill would never
    converge. (No file is named by two documents on dev or prod -- measured
    2026-08-29, distinct ids exactly equalled document-file pairs -- but the
    local fixtures do share ids, which is how this was found.)

    ``repair=True`` also rewrites backlinks that name a different document, for
    the case where the metadata is known to have gone wrong.
    """
    wanted = {}
    for file_id, sample_name, feature_key in iter_backlinks(document):
        # First writer wins within a document: a file appears once per slot, and
        # measured 2026-08-29 no file is named by two documents on either
        # database (distinct ids exactly equalled document-file pairs).
        wanted.setdefault(file_id, (sample_name, feature_key))
    if not wanted:
        return [], 0, 0

    existing = {row['_id']: (row.get(METADATA_FIELD) or {}).get(PROJECT_ID)
                for row in fs_files.find({'_id': {'$in': list(wanted)}},
                                         {f'{METADATA_FIELD}.{PROJECT_ID}': 1})}

    project_id = document['_id']
    chain_id = document.get('version_chain_id')
    operations, correct = [], 0
    for file_id, (sample_name, feature_key) in wanted.items():
        if file_id not in existing:
            continue                       # no such row; I12's finding, not ours
        labelled_as = existing[file_id]
        if labelled_as is not None:
            correct += 1
            if not (repair and labelled_as != project_id):
                continue
        metadata = build_metadata(project_id, sample_name=sample_name,
                                  feature_key=feature_key,
                                  version_chain_id=chain_id)
        operations.append(UpdateOne({'_id': file_id},
                                    {'$set': {METADATA_FIELD: metadata}}))
    return operations, correct, len(wanted) - len(existing)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='process at most N documents, for staging')
    parser.add_argument('--batch', type=int, default=500, metavar='N',
                        help='bulk operations per write (default 500)')
    parser.add_argument('--checkpoint', default=DEFAULT_CHECKPOINT)
    parser.add_argument('--repair', action='store_true',
                        help='also rewrite backlinks that name a different '
                             'document. Off by default: with two documents '
                             'naming one file it would not converge.')
    parser.add_argument('--resume', action='store_true',
                        help='continue after the last document the checkpoint '
                             'records as done')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    projects = database['projects']
    fs_files = database['fs.files']
    print(f'target: {db_name} ({args.expect_host})')

    query = {}
    if args.resume:
        last = read_checkpoint(args.checkpoint)
        if last:
            query['_id'] = {'$gt': ObjectId(last)}
            print(f'resuming after {last}')

    cursor = projects.find(query).sort('_id', ASCENDING)
    if args.limit:
        cursor = cursor.limit(args.limit)

    started = time.time()
    documents = to_write = correct = missing = written = 0
    pending = []

    for document in cursor:
        documents += 1
        operations, already, absent = plan_for_document(
            fs_files, document, repair=args.repair)
        to_write += len(operations)
        correct += already
        missing += absent

        if args.execute:
            pending.extend(operations)
            while len(pending) >= args.batch:
                chunk, pending = pending[:args.batch], pending[args.batch:]
                try:
                    fs_files.bulk_write(chunk, ordered=False)
                    written += len(chunk)
                except PyMongoError as exc:
                    print(f'  bulk_write failed on {len(chunk)} op(s): {exc}')
            write_checkpoint(args.checkpoint, document['_id'], documents, written)

        if documents % 25 == 0:
            print(f'  {documents} document(s), {to_write} to label, '
                  f'{correct} already correct, {written} written')

    if args.execute and pending:
        try:
            fs_files.bulk_write(pending, ordered=False)
            written += len(pending)
        except PyMongoError as exc:
            print(f'  final bulk_write failed on {len(pending)} op(s): {exc}')

    elapsed = time.time() - started
    print(f'\n{documents} document(s) in {elapsed:.1f}s')
    print(f'  to label       : {to_write}')
    print(f'  already labelled: {correct}')
    if missing:
        print(f'  named but absent from fs.files: {missing}   '
              f'(that is I12\'s finding, not this script\'s to fix)')

    total = fs_files.estimated_document_count()
    labelled = fs_files.count_documents({f'{METADATA_FIELD}.{PROJECT_ID}':
                                         {'$exists': True}})
    print(f'\nfs.files: {labelled} of {total} row(s) carry a backlink')

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
    else:
        print(f'{written} row(s) written. checkpoint: {args.checkpoint}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
