#!/usr/bin/env python
"""
snapshot_storage.py

Record one day's storage snapshot: what the database holds, summed across
projects, bucketed by who owns it.

Run from cron, once a day. The walk behind it is a single pass over fs.files
for id and length plus every project document through the application's own
iter_gridfs_file_ids, which is seconds of work and has no business happening
on a page load. The admin statistics page reads the result.

One snapshot per database per day, keyed by the day, so a second run in the
same day is not a second row -- pass --force to replace it, which is what to
do after deleting something large.

Usage:
    source caper/config.sh
    python snapshot_storage.py --expect-db caper
    python snapshot_storage.py --expect-db caper --force
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')

import django  # noqa: E402
django.setup()

from caper import storage_stats  # noqa: E402
from caper.utils import db_handle  # noqa: E402


def gib(value):
    return value / 1024 ** 3


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--expect-db', required=True,
                        help='the database this run is meant to measure, '
                             'asserted against the one the environment points '
                             'at -- dev and prod share one cluster, so the '
                             'name is never taken as the instruction')
    parser.add_argument('--force', action='store_true',
                        help="replace today's snapshot instead of keeping it")
    args = parser.parse_args()

    if db_handle.name != args.expect_db:
        print('connected to %r, but --expect-db says %r. Check which config.sh '
              'is sourced.' % (db_handle.name, args.expect_db))
        return 2

    snapshot = storage_stats.record(force=args.force)
    if snapshot is None:
        print('%s already has a snapshot for today; --force to replace it'
              % db_handle.name)
        return 0

    print('%s  %d projects  %d files  %.2f GiB  %d chunks' % (
        snapshot['database'], snapshot['projects'], snapshot['files'],
        gib(snapshot['bytes']), snapshot['chunks']))
    for key, label in storage_stats.BUCKETS:
        bucket = snapshot['buckets'][key]
        print('  %-30s %8.2f GiB  %8d files  %4s documents' % (
            label, gib(bucket['bytes']), bucket['files'],
            snapshot['documents'].get(key, '-')))
    for side in ('listed', 'restricted'):
        seen = snapshot['visibility'][side]
        print('  %-30s %8.2f GiB  %8d files' % (
            'visibility: %s' % side, gib(seen['bytes']), seen['files']))
    if snapshot['shared_bytes']:
        print('  %-30s %8.2f GiB' % ('named by a second document',
                                     gib(snapshot['shared_bytes'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
