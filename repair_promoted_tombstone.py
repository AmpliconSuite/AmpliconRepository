#!/usr/bin/env python
"""Move the head back off a tombstone that was wrongly promoted.

The population is exactly invariant I20's: a chain whose single ``is_latest``
member is a TOMBSTONE while versions nobody deleted sit beside it.  A project
in that state has, as its current version, one the user deleted -- its page
renders empty, its surviving version is unreachable as the head, and a new
upload attaches after a version that no longer has a payload.

**How it was made.**  ``plan_deletion()`` asked ``is_tombstone()`` of chain
members fetched under a projection that dropped both tombstone markers, so
every tombstone read as a surviving version and deleting a head promoted the
most recent one instead of the newest survivor.  Fixed on 2026-08-28; this
repairs what it wrote before it was.  0 such chains on prod -- the code that
produced them never ran there -- and 1 on caper-dev.

**What it does**, per chain:

  * ``is_latest`` moves from the tombstone to the highest-ordinal member that
    is not a tombstone, which is what promotion should have chosen.
  * That member gets LIVE's flags, because being the head of a chain is what
    LIVE means, and it lost them when the deletion passed it over.
  * Every tombstone in the chain has its ``redirect_to_project`` retargeted at
    the promoted version, through the routine the deletion paths already use.
  * Stored ``status`` is rewritten from ``classify()`` on every member touched.

**What it does not do.**  No pointer is rewritten and no ordinal is renumbered:
those were never wrong.  The bug moved position, so the repair moves position
back.  Nothing is deleted and no payload is touched.

Report-only unless ``--execute``.  Every write is recorded as its own inverse.
"""

import argparse
import datetime
import json
import os
import sys

from bson.objectid import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.project_status import (          # noqa: E402
    LIVE, STATUS_FLAG_FIELDS, TOMBSTONE, TOMBSTONE_MARKER_FIELDS,
    classify, is_tombstone, status_flags,
)
from caper.project_version_cleanup import (  # noqa: E402
    retarget_deleted_version_tombstones,
)

# Named from project_status rather than spelled here, for the reason the whole
# module exists: a projection that drops a field a predicate reads makes the
# predicate silently wrong, which is the defect this script repairs.
PROJECTION = {field: 1 for field in
              ('project_name', 'version_chain_id', 'version_ordinal',
               'is_latest', 'status', 'redirect_to_project')
              + STATUS_FLAG_FIELDS + TOMBSTONE_MARKER_FIELDS}


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    database = MongoClient(uri)[db_name]
    return database['projects']


def record(rollback, doc_id, fields):
    if rollback is None:
        return
    rollback.write(json.dumps(
        {'_id': str(doc_id), 'op': '$set', 'fields': fields},
        default=str) + '\n')
    rollback.flush()


def _ordinal(doc):
    value = doc.get('version_ordinal')
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def find_affected(projects):
    """(chain_id, members, tombstone_head, promote_to) for every I20 chain."""
    chains = {}
    for doc in projects.find({'version_chain_id': {'$ne': None}}, PROJECTION):
        chains.setdefault(doc['version_chain_id'], []).append(doc)

    affected = []
    for chain_id, members in sorted(chains.items(), key=lambda kv: str(kv[0])):
        members.sort(key=_ordinal)
        heads = [doc for doc in members if doc.get('is_latest') is True]
        if len(heads) != 1 or not is_tombstone(heads[0]):
            continue
        survivors = [doc for doc in members if not is_tombstone(doc)]
        if not survivors:
            continue                      # an emptied project, which is correct
        affected.append((chain_id, members, heads[0], survivors[-1]))
    return affected


def repair(projects, chain_id, members, tombstone_head, promote_to,
           execute, rollback):
    """Return the list of (id, fields) writes this chain needs."""
    writes = []

    # The tombstone gives up the head. Its status is rewritten too: the
    # promotion stamped LIVE's flags on it on the way past.
    writes.append((tombstone_head['_id'],
                   {'is_latest': False, **status_flags(TOMBSTONE)}))

    # The newest surviving version becomes the head, which is what promotion
    # should have done.
    promoted_fields = {'is_latest': True, **status_flags(LIVE)}
    writes.append((promote_to['_id'], promoted_fields))

    if not execute:
        return writes

    for doc_id, fields in writes:
        current = projects.find_one({'_id': doc_id}, PROJECTION) or {}
        record(rollback, doc_id,
               {key: current.get(key) for key in fields})
        projects.update_one({'_id': doc_id}, {'$set': fields})

    # Every tombstone in the chain redirected at the version that was wrongly
    # promoted; they follow the head. Recorded first, since the routine does
    # its own update_many.
    for member in members:
        if is_tombstone(member) and 'redirect_to_project' in member:
            record(rollback, member['_id'],
                   {'redirect_to_project': member.get('redirect_to_project')})
    retarget_deleted_version_tombstones(
        projects, str(tombstone_head['_id']), str(promote_to['_id']))
    return writes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True,
                        help='the database name this run must find, asserted '
                             'before anything is read')
    parser.add_argument('--expect-host', choices=['local', 'docdb'],
                        required=True,
                        help='local or docdb; prod and dev share one cluster, '
                             'so the database name alone identifies nothing')
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--rollback-file',
                        help='where to record the inverse of every write '
                             '(default: repair-promoted-tombstone-undo.jsonl)')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    projects = connect(db_name, args.expect_host)
    print(f'target: {db_name} ({args.expect_host})')

    affected = find_affected(projects)
    if not affected:
        print('\nno chain is headed by a tombstone while a version survives it.')
        return 0

    print(f'\n{len(affected)} chain(s) to repair:\n')
    for chain_id, members, tombstone_head, promote_to in affected:
        print(f'  chain {chain_id}  ({members[0].get("project_name")})')
        for doc in members:
            marks = []
            if is_tombstone(doc):
                marks.append('tombstone')
            if doc['_id'] == tombstone_head['_id']:
                marks.append('HEAD -> gives it up')
            if doc['_id'] == promote_to['_id']:
                marks.append('takes the head')
            stored, real = doc.get('status'), classify(doc)
            if stored != real:
                marks.append(f'stored {stored!r} but classifies {real}')
            print(f'    ord {_ordinal(doc)}  {doc["_id"]}  '
                  f'{", ".join(marks) or "-"}')
        print()

    if not args.execute:
        print('report only. Re-run with --execute to write.')
        return 0

    path = args.rollback_file or 'repair-promoted-tombstone-undo.jsonl'
    if os.path.exists(path):
        sys.exit(f'{path} exists -- it is another run\'s undo record. '
                 f'Move it aside or pass --rollback-file.')
    written = 0
    with open(path, 'w') as rollback:
        rollback.write(json.dumps({
            'run_at': datetime.datetime.now().isoformat(),
            'database': db_name, 'host': args.expect_host}) + '\n')
        for chain_id, members, tombstone_head, promote_to in affected:
            written += len(repair(projects, chain_id, members, tombstone_head,
                                  promote_to, True, rollback))
    print(f'{written} document(s) updated across {len(affected)} chain(s).')
    print(f'undo record: {path} '
          f'({sum(1 for _ in open(path))} line(s)) -- move it somewhere durable.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
