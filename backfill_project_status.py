#!/usr/bin/env python
"""
backfill_project_status.py

One-time corrections to the projects collection, each of which repairs
documents the application can no longer read correctly.  Five passes, run in
order: current, lineage, pointers, status, visibility.  The two that started
this file:

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
from caper.visibility import (               # noqa: E402
    VISIBILITY_VALUES,
    normalize_visibility_field,
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

# ---------------------------------------------------------------------------
# Pass 3 -- lineage pointers
# ---------------------------------------------------------------------------

POINTER_FIELDS = ('version_chain_id', 'previous_version_id', 'next_version_id',
                  'version_ordinal', 'is_latest')

MISSING_POINTERS_QUERY = {'version_chain_id': {'$exists': False}}


def build_chains(projects):
    """Group every document into the chain it belongs to.

    Connected components over previous_versions[], and over nothing else.  Not
    over project_name: two projects can share a name, one project can be
    renamed, and inferring lineage from a name is how a rename becomes a merge.
    """
    docs = {}
    ancestors = {}
    named_by = {}
    # version_chain_id is projected because plan_pointers() skips documents
    # that already have it. Leaving it out makes the pass plan every document
    # on every run: the writes are preconditioned so nothing is clobbered, but
    # the plan is wrong and --limit spends its budget on documents already done.
    #
    # 'delete' and 'current' are deliberately not read here. Lineage is a
    # question about references, not about status, and this pass writes the same
    # pointers whatever a document's status is -- a superseded version and a
    # tombstone both occupy their ordinal.
    for doc in projects.find({}, {'project_name': 1, 'previous_versions': 1,
                                  'version_chain_id': 1}):
        doc_id = str(doc['_id'])
        docs[doc_id] = doc
        ancestors[doc_id] = [str(entry['linkid'])
                             for entry, _encoding in iter_previous_versions(doc)]

    for doc_id in docs:
        for linkid in ancestors[doc_id]:
            if linkid in docs:
                named_by.setdefault(linkid, []).append(doc_id)

    # A chain is what one head reaches, not what union-find merges.
    #
    # The earlier version grouped by connected component over previous_versions[]
    # references. That is wrong whenever two live projects reference each other's
    # history -- a re-upload that starts a fresh chain while still naming an old
    # version merges two real chains into one component with two heads, and the
    # ordering step then refuses it as ambiguous. Measured on caper-dev
    # 2026-09-02: five components held 19 documents, and every one of them was
    # two or more separable chains rather than one broken chain. Those documents
    # are historical fixtures that a test environment mirroring production is
    # supposed to have, so refusing them was the expensive answer.
    #
    # Reaching is transitive because a version that dropped an ancestor from its
    # own list still reaches it through the version in between.
    heads = [doc_id for doc_id in docs if not named_by.get(doc_id)]

    def reaches(head):
        seen, stack = set(), [head]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            for linkid in ancestors.get(node, ()):
                if linkid in docs and linkid not in seen:
                    stack.append(linkid)
        return seen

    reached = {head: reaches(head) for head in heads}

    # A document two heads both reach is a fork, and a fork is the one thing
    # here that is genuinely ambiguous: nothing in the data says which branch is
    # the real history, and the ordinals of everything below the fork depend on
    # the answer. Those heads are merged back into a single group so that the
    # ordering step sees more than one head and refuses the whole thing -- the
    # members below the fork must not be given ordinals either.
    groups = []
    for head, members in reached.items():
        merged = [head]
        for other in list(groups):
            if members & set().union(*(reached[h] for h in other)):
                merged.extend(other)
                groups.remove(other)
        groups.append(merged)

    chains = {}
    for group in groups:
        key = min(group)
        chains[key] = sorted(set().union(*(reached[h] for h in group)))

    # Anything no head reaches is in a reference cycle, so it has no head at
    # all. Grouped together rather than dropped, so the ordering step refuses it
    # out loud instead of it vanishing from both the plan and the report.
    unreached = sorted(set(docs) - {m for members in chains.values() for m in members})
    if unreached:
        chains['cycle:%s' % unreached[0]] = unreached

    return docs, ancestors, named_by, chains


def order_chain(members, docs, ancestors, named_by):
    """The chain's members oldest-first, or a reason it cannot be ordered.

    The order is read off the head's own previous_versions[], because that is
    the order the application wrote: each new version is created with the old
    list plus the version it replaces.  No date is consulted -- dates are
    missing on some documents and tied on others, and the list is authoritative
    where it exists.

    Returns (ordered, None) or (None, reason).
    """
    heads = [m for m in members if not named_by.get(m)]
    if len(heads) != 1:
        return None, ('%d documents in this chain are named by nothing, so it '
                      'has %d possible heads' % (len(heads), len(heads)))

    head = heads[0]
    listed = [m for m in ancestors[head] if m in docs and m in set(members)]
    if len(set(listed)) != len(listed):
        return None, 'the head lists the same version more than once'

    # An ancestor the head reaches but does not list itself: some intermediate
    # version named it and the head's own list dropped it. It is still part of
    # this history and goes in front of everything the head does list, because
    # the only thing known about it is that it is older than the version that
    # named it. Ties are broken by date, then by id, so a re-run orders the
    # same way and the ordinals are stable.
    unlisted = sorted(set(members) - set(listed) - {head},
                      key=lambda m: (str(docs[m].get('date') or ''), m))
    if not listed and not unlisted:
        return [head], None

    return unlisted + listed + [head], None


def plan_pointers(projects):
    """(doc, fields) for every document that can be given lineage pointers.

    Also returns the chains it refused, each with the reason, because a chain
    this cannot order is a finding about the data rather than a gap in the
    backfill.
    """
    docs, ancestors, named_by, chains = build_chains(projects)

    plan, refused = [], []
    for members in chains.values():
        ordered, reason = order_chain(members, docs, ancestors, named_by)
        if ordered is None:
            refused.append((members, reason))
            continue

        # The oldest version's id names the chain: derivable from the data
        # rather than minted, so a re-run computes the same value, and stable
        # because a new version is appended at the other end.
        chain_id = ObjectId(ordered[0])

        for ordinal, doc_id in enumerate(ordered, start=1):
            doc = docs[doc_id]
            if 'version_chain_id' in doc:
                continue
            plan.append((doc, {
                'version_chain_id': chain_id,
                'previous_version_id': (ObjectId(ordered[ordinal - 2])
                                        if ordinal > 1 else None),
                'next_version_id': (ObjectId(ordered[ordinal])
                                    if ordinal < len(ordered) else None),
                'version_ordinal': ordinal,
                'is_latest': ordinal == len(ordered),
            }))

    plan.sort(key=lambda entry: (entry[1]['version_chain_id'],
                                 entry[1]['version_ordinal']))
    return plan, refused


def apply_pointers(projects, plan, execute, rollback=None):
    written = skipped = 0
    for doc, fields in plan:
        print('  %s  %-38s  chain %s  ordinal %d%s' % (
            doc['_id'], (doc.get('project_name') or '?')[:38],
            fields['version_chain_id'], fields['version_ordinal'],
            '  is_latest' if fields['is_latest'] else ''))
        if not execute:
            continue
        record(rollback, doc['_id'], '$unset',
               {field: '' for field in POINTER_FIELDS})
        result = projects.update_one(
            combine(MISSING_POINTERS_QUERY, _id=doc['_id']),
            {'$set': fields})
        if result.modified_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- changed since the plan was built')
    return written, skipped


# ---------------------------------------------------------------------------
# Pass 4 -- the stored status field
# ---------------------------------------------------------------------------

MISSING_STATUS_QUERY = {'status': {'$exists': False}}


def plan_status(projects, refresh=False):
    """(doc, stored, computed) for documents whose 'status' needs writing.

    Run last on purpose. classify() reads the flags, so this pass must see the
    values the 'current' pass wrote rather than the absences it replaced --
    otherwise 70 documents on prod would be stored as DETACHED, which is what
    they were before the backfill and not what they are after it.

    With *refresh*, documents whose stored status disagrees with classify() are
    included too.  That case is not hypothetical and it is not a data fault: a
    deployment running code from before status_after() existed writes 'delete'
    on a soft delete and leaves 'status' behind, so the field goes stale on the
    first deletion after the backfill and before the deploy.  Recomputing is
    safe while nothing reads the field -- the flags remain authoritative until
    Phase 2 -- and refusing to fix it would mean the backfill can create a
    disagreement it cannot repair.
    """
    plan = []
    for doc in projects.find({}):
        computed = classify(doc)
        stored = doc.get('status')
        if stored is None:
            plan.append((doc, None, computed))
        elif refresh and stored != computed:
            plan.append((doc, stored, computed))
    plan.sort(key=lambda entry: entry[0]['_id'])
    return plan


def apply_status(projects, plan, execute, rollback=None):
    written = skipped = 0
    for doc, stored, computed in plan:
        print('  %s  %-40s  %s' % (
            doc['_id'], (doc.get('project_name') or '?')[:40],
            computed if stored is None else '%s -> %s' % (stored, computed)))
        if not execute:
            continue
        # The undo restores exactly what was there, which for a first write is
        # the absence of the field and for a refresh is the stale value. Both
        # are recorded rather than assumed, so replaying the file is the same
        # loop either way.
        if stored is None:
            record(rollback, doc['_id'], '$unset', {'status': ''})
            precondition = MISSING_STATUS_QUERY
        else:
            record(rollback, doc['_id'], '$set', {'status': stored})
            precondition = {'status': stored}
        result = projects.update_one(
            combine(precondition, _id=doc['_id']),
            {'$set': {'status': computed}})
        if result.modified_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- changed since the plan was built')
    return written, skipped


# ---------------------------------------------------------------------------
# Pass 5 -- the legacy boolean visibility
# ---------------------------------------------------------------------------

LEGACY_VISIBILITY_QUERY = {'private': {'$type': 'bool'}}


def plan_visibility(projects):
    """(doc, stored, canonical) for documents whose 'private' is a boolean.

    'private' holds one of three visibility strings.  A handful of documents
    predate that and hold a boolean; on prod, measured 2026-08-27, exactly one
    does (True).  This is a schema irregularity and **not** an exposure: every
    query in the application matches with $in across both encodings, so the
    document is already treated as private everywhere it is read.

    What it does break is anything that compares the value rather than querying
    it -- the edit form's visibility dropdown renders with nothing selected for
    a boolean, because a ChoiceField over the three strings matches no boolean.

    Only booleans are rewritten.  A string outside the three legal values is
    reported and left alone: coercing it would hide corrupt data, which is the
    same call schema_validate._normalize_legacy_visibility makes.
    """
    plan = []
    unknown = []
    for doc in projects.find({}):
        stored = doc.get('private')
        if isinstance(stored, bool):
            plan.append((doc, stored, normalize_visibility_field(stored)))
        elif stored is not None and stored not in VISIBILITY_VALUES:
            unknown.append((doc, stored))
    plan.sort(key=lambda entry: entry[0]['_id'])
    return plan, unknown


def apply_visibility(projects, plan, execute, rollback=None):
    written = skipped = 0
    for doc, stored, canonical in plan:
        print('  %s  %-40s  %r -> %r' % (
            doc['_id'], (doc.get('project_name') or '?')[:40], stored, canonical))
        if not execute:
            continue
        record(rollback, doc['_id'], '$set', {'private': stored})
        # Preconditioned on the boolean still being there, so a document whose
        # visibility was edited between the report and the write is skipped
        # rather than reverted to whatever the plan computed.
        result = projects.update_one(
            combine({'private': stored}, _id=doc['_id']),
            {'$set': {'private': canonical}})
        if result.modified_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- changed since the plan was built')
    return written, skipped


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
    parser.add_argument('--only',
                        choices=['current', 'lineage', 'pointers', 'status',
                                 'visibility'],
                        help='Run one pass instead of all five.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='Act on only the first N documents of each pass, '
                             'in _id order. The report still counts them all, '
                             'so the line above the list says how many were '
                             'left. Running again with a larger N (or none) '
                             'picks up where this stopped, because a document '
                             'already written no longer appears in the plan.')
    parser.add_argument('--refresh-status', action='store_true',
                        help="Also rewrite a stored 'status' that disagrees "
                             "with classify(). Needed where the backfill has "
                             "run but the code that maintains the field has "
                             "not been deployed yet.")
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

    if args.only in (None, 'pointers'):
        print('=' * 78)
        print('pointers -- lineage read off previous_versions[] and written down')
        print('=' * 78)
        plan, refused = plan_pointers(projects)
        if refused:
            print('  %d chain(s) left alone -- the data does not order them:\n'
                  % len(refused))
            for members, reason in refused:
                print('      %s' % reason)
                for member in sorted(members):
                    print('        %s' % member)
                print('')
        if not plan:
            print('  nothing to do')
        else:
            print('  %d document(s) in %d chain(s)\n' % (
                len(plan), len({fields['version_chain_id'] for _doc, fields in plan})))
            plan = take(plan, args.limit)
            written, skipped = apply_pointers(projects, plan, args.execute, rollback)
            totals['written'] += written
            totals['skipped'] += skipped
        print('')

    if args.only in (None, 'status'):
        print('=' * 78)
        print("status -- what classify() says, written down so a query can use it")
        print('=' * 78)
        plan = plan_status(projects, refresh=args.refresh_status)
        if not plan:
            print('  nothing to do')
        else:
            by_status = {}
            for _doc, _stored, computed in plan:
                by_status[computed] = by_status.get(computed, 0) + 1
            print('  %d document(s): %s\n' % (
                len(plan), ', '.join('%d %s' % (n, s)
                                     for s, n in sorted(by_status.items()))))
            plan = take(plan, args.limit)
            written, skipped = apply_status(projects, plan, args.execute, rollback)
            totals['written'] += written
            totals['skipped'] += skipped
        print('')

    if args.only in (None, 'visibility'):
        print('=' * 78)
        print("visibility -- the legacy boolean 'private', written as a string")
        print('=' * 78)
        plan, unknown = plan_visibility(projects)
        for doc, stored in unknown:
            print('  %s  %-40s  UNKNOWN VALUE %r -- left alone' % (
                doc['_id'], (doc.get('project_name') or '?')[:40], stored))
        if not plan:
            print('  nothing to do')
        else:
            print('  %d document(s) with a boolean visibility\n' % len(plan))
            plan = take(plan, args.limit)
            written, skipped = apply_visibility(projects, plan, args.execute, rollback)
            totals['written'] += written
            totals['skipped'] += skipped
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
