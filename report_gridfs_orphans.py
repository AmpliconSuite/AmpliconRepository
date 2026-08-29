#!/usr/bin/env python
"""Classify every GridFS file. **Read-only. This deletes nothing, ever.**

The point of the backlinks is that "is this file orphaned?" stops requiring a
correct traversal of the whole database. That traversal has now been wrong
twice: once classifying 84 of 345 production documents as orphaned when 77 still
held a payload, and once making 80,170 live files look like garbage. This report
asks the question per file instead, using the backlink each file carries.

The categories, and what each means for deletion -- **none of which this script
acts on**:

  live                        the named document still references it. Never.
  unreferenced-by-its-document  residue of a version edit. Deletable once that
                              document's status is confirmed.
  document-gone               residue of a purge. Deletable.
  tombstone-payload           a tombstone still holding files: deletable, **and
                              a bug**, because a deletion path did not finish.
  unlabelled                  no backlink. Before the backfill has run this
                              means nothing at all; after it, genuinely
                              stranded.

**``unlabelled`` is not a synonym for orphaned** and must never be treated as
one. The report prints how much of the collection is labelled precisely so that
a reader can see whether the backfill has actually finished before drawing any
conclusion from that row.

Grouped by project rather than file: one pass over the documents and one query
per project, instead of a lookup per file.
"""

import argparse
import os
import sys
from collections import Counter

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.gridfs_backlinks import (                    # noqa: E402
    DOCUMENT_GONE, LIVE_FILE, METADATA_FIELD, PROJECT_ID, TOMBSTONE_PAYLOAD,
    UNLABELLED, UNREFERENCED_BY_ITS_DOCUMENT, iter_backlinks,
)
from caper.project_status import TOMBSTONE, classify    # noqa: E402

ORDER = (LIVE_FILE, UNREFERENCED_BY_ITS_DOCUMENT, TOMBSTONE_PAYLOAD,
         DOCUMENT_GONE, UNLABELLED)


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri)[db_name]


def report(database, verbose=False):
    projects = database['projects']
    fs_files = database['fs.files']

    counts = Counter()
    bytes_by = Counter()
    tombstones_holding = []

    seen_project_ids = set()
    for document in projects.find({}):
        doc_id = document['_id']
        seen_project_ids.add(doc_id)
        named = {file_id for file_id, _, _ in iter_backlinks(document)}
        is_tombstone = classify(document) == TOMBSTONE

        rows = list(fs_files.find({f'{METADATA_FIELD}.{PROJECT_ID}': doc_id},
                                  {'length': 1}))
        for row in rows:
            if is_tombstone:
                label = TOMBSTONE_PAYLOAD
            elif row['_id'] in named:
                label = LIVE_FILE
            else:
                label = UNREFERENCED_BY_ITS_DOCUMENT
            counts[label] += 1
            bytes_by[label] += row.get('length') or 0

        if is_tombstone and rows:
            tombstones_holding.append((doc_id, document.get('project_name'),
                                       len(rows)))

    # Files whose backlink names a document that no longer exists.
    labelled_ids = fs_files.distinct(f'{METADATA_FIELD}.{PROJECT_ID}')
    for missing_id in (set(labelled_ids) - seen_project_ids):
        for row in fs_files.find({f'{METADATA_FIELD}.{PROJECT_ID}': missing_id},
                                 {'length': 1}):
            counts[DOCUMENT_GONE] += 1
            bytes_by[DOCUMENT_GONE] += row.get('length') or 0

    total = fs_files.estimated_document_count()
    labelled = sum(counts.values())
    counts[UNLABELLED] = max(total - labelled, 0)

    return counts, bytes_by, tombstones_holding, total, labelled


def human(num_bytes):
    value = float(num_bytes)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    print(f'target: {db_name} ({args.expect_host})   READ-ONLY')

    counts, bytes_by, tombstones, total, labelled = report(database, args.verbose)

    print(f'\nfs.files: {total:,} row(s), {labelled:,} carrying a backlink '
          f'({(labelled / total * 100) if total else 0:.1f}%)')
    if labelled < total:
        print('  the backfill has not finished here, so "unlabelled" below is '
              'not a count of orphans -- it is a count of rows not yet asked.')

    print()
    for label in ORDER:
        print(f'  {label:30} {counts.get(label, 0):>10,}   '
              f'{human(bytes_by.get(label, 0)):>12}')

    if tombstones:
        print(f'\n{len(tombstones)} TOMBSTONE document(s) still holding files. '
              f'A tombstone\'s payload was supposed to be purged, so each of '
              f'these is a deletion that did not finish:')
        for doc_id, name, count in tombstones[:20]:
            print(f'  {doc_id}  {str(name)[:40]:40} {count:>8,} file(s)')

    print('\nNothing was deleted. This report only labels.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
