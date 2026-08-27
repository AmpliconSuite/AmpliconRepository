#!/usr/bin/env python
"""
backfill_project_status.py

Two one-time corrections to the projects collection, both of which repair
documents the application can no longer read correctly.

  current   A project deleted before the 'current' flag existed has
            delete=True and no 'current' field at all.  The soft-delete path
            sets 'delete' and deliberately leaves 'current' alone, so it cannot
            create a field that was never there.  The result classifies as
            DETACHED, and -- because the admin restore page selects
            {delete: True, current: True} -- it is not offered for restore.
            Someone deleted these projects and the recovery path does not list
            them.  70 documents on prod, 51 on dev.

            The value to write is derivable, not a guess: a document another
            project names in previous_versions[] was a version of that project
            and belongs at current=False; one nothing names was the head of its
            own chain when it was deleted and belongs at current=True.

  lineage   Before April 2024, previous_versions[] entries were stored as
            strings holding JSON, keyed 'link' rather than 'linkid'.  Nothing
            in the application reads that encoding: the entry renders as a link
            to /project/[{"date": ...}] and matches no query, so the history
            table is short by one row and the reference looks dangling when it
            is not.  Rewriting the entry in the encoding the reader expects
            changes no reference, only how it is written.  2 documents on prod,
            5 on dev.

Neither correction is applied to a document with delete=False and no usable
'current' value.  Those are reachable today, they are not one population --
some are pre-flag projects, some are upload placeholders that never finished --
and no rule here is right for all of them.

Reporting is the default.  Nothing is written without --execute, and every
write is guarded by a precondition on the field being changed, so a document
modified between the report and the execute is skipped rather than clobbered.
Re-running after a successful execute finds nothing to do.

Usage:
    source caper/config.sh
    python backfill_project_status.py --expect-db caper-dev
    python backfill_project_status.py --expect-db caper-dev --execute

Requirements:
    pymongo, bson.  Run from the repository root, which is where
    caper/caper/project_status.py is imported from.
"""

import argparse
import datetime
import json
import os
import sys

from bson import ObjectId
from pymongo import MongoClient, uri_parser

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.project_status import (            # noqa: E402
    CURRENT_ENCODING,
    MISSING_CURRENT_QUERY,
    SOFT_DELETED,
    SUPERSEDED,
    TOMBSTONE,
    classify,
    combine,
    iter_lineage_references,
    iter_previous_versions,
    status_flags,
)


# ---------------------------------------------------------------------------
# Connecting to the right database
# ---------------------------------------------------------------------------

def connect(expected_db, expected_host):
    """Return the projects collection, or exit before reading anything.

    Prod and dev share one DocumentDB cluster, and dev's database name is the
    same one the local docker mongo uses, so neither the name nor the host
    identifies a deployment on its own.  Both are asserted, and both are
    printed, so what was written to is visible in the output rather than
    inferred from which terminal it was run in.
    """
    uri = os.getenv('DB_URI_SECRET')
    if not uri:
        sys.exit('DB_URI_SECRET is not set.  Run:  source caper/config.sh')

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != expected_db:
        sys.exit('DB_NAME is %r, expected %r -- nothing read' % (db_name, expected_db))

    hosts = uri_parser.parse_uri(uri)['nodelist']
    kind = ('docdb' if all(h.endswith('.docdb.amazonaws.com') for h, _port in hosts)
            else 'local')
    if expected_host and kind != expected_host:
        sys.exit('connected to a %s host, expected %s -- nothing read' % (kind, expected_host))

    print('target: %r on %s' % (db_name, kind))
    return MongoClient(uri)[db_name]['projects']


# ---------------------------------------------------------------------------
# Undoing it without restoring the cluster
# ---------------------------------------------------------------------------

def record(rollback, doc_id, operator, fields):
    """Append the inverse of one write, as a line of JSON.

    The cluster's own backups are not a practical undo for this. Prod and dev
    share one DocumentDB cluster and a restore builds a *new* cluster rather
    than rewinding this one, so undoing a 70-field write that way means
    rewinding dev too and repointing both connection strings.

    The inverse of each write here is exact and tiny -- put the field back the
    way it was, on that one _id -- so it is written down as it happens.
    Replaying the file is a for-loop; it is a record, not a tool.
    """
    if rollback is None:
        return
    rollback.write(json.dumps({
        '_id': str(doc_id), 'op': operator, 'fields': fields}, default=str) + '\n')
    rollback.flush()   # a run that dies partway still leaves an undo for what it did


# ---------------------------------------------------------------------------
# Pass 1 -- the missing 'current' flag
# ---------------------------------------------------------------------------

def plan_current(projects):
    """(doc, target_status) for every document missing its 'current' flag."""
    referenced = set()
    for doc in projects.find({}, {'previous_versions': 1}):
        for linkid, _encoding in iter_lineage_references(doc):
            referenced.add(linkid)

    plan = []
    for doc in projects.find(MISSING_CURRENT_QUERY,
                             {'project_name': 1, 'delete': 1, 'current': 1,
                              'delete_user': 1, 'delete_date': 1,
                              'version_deleted_from_history': 1,
                              'payload_purged': 1}):
        if classify(doc) == TOMBSTONE:
            # Its markers decide its status whatever 'current' says, so
            # writing the flag would change nothing and imply otherwise.
            continue
        # A version another project still names is that project's history.
        # Anything else was the head of its own chain when it was deleted.
        target = SUPERSEDED if str(doc['_id']) in referenced else SOFT_DELETED
        plan.append((doc, target))
    # Sorted so that --limit takes the same documents every time: a first run of
    # three and a later run of the rest must not overlap or leave a gap.
    plan.sort(key=lambda entry: entry[0]['_id'])
    return plan


def apply_current(projects, plan, execute, rollback=None):
    written = skipped = 0
    for doc, target in plan:
        value = status_flags(target)['current']
        print('  %s  %-40s  %s -> current=%s' % (
            doc['_id'], (doc.get('project_name') or '?')[:40], target, value))
        if not execute:
            continue
        record(rollback, doc['_id'], '$unset', {'current': ''})
        # The precondition is the same query that selected the document.  If
        # anything wrote 'current' since the plan was built, this matches
        # nothing and the document is left alone.
        result = projects.update_one(
            combine(MISSING_CURRENT_QUERY, _id=doc['_id']),
            {'$set': {'current': value}})
        if result.modified_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- changed since the plan was built')
    return written, skipped


# ---------------------------------------------------------------------------
# Pass 2 -- lineage entries in the encoding nothing reads
# ---------------------------------------------------------------------------

def plan_lineage(projects):
    """(doc, rewritten_previous_versions) for documents holding legacy entries."""
    plan = []
    for doc in projects.find({'previous_versions': {'$exists': True, '$ne': []}},
                             {'project_name': 1, 'previous_versions': 1}):
        entries = list(iter_previous_versions(doc))
        if not any(encoding != CURRENT_ENCODING for _entry, encoding in entries):
            continue
        unusable = [entry['linkid'] for entry, _encoding in entries
                    if not ObjectId.is_valid(entry['linkid'])]
        if unusable:
            # Checked on the value, not on the encoding label: a string that is
            # not JSON comes back from iter_previous_versions() tagged as the
            # current encoding, because for a *reader* "treat it as a bare id"
            # is a reasonable last resort.  Writing it back is not -- it would
            # store prose in a field that is supposed to hold an id, and the
            # entry would stop looking broken while still being broken.
            print('  %s  %-40s  LEFT ALONE -- entry holds %r, which is not an id'
                  % (doc['_id'], (doc.get('project_name') or '?')[:40], unusable[0]))
            continue
        # iter_previous_versions() already normalises both encodings to the
        # same shape, so the rewrite is what the reader was going to build
        # anyway -- written down instead of recomputed on every page load.
        plan.append((doc, [entry for entry, _encoding in entries]))
    plan.sort(key=lambda entry: entry[0]['_id'])
    return plan


def apply_lineage(projects, plan, execute, rollback=None):
    written = skipped = 0
    for doc, rewritten in plan:
        print('  %s  %-40s' % (doc['_id'], (doc.get('project_name') or '?')[:40]))
        for old, new in zip(doc.get('previous_versions', []), rewritten):
            if old != new:
                print('      was  %r' % (old,))
                print('      now  %r' % (new,))
        if not execute:
            continue
        record(rollback, doc['_id'],
               '$set', {'previous_versions': doc['previous_versions']})
        # Preconditioned on the array being byte-for-byte what was read.
        result = projects.update_one(
            {'_id': doc['_id'], 'previous_versions': doc['previous_versions']},
            {'$set': {'previous_versions': rewritten}})
        if result.modified_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- changed since the plan was built')
    return written, skipped


# ---------------------------------------------------------------------------

def take(plan, limit):
    """The first *limit* entries of a plan, saying how many are held back.

    Printed rather than silent because the exit status of a limited run looks
    exactly like the exit status of a complete one, and "70 document(s)"
    followed by three lines of output is otherwise easy to read as a failure.
    """
    # `limit is None`, not `not limit`: 0 has to mean zero documents. Someone
    # typing --limit 0 to make a run do nothing must not get all 70 instead.
    if limit is None or limit >= len(plan):
        return plan
    print('  --limit %d: acting on the first %d, leaving %d for a later run\n'
          % (limit, limit, len(plan) - limit))
    return plan[:limit]


def check_targets_exist(projects, plan):
    """Warn where a rewritten lineage entry names a document that is not there.

    The rewrite is still correct -- it preserves whatever the entry held -- but
    a reference that resolves to nothing after it is readable is a different
    problem, and it should not first become visible as a blank row on a
    history page.
    """
    # plan_lineage() has already refused any entry whose linkid is not a valid
    # ObjectId, so these all convert.
    wanted = {ObjectId(entry['linkid']) for _doc, entries in plan for entry in entries}
    present = {row['_id'] for row in projects.find({'_id': {'$in': list(wanted)}}, {'_id': 1})}
    for missing in wanted - present:
        print('  rewritten entry names %s, which is not in the collection' % missing)


def main():
    parser = argparse.ArgumentParser(
        description='Backfill the missing current flag and rewrite lineage '
                    'entries stored in the pre-April-2024 encoding.')
    parser.add_argument('--expect-db', required=True,
                        help="Abort unless DB_NAME is this. 'caper' is prod.")
    parser.add_argument('--expect-host', choices=['local', 'docdb'],
                        help='Abort unless the host is this kind.')
    parser.add_argument('--execute', action='store_true',
                        help='Actually write. Without it the script only reports.')
    parser.add_argument('--only', choices=['current', 'lineage'],
                        help='Run one pass instead of both.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='Act on only the first N documents of each pass, '
                             'in _id order. The report still counts them all, '
                             'so the line above the list says how many were '
                             'left. Running again with a larger N (or none) '
                             'picks up where this stopped, because a document '
                             'already written no longer appears in the plan.')
    parser.add_argument('--rollback-file',
                        help='Where to record the inverse of each write. '
                             'Defaults to a timestamped file in the working '
                             'directory. Only written under --execute.')
    args = parser.parse_args()

    projects = connect(args.expect_db, args.expect_host)
    print('mode: %s\n' % ('EXECUTE -- writes documents' if args.execute
                          else 'REPORT -- nothing is written (pass --execute)'))

    rollback = None
    if args.execute:
        path = args.rollback_file or 'backfill-rollback-%s-%s.jsonl' % (
            args.expect_db, datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
        # Opened before the first write, and exclusively: an existing file is a
        # previous run's undo, and truncating it would destroy the only cheap
        # way back from that run.
        try:
            rollback = open(path, 'x')
        except FileExistsError:
            sys.exit('%s exists -- it is another run\'s undo record. '
                     'Pass --rollback-file with a new name.' % path)
        print('recording the undo for every write to %s\n' % path)

    totals = {'written': 0, 'skipped': 0}

    if args.only in (None, 'current'):
        print('=' * 78)
        print("current -- deleted before the flag existed, so not offered for restore")
        print('=' * 78)
        plan = plan_current(projects)
        if not plan:
            print('  nothing to do')
        else:
            by_status = {}
            for _doc, target in plan:
                by_status[target] = by_status.get(target, 0) + 1
            print('  %d document(s): %s\n' % (
                len(plan), ', '.join('%d %s' % (n, s) for s, n in sorted(by_status.items()))))
            plan = take(plan, args.limit)
            written, skipped = apply_current(projects, plan, args.execute, rollback)
            totals['written'] += written
            totals['skipped'] += skipped
        print('')

    if args.only in (None, 'lineage'):
        print('=' * 78)
        print('lineage -- entries stored in the encoding the application cannot read')
        print('=' * 78)
        plan = plan_lineage(projects)
        if not plan:
            print('  nothing to do')
        else:
            print('  %d document(s)\n' % len(plan))
            plan = take(plan, args.limit)
            written, skipped = apply_lineage(projects, plan, args.execute, rollback)
            totals['written'] += written
            totals['skipped'] += skipped
            check_targets_exist(projects, plan)
        print('')

    if args.execute:
        name = rollback.name
        rollback.close()
        print('%d document(s) written, %d skipped' % (totals['written'], totals['skipped']))
        print('undo record: %s (%d line(s))' % (name, sum(1 for _ in open(name))))
        # A skip means a document changed between the plan and the write, which
        # is not an error but is not a completed backfill either.
        return 1 if totals['skipped'] else 0

    print('report only -- nothing was written')
    return 0


if __name__ == '__main__':
    sys.exit(main())
