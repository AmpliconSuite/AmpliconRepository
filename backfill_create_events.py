#!/usr/bin/env python
"""Give every project document the creation event it never got.

Audit logging was added after the site had been running for two years, so most
project documents have no event saying they were made. On prod on 2026-08-30,
246 of 311 documents had no ``create`` or ``edit_new_version`` event naming
them, and their history pages were blank. What each of those documents does
carry is ``date`` (when the version was written) and ``creator`` (who ran the
aggregation), and those two are exactly a creation event.

**What is reconstructed, and what is not.** Three fields are taken from the
document -- the timestamp from ``date``, the actor from ``creator``, and the
event type from ``version_ordinal``. Tool versions and sample count are copied
across when the document has them, because the event's copy of those means "the
versions this run used", which is the same thing the document records. Nothing
else is written: no ``s3_uri``, no file size, no before/after state. Those were
never recorded and are not guessable, and a gap is honest where a plausible
value is not.

**Every event this writes is flagged ``backfilled: True``** and carries the
basis it was reconstructed from. The admin history view badges them as
reconstructed. That flag is the point: a synthesised event that cannot be told
apart from an observed one has made the log less trustworthy, not more.

**The event type is inferred and can be wrong in one known way.** Ordinal 1 is
written as ``create`` and anything higher as ``edit_new_version``, which follows
from ordinals never being renumbered on deletion. But ordinals were assigned by
the Phase 1 migration from the denormalised ``previous_versions[]``, so a
project whose genuine first version had already been permanently deleted before
that migration has its oldest *surviving* version sitting at ordinal 1. Such a
document gets ``create`` when the truth is ``edit_new_version``. The timestamp
and the actor are right either way, and the ``backfilled`` flag is what tells a
reader the type was derived rather than observed.

Idempotent: coverage is recomputed every run, so once a document has a creation
event this finds nothing more to do for it. No marker and no override needed.

Report-only unless ``--execute``.
"""

import argparse
import datetime
import json
import os
import sys

from pymongo import MongoClient
from pymongo.read_preferences import ReadPreference

AUDIT = 'project_audit_log'

#: Copied onto the event when the document has them. These describe the run,
#: which is what the event's fields of the same name have always meant.
COPIED = ('AA_version', 'AC_version', 'ASP_version', 'sample_count')

#: What the reconstruction rests on, recorded on every event it writes so a
#: reader can judge it without finding this script.
BASIS = 'date + creator + version_ordinal'

PROJECTION = {'_id': 1, 'project_name': 1, 'date': 1, 'creator': 1,
              'version_chain_id': 1, 'version_ordinal': 1, 'status': 1,
              'AA_version': 1, 'AC_version': 1, 'ASP_version': 1,
              'sample_count': 1}


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


def parse_date(value):
    """*value* as a datetime, or ``None`` if it is not one.

    Prod stores ``date`` as an ISO-8601 string on all 311 documents; dev has a
    mix of those and real datetimes. Both are accepted, anything else is a
    document this script leaves alone rather than dating by guesswork.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def event_type_for(doc):
    """``'create'`` for the first version of a chain, else an edit."""
    ordinal = doc.get('version_ordinal')
    if isinstance(ordinal, int) and ordinal > 1:
        return 'edit_new_version'
    return 'create'


def covered(audit):
    """The project ids that already have a creation event."""
    return {str(event.get('project_uuid')) for event in audit.find(
        {'event_type': {'$in': ['create', 'edit_new_version']}},
        {'project_uuid': 1})}


def plan(audit, projects):
    """(events to insert, documents skipped and why)."""
    have = covered(audit)
    to_insert, skipped = [], []
    for doc in projects.find({}, PROJECTION):
        if str(doc['_id']) in have:
            continue
        timestamp = parse_date(doc.get('date'))
        if timestamp is None:
            skipped.append((doc, 'no usable date'))
            continue
        entry = {
            'timestamp': timestamp,
            'user_email': doc.get('creator') or 'unknown',
            'project_uuid': str(doc['_id']),
            'project_name': doc.get('project_name'),
            'event_type': event_type_for(doc),
            'new_version': True,
            'version_chain_id': (str(doc['version_chain_id'])
                                 if doc.get('version_chain_id') is not None
                                 else None),
            'backfilled': True,
            'backfill_basis': BASIS,
            'backfilled_at': datetime.datetime.now(datetime.timezone.utc),
        }
        for field in COPIED:
            if doc.get(field) is not None:
                entry[field] = doc[field]
        to_insert.append(entry)
    return to_insert, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='insert at most N events, for staging a first run')
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

    before_events = audit.count_documents({})
    to_insert, skipped = plan(audit, projects)
    kinds = {}
    for entry in to_insert:
        kinds[entry['event_type']] = kinds.get(entry['event_type'], 0) + 1
    unknown_actor = sum(1 for e in to_insert if e['user_email'] == 'unknown')

    print(f'\n  project documents             : {projects.count_documents({})}')
    print(f'  audit events now              : {before_events}')
    print(f'  documents with no create event: {len(to_insert) + len(skipped)}')
    print(f'  events to insert              : {len(to_insert)}')
    for kind in sorted(kinds):
        print(f'      as {kind:<18}: {kinds[kind]}')
    print(f'  of those, actor unknown       : {unknown_actor}')
    print(f'  skipped (no usable date)      : {len(skipped)}')

    if args.verbose:
        for doc, why in skipped:
            print(f'    skipped {doc["_id"]}: {why}')
        for entry in to_insert[:20]:
            print(f'    {entry["timestamp"]}  {entry["event_type"]:<17} '
                  f'{entry["user_email"][:28]:<28} '
                  f'{str(entry["project_name"])[:34]}')

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
        return 0

    work = to_insert
    if args.limit is not None:
        work = work[:args.limit]
        print(f'\n--limit {args.limit}: writing {len(work)} of {len(to_insert)}')

    if not work:
        print('\nnothing to do.')
        return 0

    # Upsert rather than insert, keyed on "the backfilled creation event for
    # this project". Plan-level idempotence already stops a second run finding
    # anything; this makes a duplicate impossible rather than merely unlikely,
    # which matters because an insert -- unlike an update -- has no filter that
    # can refuse a plan built a moment ago from data that has since moved.
    inserted = []
    for entry in work:
        result = audit.update_one(
            {'project_uuid': entry['project_uuid'], 'backfilled': True},
            {'$setOnInsert': entry}, upsert=True)
        if result.upserted_id is not None:
            inserted.append(result.upserted_id)
    print(f'\n{len(inserted)} event(s) inserted.')
    if len(inserted) < len(work):
        print(f'  ({len(work) - len(inserted)} already had a backfilled '
              'event and were left alone)')

    if args.undo_file:
        # Written after the insert because the inverse of an insert is the id
        # it produced, which does not exist until then. The events carry
        # backfilled:True as well, so the set is recoverable even without this.
        with open(args.undo_file, 'w') as handle:
            json.dump({'delete_ids': [str(i) for i in inserted],
                       'also_identified_by': {'backfilled': True,
                                              'backfill_basis': BASIS}},
                      handle, indent=2)
        print(f'undo record: {args.undo_file}')

    to_insert, skipped = plan(audit, projects)
    print(f'after: {audit.count_documents({})} events, '
          f'{len(to_insert)} document(s) still without one')
    return 0


if __name__ == '__main__':
    sys.exit(main())
