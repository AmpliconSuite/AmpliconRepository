#!/usr/bin/env python
"""
Remove upload placeholders that crashed before they became projects.

An upload inserts a placeholder document -- name suffixed "(Processing...)",
`aggregation_in_progress: True`, `owner` set, `runs` empty -- and a background
thread replaces it with the real project, unsetting `owner` and
`original_project_name` as it goes.  When that thread dies, the placeholder
stays.  It is LIVE, so it sits on its owner's profile page looking like a
project, and it can never finish.

Measured 2026-08-27: 21 on prod, 4 on dev, the oldest from 2025-12-08.

Report by default.  --execute writes, and records the whole document it removed
so the delete can be undone.

The interesting part of this script is reasons_to_keep().  "These are dead" is
a claim, and the ways it could be wrong are known: the upload could still be
running, it could hold a payload after all, or something could reference it.
Those checks are in the tool rather than in whoever ran it, so a document that
is not what this script is for survives being handed to it.

Usage:
    python clear_stale_uploads.py --expect-db caper
    python clear_stale_uploads.py --expect-db caper --execute --limit 5
"""

import argparse
import datetime
import json
import os
import sys

from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.project_status import iter_previous_versions
from caper.project_version_cleanup import GRIDFS_FILE_KEYS

# The placeholder's three fingerprints, all set together at insert and all
# cleared together on success. Requiring all three -- rather than `owner` alone
# -- means a future document that happens to carry one of them is not swept up.
STALE_UPLOAD_QUERY = {
    'owner': {'$exists': True},
    'aggregation_in_progress': True,
    'project_name': {'$regex': r'\(Processing'},
}

# An aggregation that is merely slow must never be mistaken for one that died.
# The longest real run measured on this site is hours, not days.
DEFAULT_MIN_AGE_DAYS = 7


def gridfs_ids_anywhere(value, parent_key=None):
    """Every GridFS file id in a document, by the canonical key list.

    Deliberately not a check of the fields a placeholder is *expected* to have.
    The question is whether this document owns bytes, and the honest way to ask
    is to walk the whole thing with the same key list the deletion paths use.
    """
    if parent_key in GRIDFS_FILE_KEYS:
        if isinstance(value, ObjectId) or (isinstance(value, str)
                                           and ObjectId.is_valid(value)):
            yield parent_key
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from gridfs_ids_anywhere(child, key)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from gridfs_ids_anywhere(child, parent_key)


def build_reference_index(projects):
    """Who names whom, over the whole collection.

    Built over every document rather than over the candidates, because a
    reference that would save a placeholder comes from outside the group by
    definition.
    """
    references = {}
    for doc in projects.find({}, {'previous_versions': 1, 'redirect_to_project': 1}):
        for entry, _encoding in iter_previous_versions(doc):
            references.setdefault(str(entry.get('linkid')), []).append(
                ('history of', str(doc['_id'])))
        target = doc.get('redirect_to_project')
        if target:
            references.setdefault(str(target), []).append(
                ('redirect target of', str(doc['_id'])))
    return references


def reasons_to_keep(doc, references, min_age_days, now=None):
    """Every way "this placeholder is dead" could be wrong. Empty means dead.

    Each entry is a sentence, not a flag, because this is what gets printed
    when the script declines to delete something and the person reading it
    needs to know why without opening the source.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    reasons = []

    # From the ObjectId, not from date_created: 15 of the 21 on prod have no
    # date_created at all, and an age that is absent must not read as old.
    age = now - doc['_id'].generation_time
    if age.total_seconds() < min_age_days * 86400:
        reasons.append(
            'created %.1f days ago, under the %d-day floor -- it may still be '
            'aggregating' % (age.total_seconds() / 86400, min_age_days))

    runs = doc.get('runs') or {}
    if runs:
        reasons.append('holds %d sample(s) in runs' % len(runs))

    keys = sorted(set(gridfs_ids_anywhere(doc)))
    if keys:
        reasons.append('names GridFS files in %s' % ', '.join(keys))

    for relation, other in references.get(str(doc['_id']), []):
        reasons.append('is the %s %s' % (relation, other))

    return reasons


def plan(projects, min_age_days, now=None):
    """(deletable, held_back) -- held_back carries its reasons."""
    references = build_reference_index(projects)
    deletable, held_back = [], []
    for doc in projects.find(STALE_UPLOAD_QUERY):
        reasons = reasons_to_keep(doc, references, min_age_days, now=now)
        (held_back if reasons else deletable).append(
            (doc, reasons) if reasons else doc)
    deletable.sort(key=lambda doc: doc['_id'])
    return deletable, held_back


def apply_plan(projects, deletable, execute, undo=None):
    written = skipped = 0
    for doc in deletable:
        age_days = (datetime.datetime.now(datetime.timezone.utc)
                    - doc['_id'].generation_time).total_seconds() / 86400
        print('  %s  %5.0fd  %s' % (
            doc['_id'], age_days, str(doc.get('project_name'))[:48]))
        if not execute:
            continue
        # The whole document, because the inverse of a delete is an insert. A
        # placeholder has no runs, so this stays small.
        if undo is not None:
            undo.write(json.dumps({'op': 'insert', 'document': doc},
                                  default=str) + '\n')
            undo.flush()
        # Preconditioned on the document still being a stale placeholder, so a
        # thread that woke up and finished the upload between the plan and here
        # keeps its work.
        result = projects.delete_one(dict(STALE_UPLOAD_QUERY, _id=doc['_id']))
        if result.deleted_count:
            written += 1
        else:
            skipped += 1
            print('      SKIPPED -- no longer a stale placeholder')
    return written, skipped


def take(plan_items, limit):
    if limit is None or limit >= len(plan_items):
        return plan_items
    print('  --limit %d: acting on the first %d, leaving %d for a later run\n'
          % (limit, limit, len(plan_items) - limit))
    return plan_items[:limit]


def connect(expect_db):
    db_name = os.environ.get('DB_NAME')
    uri = os.environ.get('DB_URI_SECRET') or os.environ.get('DB_URI')
    if db_name != expect_db:
        sys.exit("ABORT: DB_NAME is %r, expected %r. Nothing was read."
                 % (db_name, expect_db))
    if not uri:
        sys.exit('ABORT: no connection string in the environment.')
    print("target: %r on %s\n" % (
        db_name, 'docdb' if 'docdb.amazonaws.com' in uri else 'local'))
    return MongoClient(uri)[db_name]['projects']


def main():
    parser = argparse.ArgumentParser(
        description='Remove upload placeholders whose aggregation never finished.')
    parser.add_argument('--expect-db', required=True,
                        help="Abort unless DB_NAME is this. 'caper' is prod.")
    parser.add_argument('--execute', action='store_true',
                        help='Actually delete. Without it the script reports.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='Delete only the first N, in _id order.')
    parser.add_argument('--min-age-days', type=int, default=DEFAULT_MIN_AGE_DAYS,
                        help='Refuse anything younger. Default %d.'
                             % DEFAULT_MIN_AGE_DAYS)
    parser.add_argument('--undo-file',
                        help='Where to record each removed document. Defaults '
                             'to a timestamped file. Only under --execute.')
    args = parser.parse_args()

    projects = connect(args.expect_db)
    print('mode: %s\n' % ('EXECUTE -- deletes documents' if args.execute
                          else 'REPORT -- nothing is deleted (pass --execute)'))

    deletable, held_back = plan(projects, args.min_age_days)

    if held_back:
        print('held back -- these are not dead:')
        for doc, reasons in held_back:
            print('  %s  %s' % (doc['_id'], str(doc.get('project_name'))[:48]))
            for reason in reasons:
                print('      %s' % reason)
        print('')

    if not deletable:
        print('no stale upload placeholders to remove')
        return 0

    print('%d stale upload placeholder(s):' % len(deletable))
    undo = None
    if args.execute:
        path = args.undo_file or 'clear-stale-uploads-undo-%s-%s.jsonl' % (
            args.expect_db, datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
        try:
            undo = open(path, 'x')
        except FileExistsError:
            sys.exit('%s exists -- it is another run\'s undo record. '
                     'Pass --undo-file with a new name.' % path)
        print('recording every removed document to %s\n' % path)

    written, skipped = apply_plan(projects, take(deletable, args.limit),
                                  args.execute, undo)

    if args.execute:
        name = undo.name
        undo.close()
        print('\n%d removed, %d skipped' % (written, skipped))
        print('undo record: %s (%d line(s))' % (name, sum(1 for _ in open(name))))
        return 1 if skipped else 0

    print('\nreport only -- nothing was deleted')
    return 0


if __name__ == '__main__':
    sys.exit(main())
