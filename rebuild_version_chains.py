#!/usr/bin/env python
"""Rebuild the derived ``project_version_chains`` view from the project documents.

The view is a materialised copy of lineage that already lives on the documents.
It exists so a chain is an addressable thing rather than something every reader
re-walks, and so there is a second copy to check the documents against.

**Documents are the authority.** This command is the only writer. Feature code
reads the view and never writes it, and a test enforces that. On disagreement
the view is wrong by definition and is rebuilt -- which is why a stale view is a
recoverable condition rather than a corruption.

Two fields are authoritative on the chain document because they belong to the
project rather than to any version of it, and must outlive the deletion of every
version: ``canonical_name`` and ``retired``. This seeds them when a chain is
first inserted and never overwrites them afterwards.

Report-only unless ``--execute``. The report says how many chains would be
created, how many are stale and how many already agree, so a run that expects to
change nothing can be checked before it is allowed to.
"""

import argparse
import os
import sys

from pymongo import ASCENDING, MongoClient, ReadPreference

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.version_chains import (             # noqa: E402
    AUTHORITATIVE_FIELDS, COLLECTION, build_chain_document,
    default_canonical_name, group_into_chains, is_empty_chain, source_digest,
)

# Every field the derivation reads. Named rather than fetching whole documents:
# a project document can be very large, and this walks all of them.
PROJECTION = {field: 1 for field in (
    'project_name', 'version_chain_id', 'version_ordinal', 'is_latest',
    'previous_version_id', 'next_version_id', 'date',
    'delete', 'current', 'status',
    'version_deleted_from_history', 'payload_purged',
)}


def connect(db_name, expect_host):
    """Open the database, pinned to PRIMARY.

    The cluster's URI asks for ``secondaryPreferred``, so an unpinned client
    builds its plan from a replica. That is fatal for this command in a way it
    is not for a reporting one: the digest it writes is derived from whatever
    the documents said when it read them, so a lagging replica produces a view
    that is confidently wrong and reports success. The case that matters is
    exactly the one this exists for -- a document edited by hand moments before
    the rebuild.
    """
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri, read_preference=ReadPreference.PRIMARY)[db_name]


def plan(projects, chain_view):
    """(to_create, stale, current, empty) without writing anything."""
    documents = list(projects.find({'version_chain_id': {'$ne': None}}, PROJECTION))
    chains = group_into_chains(documents)

    stored = {doc['_id']: doc for doc in
              chain_view.find({}, {'source_digest': 1})}

    to_create, stale, agreeing, empty = [], [], [], []
    for chain_id, members in sorted(chains.items(), key=lambda kv: str(kv[0])):
        digest = source_digest(members)
        if is_empty_chain(members):
            empty.append(chain_id)
        existing = stored.get(chain_id)
        if existing is None:
            to_create.append((chain_id, members))
        elif existing.get('source_digest') != digest:
            stale.append((chain_id, members))
        else:
            agreeing.append((chain_id, members))

    # Chain documents whose chain no longer exists in the projects collection.
    orphaned = [chain_id for chain_id in stored if chain_id not in chains]
    return to_create, stale, agreeing, empty, orphaned


def rebuild(chain_view, chain_id, members):
    """Write one chain document. Derived fields replaced, authoritative kept."""
    derived = build_chain_document(chain_id, members)
    update = {'$set': {key: value for key, value in derived.items() if key != '_id'}}

    seed = {'canonical_name': default_canonical_name(members), 'retired': False}
    update['$setOnInsert'] = seed

    chain_view.update_one({'_id': chain_id}, update, upsert=True)


def ensure_indexes(chain_view):
    """The one index this collection needs: find a chain by its head document."""
    chain_view.create_index([('head_project_id', ASCENDING)],
                            name='idx_chain_head_project')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True,
                        help='the database name this run must find, asserted '
                             'before anything is read')
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True,
                        help='local or docdb; prod and dev share one cluster, '
                             'so the database name alone identifies nothing')
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='write at most N chains, for staging a first run')
    parser.add_argument('--verbose', action='store_true',
                        help='list every chain, not just the counts')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    projects = database['projects']
    chain_view = database[COLLECTION]
    print(f'target: {db_name} ({args.expect_host})')

    to_create, stale, agreeing, empty, orphaned = plan(projects, chain_view)

    print(f'\n{len(to_create) + len(stale) + len(agreeing)} chain(s) derived from '
          f'{projects.count_documents({"version_chain_id": {"$ne": None}})} '
          f'document(s) that name one')
    print(f'  to create : {len(to_create)}')
    print(f'  stale     : {len(stale)}')
    print(f'  agreeing  : {len(agreeing)}')
    print(f'  empty     : {len(empty)}   (every member a tombstone; derived, not stored)')
    if orphaned:
        print(f'  orphaned  : {len(orphaned)}   chain document(s) whose chain has '
              f'no documents left -- not removed by this command')

    if args.verbose:
        for label, group in (('create', to_create), ('stale', stale)):
            for chain_id, members in group:
                print(f'    {label:6} {chain_id}  {len(members)} member(s)  '
                      f'{default_canonical_name(members)}')

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
        return 0

    work = to_create + stale
    if args.limit is not None:
        work = work[:args.limit]
        print(f'\n--limit {args.limit}: writing {len(work)} of '
              f'{len(to_create) + len(stale)}')

    ensure_indexes(chain_view)
    for chain_id, members in work:
        rebuild(chain_view, chain_id, members)
    print(f'\n{len(work)} chain document(s) written.')

    # Re-plan rather than trusting the arithmetic: the point of the digest is
    # that agreement is checked, not assumed.
    to_create, stale, agreeing, empty, orphaned = plan(projects, chain_view)
    print(f'after: {len(agreeing)} agreeing, {len(stale)} stale, '
          f'{len(to_create)} missing')
    return 0


if __name__ == '__main__':
    sys.exit(main())
