#!/usr/bin/env python
"""Stamp ``version_chain_id`` on audit events that were written without one.

An audit event names a project document. That is enough only while the document
exists: once a version is permanently deleted, an event holding just its id can
no longer be tied to the project it was part of. The chain id fixes that, which
is why the event schema calls for it -- and every event written before
2026-08-30 predates it. On prod that is all 121 of them.

**This is a join, not an inference.** The chain id is read off the project the
event already names; nothing is guessed and nothing new is asserted. Events
whose project no longer exists get nothing, because for those the answer is
genuinely unknown -- on prod, 8 of the 121.

Idempotent by construction: only events *missing* the field are considered, and
the value written is whatever the projects collection says right now. Running it
twice does nothing the second time, so it needs no marker and no override.

Report-only unless ``--execute``.
"""

import argparse
import json
import os
import sys

from pymongo import MongoClient
from pymongo.read_preferences import ReadPreference

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.lineage import resolve_id                          # noqa: E402

AUDIT = 'project_audit_log'


def connect(db_name, expect_host):
    """The database, pinned to the primary.

    The cluster URI carries ``readPreference=secondaryPreferred``, which is
    right for a web app and wrong for a migration: a plan built from a replica
    can be stale, and an insert has no filter to protect it the way an update
    does. Measured on dev 2026-08-30 -- the verification re-plan run seconds
    after a 181-event insert still reported all 181 as missing, because it read
    a replica that had not caught up. Reading the primary makes the plan and the
    verification say what is actually there.
    """
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri).get_database(
        db_name, read_preference=ReadPreference.PRIMARY)


def chain_index(projects):
    """``{project id or linkid: chain id}`` for every project document.

    Both spellings, because the audit log carries both: one caller passes
    ``linkid`` where the other seven pass ``_id``.
    """
    index = {}
    for doc in projects.find({}, {'_id': 1, 'linkid': 1, 'version_chain_id': 1}):
        chain_id = doc.get('version_chain_id')
        if chain_id is None:
            continue
        index[str(doc['_id'])] = chain_id
        if doc.get('linkid'):
            index[str(doc['linkid'])] = chain_id
    return index


def plan(audit, projects):
    """(resolvable, unresolvable) events lacking a chain id."""
    index = chain_index(projects)
    resolvable, unresolvable = [], []
    for event in audit.find({'version_chain_id': {'$exists': False}},
                            {'project_uuid': 1, 'event_type': 1,
                             'project_name': 1, 'timestamp': 1}):
        uuid = str(event.get('project_uuid'))
        chain_id = index.get(uuid)
        if chain_id is None and resolve_id(uuid) is not None:
            chain_id = index.get(str(resolve_id(uuid)))
        if chain_id is None:
            unresolvable.append(event)
        else:
            resolvable.append((event, chain_id))
    return resolvable, unresolvable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='stamp at most N events, for staging a first run')
    parser.add_argument('--undo-file', metavar='PATH',
                        help='where to write the inverse of every write')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    audit, projects = database[AUDIT], database['projects']
    print(f'target: {db_name} ({args.expect_host})')

    total = audit.count_documents({})
    already = audit.count_documents({'version_chain_id': {'$exists': True}})
    resolvable, unresolvable = plan(audit, projects)

    print(f'\n  audit events                  : {total}')
    print(f'  already carry a chain id      : {already}')
    print(f'  can be stamped (project found): {len(resolvable)}')
    print(f'  cannot (project is gone)      : {len(unresolvable)}')

    if args.verbose:
        for event in unresolvable:
            print(f'    unresolvable: {event.get("project_uuid")}  '
                  f'{event.get("event_type")}  '
                  f'{str(event.get("project_name"))[:40]}')

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
        return 0

    work = resolvable
    if args.limit is not None:
        work = work[:args.limit]
        print(f'\n--limit {args.limit}: writing {len(work)} of {len(resolvable)}')

    if args.undo_file:
        with open(args.undo_file, 'w') as handle:
            json.dump([{'_id': str(event['_id']),
                        'restore': {'$unset': {'version_chain_id': ''}}}
                       for event, _ in work], handle, indent=2)
        print(f'undo record: {args.undo_file}')

    written = 0
    for event, chain_id in work:
        # The filter re-asserts absence: an event that gained a chain id
        # between the plan and the write keeps the one it was given.
        written += audit.update_one(
            {'_id': event['_id'], 'version_chain_id': {'$exists': False}},
            {'$set': {'version_chain_id': str(chain_id)}}).modified_count
    print(f'\n{written} event(s) stamped.')

    resolvable, unresolvable = plan(audit, projects)
    print(f'after: {len(resolvable)} stampable, {len(unresolvable)} unresolvable')
    return 0


if __name__ == '__main__':
    sys.exit(main())
