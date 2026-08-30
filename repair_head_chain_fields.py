#!/usr/bin/env python
"""Put back the chain-level fields that promotion dropped from a chain's head.

I17's findings, made fixable. A chain-level field is one that belongs to the
project rather than to any one of its versions, so the head -- the document the
project is read through -- has to carry it. Until 2026-08-30 promotion decided
what to carry from a hand-written list of nine names, and anything the list
forgot was left behind on the older version. Nothing was corrupted and no other
invariant noticed: the value is still there, on a document nobody reads.

Measured on prod 2026-08-30, seven such fields across six projects:

  featured     TCGA and GLASS carry featured=True on v1 and no featured field
               at all on the head. The featured list queries the head, so both
               have been quietly missing from it. This is not a deliberate
               unfeaturing -- the admin control writes `$set {featured: False}`,
               which leaves the field present. Absence can only be inheritance.
  privateKey   three projects. It is an API upload credential, checked on top
               of membership, so a head without it makes API re-upload of that
               project fail with 403.
  alias        two projects. Vestigial: 5 documents site-wide and nothing reads
               it. Restored for consistency, at no risk either way.

**The value comes from the newest version that has one.** A field can sit on
several older versions with different values, and the most recently set is the
one the project last meant.

Heads only, and only fields that are absent from the head. A head holding a
different value is not this script's business: that is a disagreement, not a
gap, and guessing which side is right is how a live value gets overwritten.

Report-only unless ``--execute``.
"""

import argparse
import json
import os
import sys

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.project_fields import CARRIED_ON_PROMOTION           # noqa: E402

PROJECTION = {'project_name': 1, 'version_chain_id': 1, 'version_ordinal': 1,
              'is_latest': 1}
PROJECTION.update({field: 1 for field in CARRIED_ON_PROMOTION})


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri)[db_name]


def _ordinal(doc):
    value = doc.get('version_ordinal')
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def plan(projects):
    """[(head_id, project_name, field, value, from_ordinal), ...]."""
    chains = {}
    for doc in projects.find({'version_chain_id': {'$ne': None}}, PROJECTION):
        chains.setdefault(str(doc['version_chain_id']), []).append(doc)

    repairs = []
    for _chain_id, members in sorted(chains.items()):
        if len(members) < 2:
            continue
        heads = [d for d in members if d.get('is_latest') is True]
        # Not exactly one head is I3's finding. Writing to a chain whose head is
        # ambiguous is how the wrong document gets the value.
        if len(heads) != 1:
            continue
        head = heads[0]
        others = sorted((d for d in members if d['_id'] != head['_id']),
                        key=_ordinal, reverse=True)
        for field in sorted(CARRIED_ON_PROMOTION):
            if field in head:
                continue
            source = next((d for d in others if field in d), None)
            if source is None:
                continue
            repairs.append((head['_id'], head.get('project_name'), field,
                            source[field], _ordinal(source)))
    return repairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--execute', action='store_true',
                        help='write. Without it this reports and changes nothing.')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='repair at most N fields, for staging a first run')
    parser.add_argument('--undo-file', metavar='PATH',
                        help='where to write the inverse of every write')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    projects = database['projects']
    print(f'target: {db_name} ({args.expect_host})')

    repairs = plan(projects)
    print(f'\n{len(repairs)} field(s) to restore onto {len({r[0] for r in repairs})} head(s)\n')
    for head_id, name, field, value, ordinal in repairs:
        shown = repr(value)
        if len(shown) > 30:
            shown = shown[:27] + '...'
        print(f'  {str(name)[:30]:<32} {field:<16} <- v{ordinal}  {shown}')

    if not repairs:
        print('nothing to do.')
        return 0

    if not args.execute:
        print('\nreport only. Re-run with --execute to write.')
        return 0

    work = repairs
    if args.limit is not None:
        work = work[:args.limit]
        print(f'\n--limit {args.limit}: writing {len(work)} of {len(repairs)}')

    if args.undo_file:
        with open(args.undo_file, 'w') as handle:
            json.dump([{'_id': str(h), 'unset': f, 'restored_value': repr(v),
                        'from_version_ordinal': o}
                       for h, _n, f, v, o in work], handle, indent=2)
        print(f'undo record: {args.undo_file}')

    written = 0
    for head_id, _name, field, value, _ordinal in work:
        # The filter re-asserts absence. A value written between the plan and
        # here does not match, and is left alone rather than overwritten -- the
        # one thing this script must never do is clobber a live field.
        written += projects.update_one(
            {'_id': head_id, field: {'$exists': False}},
            {'$set': {field: value}}).modified_count
    print(f'\n{written} field(s) restored.')

    print(f'after: {len(plan(projects))} field(s) still missing from a head')
    return 0


if __name__ == '__main__':
    sys.exit(main())
