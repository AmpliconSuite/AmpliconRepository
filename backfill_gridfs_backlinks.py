#!/usr/bin/env python
"""Write ``fs.files.metadata`` backlinks for files stored before they were written.

``put_with_backlink()`` labels every file the site stores from now on. This
labels the ones already there: 602,577 ids named by 246 documents on dev, and
942,279 named by 311 on prod, both measured 2026-08-29. Neither number is the
size of ``fs.files`` -- on dev that collection holds 790,895 rows, so 188,320 of
them are named by no document at all. Those are left exactly as they are;
``report_gridfs_orphans.py`` is what accounts for them.

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


def ensure_index(fs_files):
    """Index the backlink. Without it the backlink buys nothing.

    The point of writing ``metadata.project_id`` is that "which files belong to
    this project?" stops being a walk of every document. Unindexed it is instead
    a scan of every *file* -- 790,895 rows on dev, more on prod -- once per
    project, which is worse than the traversal it replaced.
    """
    name = fs_files.create_index([(f'{METADATA_FIELD}.{PROJECT_ID}', ASCENDING)],
                                 name='idx_fs_files_backlink_project')
    print(f'index ready: {name}')
    return name


def rollback(fs_files, path, batch, execute):
    """Remove the metadata a previous run wrote, from that run's --undo file.

    Every id in the file was a row carrying no backlink before the run, so the
    inverse is an unconditional ``$unset`` of the whole subdocument. Only ids
    the file names are touched: a row this script never wrote is not this
    script's to clear.
    """
    ids = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                ids.append(ObjectId(line))
    print(f'{len(ids)} id(s) recorded in {path}')
    if not execute:
        print('report only. Re-run with --execute to unset them.')
        return 0
    cleared = 0
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        result = fs_files.update_many({'_id': {'$in': chunk}},
                                      {'$unset': {METADATA_FIELD: ''}})
        cleared += result.modified_count
    print(f'{cleared} row(s) cleared')
    return 0


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
        # Carried as a pair rather than reading the id back off the UpdateOne:
        # the filter is private to pymongo, and the undo record must not depend
        # on an attribute that a library upgrade is free to rename.
        operations.append((file_id,
                           UpdateOne({'_id': file_id},
                                     {'$set': {METADATA_FIELD: metadata}})))
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
    parser.add_argument('--undo', metavar='PATH',
                        help='append the id of every row written, one per '
                             'line. The inverse of this run is $unset of '
                             'metadata on exactly those rows. Required with '
                             '--execute.')
    parser.add_argument('--repair', action='store_true',
                        help='also rewrite backlinks that name a different '
                             'document. Off by default: with two documents '
                             'naming one file it would not converge.')
    parser.add_argument('--rollback', metavar='PATH',
                        help='undo a previous run: read the ids from its --undo '
                             'file and remove the metadata this script put on '
                             'them. Needs --execute. Does nothing else.')
    parser.add_argument('--resume', action='store_true',
                        help='continue after the last document the checkpoint '
                             'records as done')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    if args.execute and not (args.undo or args.rollback):
        sys.exit('--execute needs --undo PATH: a write with no recorded '
                 'inverse is not one this repo makes.')
    database = connect(db_name, args.expect_host)
    projects = database['projects']
    fs_files = database['fs.files']
    print(f'target: {db_name} ({args.expect_host})')

    if args.rollback:
        return rollback(fs_files, args.rollback, args.batch, args.execute)

    undo = open(args.undo, 'a') if args.execute else None
    if args.execute:
        ensure_index(fs_files)

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

    def flush(chunk):
        """Record the inverse, then write. In that order.

        The undo file is the only record of which rows this run touched, so it
        is fsynced before the write rather than after: a run killed mid-batch
        must leave a record that covers more than it did, never less.
        """
        nonlocal written
        for file_id, _ in chunk:
            undo.write(f'{file_id}\n')
        undo.flush()
        os.fsync(undo.fileno())
        try:
            fs_files.bulk_write([operation for _, operation in chunk],
                                ordered=False)
            written += len(chunk)
        except PyMongoError as exc:
            print(f'  bulk_write failed on {len(chunk)} op(s): {exc}')

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
                flush(chunk)
            write_checkpoint(args.checkpoint, document['_id'], documents, written)

        if documents % 25 == 0:
            print(f'  {documents} document(s), {to_write} to label, '
                  f'{correct} already correct, {written} written')

    if args.execute and pending:
        flush(pending)
    if undo is not None:
        undo.close()

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
