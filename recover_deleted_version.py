#!/usr/bin/env python3
"""
Restore a version that was deleted from a project's history.

Usage:
    python recover_deleted_version.py <deleted_version_id> [<current_version_id>]
                                      [--expect-db NAME] [--apply]

``current_version_id`` is optional: when the deleted version carries lineage
pointers, the head of its chain is read from them.  Give it explicitly for a
document the backfill has not reached.

Dry-run by default; ``--apply`` writes.

What can and cannot be recovered
--------------------------------

A deleted version leaves a tombstone: a small document that keeps the version's
identity, its place in the chain and its tool versions, and nothing else.  The
deletion **purges the GridFS payload**, and that is not reversible.  So:

* the version's *position* is always recoverable -- it never left the chain,
  and since the read paths follow the pointers it is already visible in the
  history table, marked as deleted;
* the version's *data* is recoverable only if the purge did not happen.

This script therefore refuses to touch a document whose payload was purged.
Clearing the tombstone markers on such a document would produce a SUPERSEDED
version that resolves by URL, appears in history as a real version, and has no
files behind it -- a project page that renders empty rather than one that says
the version was deleted.  That is strictly worse than the tombstone, and the
earlier version of this script did exactly it: it cleared
``version_deleted_from_history`` and left ``payload_purged`` and
``redirect_to_project`` in place, which also left the document classified as
neither a tombstone nor a healthy version.

What it does when the payload survives
--------------------------------------

1. Clears every tombstone marker, together, so the document classifies
   cleanly rather than landing between two states.
2. Restores the flags: LIVE if the version holds ``is_latest``, SUPERSEDED
   otherwise.
3. Re-adds the version to the head's ``previous_versions[]``, which is what
   the array-based readers still use during the compatibility window.

The lineage pointers are deliberately not touched.  A deletion does not change
them -- the deleted version keeps its ordinal and its neighbours keep pointing
at it -- so there is nothing about the chain's structure to put back.
"""

import argparse
import os
import sys

import django

# ---------------------------------------------------------------------------
# Bootstrap Django so we can reuse the app's DB settings and collection_handle
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'caper'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
django.setup()

from bson import ObjectId                                            # noqa: E402
from caper import lineage                                            # noqa: E402
from caper.project_status import (                                   # noqa: E402
    LIVE,
    SUPERSEDED,
    TOMBSTONE,
    TOMBSTONE_MARKER_FIELDS,
    classify,
    is_tombstone,
    status_flags,
)
from caper.project_version_cleanup import iter_gridfs_file_ids       # noqa: E402
from caper.utils import collection_handle                            # noqa: E402

# Everything build_deleted_version_tombstone() writes to mark a deleted
# version, cleared together: a document carrying some of them is a state
# classify() has no name for, and clearing a subset is how the previous version
# of this script produced one.  The first two come from project_status rather
# than being spelled again here -- they are the predicate, and a third marker
# must not have to be remembered in two places.
TOMBSTONE_MARKERS = TOMBSTONE_MARKER_FIELDS + (
    'redirect_to_project', 'delete_user', 'delete_date')


def fetch(doc_id, label):
    try:
        oid = ObjectId(str(doc_id))
    except Exception:
        print(f"ERROR: {label} id {doc_id!r} is not an ObjectId.")
        sys.exit(1)
    doc = collection_handle.find_one({'_id': oid})
    if doc is None:
        print(f"ERROR: {label} document {doc_id!r} not found in the database.")
        sys.exit(1)
    return doc


def describe(label, doc):
    print(f"\n{label}: {doc['_id']}")
    print(f"  project_name : {doc.get('project_name')}")
    print(f"  date         : {doc.get('date')}")
    print(f"  status       : {classify(doc)}")
    if lineage.has_pointers(doc):
        print(f"  chain        : {doc.get('version_chain_id')} "
              f"ordinal {doc.get('version_ordinal')}"
              f"{'  is_latest' if doc.get('is_latest') else ''}")
    else:
        print("  chain        : no lineage pointers on this document")
    files = list(iter_gridfs_file_ids(doc))
    print(f"  GridFS files : {len(files)}")


def find_head(deleted_doc, current_id):
    """The version to re-add the recovered one to, and how it was found."""
    if current_id:
        return fetch(current_id, 'current_version'), 'named on the command line'

    members = lineage.chain_members(collection_handle, deleted_doc)
    if members is None:
        print("ERROR: this document has no lineage pointers, so the current "
              "version cannot be derived. Pass it as the second argument.")
        sys.exit(1)
    head = lineage.head(members)
    if head is None or head['_id'] == deleted_doc['_id']:
        return None, 'this version is the head of its own chain'
    return head, 'read from the chain'


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('deleted_version_id',
                        help='_id of the version deleted from history')
    parser.add_argument('current_version_id', nargs='?',
                        help='_id of the current version; derived from the '
                             'chain when omitted')
    parser.add_argument('--expect-db', metavar='NAME',
                        help='the database name this run intends to write; '
                             'required for any database not on this machine')
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes (default is dry-run)')
    args = parser.parse_args()

    # Prod and dev share one cluster, and dev's database has the same name as
    # the local docker one, so the name alone identifies nothing -- but a run
    # that names the wrong one should still stop before writing.
    db_name = collection_handle.database.name
    if args.expect_db is not None and db_name != args.expect_db:
        raise SystemExit(f'connected to database {db_name!r}, but --expect-db '
                         f'says {args.expect_db!r}. Check which config.sh is sourced.')

    print(f"\n{'=' * 60}")
    print(f"Database        : {db_name}")
    print(f"Deleted version : {args.deleted_version_id}")
    print(f"Mode            : {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"{'=' * 60}")

    deleted_doc = fetch(args.deleted_version_id, 'deleted_version')
    describe('Deleted version', deleted_doc)

    # ------------------------------------------------------------------
    # Refuse the cases nothing can honestly put back
    # ------------------------------------------------------------------
    if deleted_doc.get('payload_purged') is True:
        print("\nREFUSING: this version's payload was purged when it was "
              "deleted. The GridFS files are gone and this script cannot "
              "bring them back.\n"
              "Clearing the markers would leave a version that resolves by "
              "URL, appears in history as a real version, and has no data "
              "behind it. The tombstone is the truthful record: the history "
              "table already shows this version, marked as deleted.")
        sys.exit(2)

    if not is_tombstone(deleted_doc) and classify(deleted_doc) != TOMBSTONE:
        markers = [key for key in TOMBSTONE_MARKER_FIELDS if key in deleted_doc]
        if not markers:
            print(f"\nNOTHING TO DO: this version is {classify(deleted_doc)}, "
                  f"not a deleted one.")
            sys.exit(0)
        print(f"\nPartially deleted: carries {', '.join(markers)} but does not "
              f"classify as a tombstone. Recovering it will clear all of them.")

    head, how = find_head(deleted_doc, args.current_version_id)
    if head is not None:
        describe(f'Current version ({how})', head)
    else:
        print(f"\nCurrent version : none -- {how}")

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    restored_status = LIVE if deleted_doc.get('is_latest') is True else SUPERSEDED
    print(f"\nWill clear : {', '.join(k for k in TOMBSTONE_MARKERS if k in deleted_doc)}")
    print(f"Will set   : status {restored_status} "
          f"({', '.join(f'{k}={v!r}' for k, v in status_flags(restored_status).items())})")

    updated_prev = None
    if head is not None:
        existing = head.get('previous_versions', []) or []
        already = any(str(lineage.resolve_id(
            entry.get('linkid') if isinstance(entry, dict) else entry))
            == str(deleted_doc['_id']) for entry in existing)
        if already:
            print(f"Array      : {head['_id']} already names this version")
        else:
            entry = {'date': str(deleted_doc.get('date', '1999-01-01T00:00:00.000000')),
                     'linkid': str(deleted_doc['_id'])}
            for field in ('ASP_version', 'AA_version', 'AC_version',
                          'aggregator_version'):
                entry[field] = deleted_doc.get(field, 'NA')
            updated_prev = sorted(existing + [entry],
                                  key=lambda pv: (pv.get('date', '')
                                                  if isinstance(pv, dict) else ''))
            print(f"Array      : re-adding to {head['_id']}, "
                  f"{len(existing)} -> {len(updated_prev)} entries")

    if not args.apply:
        print("\nDRY-RUN complete. Re-run with --apply to write these changes.")
        return

    collection_handle.update_one(
        {'_id': deleted_doc['_id']},
        {'$set': {**status_flags(restored_status)},
         '$unset': {key: '' for key in TOMBSTONE_MARKERS if key in deleted_doc}})

    if updated_prev is not None:
        collection_handle.update_one({'_id': head['_id']},
                                     {'$set': {'previous_versions': updated_prev}})

    after = collection_handle.find_one({'_id': deleted_doc['_id']})
    print(f"\nDone. {deleted_doc['_id']} now classifies as {classify(after)}.")
    if head is not None:
        print(f"Verify by visiting: /project/{head['_id']}")


if __name__ == '__main__':
    main()
