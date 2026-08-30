#!/usr/bin/env python
"""Clear the view counts that were carried forward, so the chain can be summed.

Until 2026-08-30 a new version was seeded with its predecessor's ``views``, so
the head of a chain already contains what every older version had earned at the
moment it was superseded. From that date versions start at zero and the
project's number is the sum across the chain -- but summing the *existing*
values would count the carried-forward history once per version it passed
through. On prod that is 129,382 against a true figure near the head's 42,158.

So this zeroes ``views`` on every **non-head** member of a multi-version chain.
Their contribution is not lost: it is already inside the head's number, which
this does not touch. What is lost is the per-version breakdown of views before
this date, which was never meaningful -- each of those numbers is its own views
plus an unknown inherited amount.

This is the one-time reconciliation that "history cannot be reconstructed"
means in practice. After it, every version's ``views`` is a count it earned
itself, and the sum is the project's.

**It must run exactly once against a database, and it enforces that itself.**
Once it has run, an older version holding a view count is no longer carried-
forward residue -- it is a view that version genuinely earned under the new
rule, and zeroing it again would delete a real count. The first dev run showed
this within seconds: 73 documents zeroed, and the re-plan immediately found one
more, which was a live page view that had landed in between. So a marker is
written on success and ``--execute`` refuses to run twice without ``--i-know``.

Report-only unless ``--execute``. Single-version chains are never touched, and
neither is any head.
"""

import argparse
import datetime
import json
import os
import sys

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.download_totals import as_int                     # noqa: E402

PROJECTION = {'project_name': 1, 'version_chain_id': 1, 'version_ordinal': 1,
              'is_latest': 1, 'views': 1}

#: Where the "this has already run" marker lives. A collection rather than a
#: flag on some existing document, so that nothing that rewrites project
#: documents can clear it by accident.
MIGRATIONS = 'schema_migrations'
MARKER = 'views_carry_forward_reconciled'


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri)[db_name]


def plan(projects):
    """(to_zero, head_total, would_be_sum, chains_touched)."""
    chains = {}
    for doc in projects.find({'version_chain_id': {'$ne': None}}, PROJECTION):
        chains.setdefault(str(doc['version_chain_id']), []).append(doc)

    to_zero, head_total, would_be_sum, touched = [], 0, 0, 0
    for _chain_id, members in sorted(chains.items()):
        if len(members) < 2:
            head_total += as_int(members[0].get('views')) if members else 0
            would_be_sum += as_int(members[0].get('views')) if members else 0
            continue
        heads = [d for d in members if d.get('is_latest') is True]
        # A chain without exactly one head is I3's finding, not this script's.
        # Skipping it is the safe choice: guessing which member is the head is
        # how a counter gets zeroed on the document everybody reads.
        if len(heads) != 1:
            continue
        touched += 1
        head_total += as_int(heads[0].get('views'))
        would_be_sum += sum(as_int(d.get('views')) for d in members)
        for doc in members:
            if doc['_id'] != heads[0]['_id'] and as_int(doc.get('views')):
                to_zero.append(doc)
    return to_zero, head_total, would_be_sum, touched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='zero at most N documents, for staging a first run')
    parser.add_argument('--undo-file', metavar='PATH',
                        help='where to write the inverse of every write')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--i-know', action='store_true',
                        help='run again on a database that has already been '
                             'reconciled. Every count this zeroes on a second '
                             'run is a real view, not residue.')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    projects = database['projects']
    print(f'target: {db_name} ({args.expect_host})')

    marker = database[MIGRATIONS].find_one({'_id': MARKER})
    if marker:
        print(f'\n  ALREADY RECONCILED on {marker.get("ran_at")}, '
              f'{marker.get("documents_zeroed")} document(s) zeroed.')
        print('  Any count below is a view a version earned since, not '
              'carried-forward residue.')

    to_zero, head_total, would_be_sum, touched = plan(projects)
    print(f'\n  multi-version chains with one head : {touched}')
    print(f'  documents to zero                  : {len(to_zero)}')
    print(f'\n  what the site shows today (heads)  : {head_total:,}')
    print(f'  summing as-is would show           : {would_be_sum:,}'
          '   <- counts carried-forward history again')
    print(f'  summing after this runs will show  : {head_total:,}'
          '   <- unchanged, which is the point')

    if args.verbose:
        for doc in to_zero:
            print(f'    {doc["_id"]}  v{doc.get("version_ordinal")}  '
                  f'views={as_int(doc.get("views")):,}  '
                  f'{str(doc.get("project_name"))[:40]}')

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
        return 0

    if marker and not args.i_know:
        sys.exit('\nrefusing: this database was already reconciled on '
                 f'{marker.get("ran_at")}. The counts above are views earned '
                 'since then, and zeroing them would delete real data. Pass '
                 '--i-know only if you are certain otherwise.')

    work = to_zero
    if args.limit is not None:
        work = work[:args.limit]
        print(f'\n--limit {args.limit}: writing {len(work)} of {len(to_zero)}')

    if args.undo_file:
        with open(args.undo_file, 'w') as handle:
            json.dump([{'_id': str(d['_id']),
                        'restore': {'views': as_int(d.get('views'))}}
                       for d in work], handle, indent=2)
        print(f'undo record: {args.undo_file}')

    written = 0
    for doc in work:
        # The filter carries the value the plan saw. A document whose count
        # moved between the plan and the write does not match, and is left for
        # the next run rather than being silently overwritten.
        written += projects.update_one(
            {'_id': doc['_id'], 'views': doc.get('views')},
            {'$set': {'views': 0}}).modified_count
    print(f'\n{written} document(s) zeroed.')

    database[MIGRATIONS].update_one(
        {'_id': MARKER},
        {'$set': {'ran_at': datetime.datetime.now(datetime.timezone.utc),
                  'documents_zeroed': written, 'database': db_name}},
        upsert=True)

    to_zero, head_total, would_be_sum, touched = plan(projects)
    print(f'after: {len(to_zero)} document(s) hold a non-head count; summing '
          f'now shows {would_be_sum:,} against heads {head_total:,}')
    if to_zero:
        print('  (views earned while this ran. They are real and stay.)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
