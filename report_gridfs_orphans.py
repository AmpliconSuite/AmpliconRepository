#!/usr/bin/env python
"""Account for every GridFS file. **Read-only. This deletes nothing, ever.**

Every ``fs.files`` row lands in exactly one bucket, and the buckets sum to the
collection count. What is owned is computed by walking the project documents,
not by reading the backlinks, so this reports the same numbers before and after
``backfill_gridfs_backlinks.py`` has run -- and where both exist it says whether
they agree.

The buckets, and what each means for deletion -- **none of which this script
acts on**:

  owned-by-a-live-document  a retained document names it. Never.
  owned-by-a-tombstone      a TOMBSTONE names it: deletable, **and a bug**,
                            because a tombstone holding files means a deletion
                            path did not finish.
  residue/document-gone     backlink names a document that is gone. Deletable.
  residue/unreferenced      backlink names a document that no longer references
                            it -- residue of a version edit. Deletable once that
                            document's status is confirmed.
  residue/unlabelled        no backlink. Before the backfill has run this is
                            every residue row and means nothing beyond "not yet
                            asked"; after it, genuinely stranded.

Usage:

    report_gridfs_orphans.py --expect-db caper --expect-host docdb
"""

import argparse
import os
import sys

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.gridfs_ownership import (                  # noqa: E402
    ORDER, RESIDUE_DOCUMENT_GONE, RESIDUE_UNLABELLED, RESIDUE_UNREFERENCED,
    human, survey,
)


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri)[db_name]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--no-bytes', action='store_true',
                        help='skip the size aggregation over fs.files')
    parser.add_argument('--top', type=int, default=15, metavar='N',
                        help='projects to list, largest first (default 15)')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    print(f'target: {db_name} ({args.expect_host})   READ-ONLY')

    def progress(documents, files):
        print(f'  {documents} document(s), {files:,} owned file(s) so far')

    result = survey(database['projects'], database['fs.files'],
                    with_bytes=not args.no_bytes, progress=progress)

    total = result['total_files']
    print(f"\n{result['documents']} project document(s); fs.files has "
          f"{total:,} row(s), {human(result['total_bytes'])}")
    print()
    for label in ORDER:
        count = result['counts'][label]
        share = (count / total * 100) if total else 0
        print(f"  {label:26} {count:>10,}  {share:>5.1f}%  "
              f"{human(result['bytes'][label]):>12}")
    print(f"  {'':26} {'':>10}          {'':>12}")
    print(f"  {'residue, all causes':26} {result['residue']:>10,}  "
          f"{(result['residue'] / total * 100) if total else 0:>5.1f}%  "
          f"{human(result['residue_bytes']):>12}")

    labelled = result['labelled_rows']
    print(f"\nbacklinks: {labelled:,} of {total:,} row(s) carry one "
          f"({(labelled / total * 100) if total else 0:.1f}%)")
    if result['counts'][RESIDUE_UNLABELLED] and not labelled:
        print('  none written yet, so the whole residue sits in '
              f'{RESIDUE_UNLABELLED}. That is the question not yet asked, not '
              'a count of orphans: run backfill_gridfs_backlinks.py to split '
              f'it into {RESIDUE_DOCUMENT_GONE} and {RESIDUE_UNREFERENCED}.')
    else:
        print(f"  of the owned rows: {result['backlink_agrees']:,} agree, "
              f"{result['backlink_disagrees']:,} name a different document, "
              f"{result['backlink_missing']:,} carry none")
        if result['backlink_disagrees'] and not result['shared_rows']:
            print('  a disagreement with nothing shared means the metadata is '
                  'wrong, never the document: rebuild it with '
                  'backfill_gridfs_backlinks.py --repair.')

    if result['shared_rows']:
        print(f"\n{result['shared_rows']:,} row(s) are named by more than one "
              f"document. A backlink records one owner, so for these it records "
              f"the first document seen and the rest are counted once, under "
              f"whichever owner keeps them undeletable. Deletion must keep "
              f"reading the documents, which is what it does.")

    if result['named_absent']:
        print(f"\n{result['named_absent']:,} id(s) named by a document have no "
              f"fs.files row. That is I12's finding -- documents pointing at "
              f"storage that is gone -- not a property of the files.")

    if result['tombstones_holding']:
        rows = result['tombstones_holding']
        held = sum(row['present'] for row in rows)
        print(f"\n{len(rows)} TOMBSTONE document(s) still holding {held:,} "
              f"file(s). A tombstone's payload was supposed to be purged, so "
              f"each of these is a deletion that did not finish:")
        for row in rows[:20]:
            print(f"  {row['project_id']}  {str(row['project_name'])[:36]:36} "
                  f"{row['present']:>8,} file(s)  {human(row['bytes']):>10}")

    if args.top:
        print(f"\nlargest {args.top} project(s) by owned bytes:")
        for row in result['per_project'][:args.top]:
            flag = 'TOMBSTONE' if row['tombstone'] else ''
            print(f"  {str(row['project_name'])[:40]:40} "
                  f"{row['present']:>8,} file(s)  {human(row['bytes']):>10}  {flag}")

    print('\nNothing was deleted. This report only counts.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
