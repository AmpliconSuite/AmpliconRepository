#!/usr/bin/env python
"""How fast each upload wrote to GridFS. **Read-only.**

Every per-feature file an upload stores carries a backlink naming its project
document and an ``uploadDate``, so the span from an upload's first file to its
last is how long its GridFS loop ran, give or take the first file. That makes
the speed of every past upload readable from ``fs.files`` alone, with no new
field.

For each upload: when it started, its span, files, MiB and s/MiB, newest
first. Then the median s/MiB per month over uploads of at least ``--min-mib``,
because small uploads cost more per MiB and would drown the comparison.

Measured on prod 2026-10-14 (uploads of at least 100 MiB): a median of
0.13 s/MiB before 2026-09-03 and 0.30 after. The upload log line
``Putting files in GridFS took ...`` reports the same figures for one upload.

Compare like with like: since uploads stopped storing sample-level files once
per feature, an upload writes far fewer MiB than the same data did before.

Usage:

    report_upload_rates.py --expect-db caper --expect-host docdb [--since 2026-08-01]
"""

import argparse
import datetime
import os
import statistics
import sys

from bson.objectid import ObjectId
from pymongo import MongoClient


def connect(db_name, expect_host):
    uri = os.environ['DB_URI_SECRET']
    is_local = 'localhost' in uri or '127.0.0.1' in uri
    if expect_host == 'local' and not is_local:
        sys.exit('--expect-host local, but the URI does not name localhost')
    if expect_host == 'docdb' and is_local:
        sys.exit('--expect-host docdb, but the URI names localhost')
    return MongoClient(uri)[db_name]


def upload_spans(files, since):
    """One row per project document: first and last uploadDate, files, bytes."""
    pipeline = [
        {'$match': {'uploadDate': {'$gte': since},
                    'metadata.feature_key': {'$exists': True}}},
        {'$group': {'_id': '$metadata.project_id',
                    'start': {'$min': '$uploadDate'},
                    'end': {'$max': '$uploadDate'},
                    'files': {'$sum': 1},
                    'bytes': {'$sum': '$length'}}},
    ]
    rows = []
    for row in files.aggregate(pipeline, allowDiskUse=True):
        seconds = (row['end'] - row['start']).total_seconds()
        mib = row['bytes'] / 2**20
        rows.append({
            'project_id': row['_id'],
            'start': row['start'],
            'seconds': seconds,
            'files': row['files'],
            'mib': mib,
            # A single file, or files written in the same instant, has no span.
            's_per_mib': seconds / mib if mib and seconds else None,
        })
    rows.sort(key=lambda r: r['start'], reverse=True)
    return rows


def monthly_medians(rows, min_mib):
    by_month = {}
    for row in rows:
        if row['s_per_mib'] is None or row['mib'] < min_mib:
            continue
        by_month.setdefault(row['start'].strftime('%Y-%m'), []).append(row['s_per_mib'])
    return [(month, len(rates), statistics.median(rates))
            for month, rates in sorted(by_month.items())]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--since', default='2025-01-01',
                        help='earliest uploadDate, YYYY-MM-DD (default 2025-01-01)')
    parser.add_argument('--min-mib', type=float, default=100,
                        help='smallest upload counted in the monthly medians (default 100)')
    parser.add_argument('--top', type=int, default=30, metavar='N',
                        help='uploads to list, newest first (default 30)')
    args = parser.parse_args(argv)

    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != args.expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {args.expect_db!r}')
    database = connect(db_name, args.expect_host)
    since = datetime.datetime.fromisoformat(args.since)
    print(f'target: {db_name} ({args.expect_host})   READ-ONLY   since {args.since}')

    rows = upload_spans(database['fs.files'], since)
    ids = [r['project_id'] for r in rows[:args.top] if isinstance(r['project_id'], ObjectId)]
    names = {d['_id']: d.get('project_name', '')
             for d in database['projects'].find({'_id': {'$in': ids}}, {'project_name': 1})}

    print(f'\n{len(rows)} upload(s); newest {min(args.top, len(rows))}:')
    print(f"  {'started (UTC)':16}  {'project':36}  {'secs':>8}  {'files':>6}  "
          f"{'MiB':>9}  {'s/MiB':>6}")
    for row in rows[:args.top]:
        name = names.get(row['project_id'], '(document gone)')
        rate = f"{row['s_per_mib']:.3f}" if row['s_per_mib'] is not None else '-'
        print(f"  {row['start']:%Y-%m-%d %H:%M}  {str(name)[:36]:36}  "
              f"{row['seconds']:>8.1f}  {row['files']:>6,}  {row['mib']:>9,.1f}  {rate:>6}")

    print(f'\nmedian s/MiB per month, uploads of at least {args.min_mib:g} MiB:')
    for month, count, median in monthly_medians(rows, args.min_mib):
        print(f'  {month}  {median:.3f}  ({count} upload(s))')
    return 0


if __name__ == '__main__':
    sys.exit(main())
