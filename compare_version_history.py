#!/usr/bin/env python
"""
Diff the two version-history readers over every project in a database.

Phase 2 moves the project page, the API and the redirects off the denormalised
``previous_versions[]`` array and onto the lineage pointers.  Invariant I11 says
the two agree about *which* documents are in a chain.  This says whether they
agree about the table a visitor actually sees -- every row, every column -- for
every project in a real database.

Nothing here writes to the database, and nothing here decides.

One caveat, because "read-only" has to be true rather than nearly true: this
imports the application, so ``django.setup()`` runs ``CaperConfig.ready()``,
which ensures the Mongo indexes and -- when ``S3_STATIC_FILES=TRUE`` -- starts a
background ``aws s3 sync`` of the static directory.  That is a write, to a
bucket every deployment shares.  Run it with a source tree that has no
``caper/static`` (the sync then finds nothing to copy and logs an error), or
with ``S3_STATIC_FILES`` unset.  The same is true of the test suite and of any
management command; it is not specific to this script, but this script is the
one people will point at production.

Each disagreement is printed with
the project it belongs to, because a difference is not automatically a fault:
the pointer reader takes each row from the version's own document, while the
array reader takes it from a copy stored in the head's array and falls back to
the document only where the copy is absent or ``'NA'``.  Where the copy went
stale, the two differ and the pointer reader is the correct one.  That is worth
seeing rather than asserting.

Usage::

    set -a; source caper/config.sh; set +a
    python compare_version_history.py                       # local
    python compare_version_history.py --expect-db caper     # prod, read-only

``--expect-db`` is required for any database not on this machine: dev's database
and the local docker mongo are both called ``caper-dev``, so the name alone
identifies nothing.
"""

import argparse
import os
import sys

_REPO_ROOT = os.environ.get('VALIDATOR_REPO_ROOT') or \
    os.path.dirname(os.path.abspath(__file__))
if os.path.join(_REPO_ROOT, 'caper') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'caper'))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')

import django                                                      # noqa: E402
django.setup()

from caper import utils                                            # noqa: E402
from caper.project_status import classify                          # noqa: E402

# Everything either reader touches, minus the three fields that make a document
# large.  A project document averages 690 KB on production and this walks all of
# them.
_HEAVY_FIELDS = ('runs', 'aggregate_df', 'sample_data')

# Columns compared row by row.  'linkid' is the key rather than a column.
_COLUMNS = ('date',) + tuple(utils.VERSION_HISTORY_FIELDS) + \
    tuple(utils.DELETED_VERSION_HISTORY_FIELDS)


def _rows_by_linkid(entries):
    return {str(entry.get('linkid')): entry for entry in entries}


def _compare(old_entries, new_entries):
    """(missing, added, changed) between the two history tables."""
    old_rows = _rows_by_linkid(old_entries)
    new_rows = _rows_by_linkid(new_entries)

    missing = sorted(set(old_rows) - set(new_rows))
    added = sorted(set(new_rows) - set(old_rows))

    changed = []
    for linkid in sorted(set(old_rows) & set(new_rows)):
        for column in _COLUMNS:
            was, now = old_rows[linkid].get(column), new_rows[linkid].get(column)
            if was != now:
                changed.append((linkid, column, was, now))
    return missing, added, changed


def _order(entries):
    return [str(entry.get('linkid')) for entry in entries]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Diff the array and pointer version-history readers. Read-only.')
    parser.add_argument('--expect-db', metavar='NAME',
                        help='the database name this run intends to read; '
                             'required for any database not on this machine')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='stop after N projects')
    parser.add_argument('--verbose', action='store_true',
                        help='print every differing row, not the first 40')
    args = parser.parse_args(argv)

    collection = utils.collection_handle
    db_name = collection.database.name
    if args.expect_db is not None and db_name != args.expect_db:
        raise SystemExit(f'connected to database {db_name!r}, but --expect-db '
                         f'says {args.expect_db!r}. Check which config.sh is sourced.')
    print(f'target: {db_name}')

    documents = collection.find({}, {field: 0 for field in _HEAVY_FIELDS})

    agree = differ = unpointered = 0
    reordered = 0
    findings = []
    for count, doc in enumerate(documents, start=1):
        if args.limit and count > args.limit:
            break

        from_pointers = utils._previous_versions_from_pointers(doc)
        if from_pointers is None:
            unpointered += 1
            continue
        new_entries, new_msg = from_pointers
        old_entries, old_msg = utils._previous_versions_from_array(doc)

        missing, added, changed = _compare(old_entries, new_entries)
        same_order = _order(old_entries) == _order(new_entries)
        if not same_order:
            reordered += 1

        if not (missing or added or changed) and same_order and old_msg == new_msg:
            agree += 1
            continue

        differ += 1
        findings.append((doc, classify(doc), missing, added, changed,
                         same_order, old_msg, new_msg))

    print(f'\n{agree + differ + unpointered} project(s) read')
    print(f'  identical history table:      {agree}')
    print(f'  differing history table:      {differ}')
    print(f'  no pointers, array fallback:  {unpointered}')
    print(f'  same rows in a different order: {reordered}')

    shown = findings if args.verbose else findings[:40]
    for doc, status, missing, added, changed, same_order, old_msg, new_msg in shown:
        print(f'\n{doc["_id"]}  {str(doc.get("project_name"))[:44]}  [{status}]')
        for linkid in missing:
            print(f'    only the array reader has row {linkid}')
        for linkid in added:
            print(f'    only the pointer reader has row {linkid}')
        for linkid, column, was, now in changed[:12]:
            print(f'    {linkid} {column}: array={was!r} pointers={now!r}')
        if len(changed) > 12:
            print(f'    ... and {len(changed) - 12} more differing column(s)')
        if not same_order:
            print('    the two readers order the rows differently')
        if old_msg != new_msg:
            print(f'    banner: array={old_msg!r}')
            print(f'            pointers={new_msg!r}')
    if len(shown) < len(findings):
        print(f'\n... and {len(findings) - len(shown)} more project(s) (pass --verbose)')

    return 1 if differ else 0


if __name__ == '__main__':
    sys.exit(main())
