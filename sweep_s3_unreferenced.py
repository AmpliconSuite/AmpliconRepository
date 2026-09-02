#!/usr/bin/env python
"""
Remove S3 objects that no project document in any database references.

`amprepo-private` is a download cache: `project_download()` writes
`{prefix}{project_id}/{project_id}.tar.gz` and rebuilds it on a miss. It is also
a bucket shared by prod, dev and individual laptops, and nothing has ever
expired the project prefixes -- so it accumulates objects whose project was
permanently deleted years ago and which no code path can ever ask for again.

Measured 2026-08-27: of 935 objects and 199.6 GiB, **211 objects and 38.9 GiB
are named by no document in either database.** Of those, the 26 dated before
2025 (19.5 GiB) are the agreed first tranche.

**A miss is recoverable but not free.** Regeneration happens inside the request,
reading the tarball out of GridFS at a measured 21 MiB/s, so the largest cached
project (5.81 GiB) is about 277 s of a blocked gunicorn worker against a 900 s
limit. That is why this sweeps only objects nothing can ask for, and leaves
every referenced object alone regardless of age or size.

## The four ways this goes wrong, and what each fix is

Written down because the first estimate of "unreferenced" was a good estimate
and would have been a bad delete list.

1. **Key shape.** Ids were once extracted from the leading path segment only, so
   `jens/dev1/{id}/{id}.tar.gz` did not match and landed in "not a project
   prefix". Here every 24-hex run *anywhere* in the key is a candidate, and a
   key that yields no candidate at all is **unclassifiable**, never
   unreferenced.
2. **Databases that cannot be queried.** The bucket is shared with laptops whose
   Mongo nobody can reach. Referenced ids are supplied as files, one per
   database, and `--require-databases` refuses to run unless at least that many
   were provided -- so forgetting one cannot silently turn its objects into
   garbage.
3. **Snapshot race.** A project created between reading the ids and listing the
   bucket looks unreferenced. **Objects are listed first and ids read second**,
   which makes anything created in between count as referenced. That is the safe
   direction and it is why the order is not an accident.
4. **Other reference channels.** `project_audit_log` carries an explicit
   `s3_uri` on some entries. Those keys are unioned into the referenced set.

## What is never swept

Superseded versions are downloadable and their S3 objects are what serve them,
so "not the head of a chain" is not a test this script knows about: the id set
you supply must contain **every project document in any status**. Soft-deleted
projects are restorable and their ids are in that set too.

Report-only by default. `--execute` deletes, `--limit N` stages, and every
delete is recorded with its size, ETag and the version id S3 returns, so the
sweep can be undone inside the bucket's 90-day noncurrent window.

Usage:
    python sweep_s3_unreferenced.py --ids-file caper.ids --ids-file caper-dev.ids \\
        --before 2025-01-01 --report-only
    python sweep_s3_unreferenced.py --ids-file ... --before 2025-01-01 \\
        --execute --limit 5 --undo-record undo.json
"""

import argparse
import datetime
import json
import os
import re
import sys

OBJECT_ID_RE = re.compile(r'(?<![0-9a-fA-F])([0-9a-fA-F]{24})(?![0-9a-fA-F])')

DEFAULT_BUCKET = 'amprepo-private'


def object_ids_in(key):
    """Every 24-hex run in *key*, lowercased.

    Anywhere in the key, not just the first segment -- `jens/dev1/{id}/{id}.tar.gz`
    is a real project tarball and the leading-segment version of this missed 198
    of them.
    """
    return {m.group(1).lower() for m in OBJECT_ID_RE.finditer(key)}


def created_at(object_id):
    """The timestamp MongoDB embeds in the first four bytes of an ObjectId.

    This is the one provenance axis that survives on this bucket: every prefix
    dates itself from its own name, with no API call and no document.
    """
    return datetime.datetime.fromtimestamp(int(object_id[:8], 16),
                                           datetime.timezone.utc)


def classify(key, referenced):
    """`referenced`, `unreferenced`, or `unclassifiable` -- never a guess.

    A key with no id in it is not a project tarball this script understands, and
    the honest answer is that it does not know. Treating it as unreferenced is
    how a sweep deletes something nobody meant it to.
    """
    ids = object_ids_in(key)
    if not ids:
        return 'unclassifiable', None
    if ids & referenced:
        return 'referenced', None
    # The id naming the object is the one that repeats (prefix and filename);
    # fall back to the first if the key names only one.
    counts = {}
    for m in OBJECT_ID_RE.finditer(key):
        i = m.group(1).lower()
        counts[i] = counts.get(i, 0) + 1
    best = max(sorted(counts), key=lambda i: counts[i])
    return 'unreferenced', best


def load_ids(paths):
    ids = set()
    per_file = {}
    for p in paths:
        got = set()
        with open(p) as f:
            for line in f:
                line = line.strip().lower()
                if OBJECT_ID_RE.fullmatch(line):
                    got.add(line)
        per_file[p] = len(got)
        ids |= got
    return ids, per_file


def restrict_to_prefixes(candidates, prefixes):
    """Keep only the candidates whose key starts with one of `prefixes`.

    Narrowing only: a prefix-scoped sweep must never reach the shared root,
    which is written by prod, dev and every laptop and which no id set is
    authoritative for.
    """
    return [(o, i) for o, i in candidates
            if any(o['Key'].startswith(prefix) for prefix in prefixes)]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ids-file', action='append', required=True,
                    help='file of referenced project ids, one per line; repeat '
                         'once per database')
    ap.add_argument('--require-databases', type=int, default=2,
                    help='refuse to run with fewer id files than this (default '
                         '2: caper and caper-dev). Forgetting one turns its '
                         'objects into apparent garbage.')
    ap.add_argument('--extra-keys-file', action='append', default=[],
                    help='file of S3 keys referenced by another channel, e.g. '
                         'project_audit_log.s3_uri')
    ap.add_argument('--bucket', default=DEFAULT_BUCKET)
    ap.add_argument('--before', default=None,
                    help='only objects whose ObjectId predates this date '
                         '(YYYY-MM-DD)')
    ap.add_argument('--bare-prefix-only', action='store_true',
                    help='only keys of the form <id>/... -- skips developer '
                         'prefixes like jens/ and ted/, which is what keeps a '
                         'first sweep to one namespace and its result checkable')
    ap.add_argument('--key-prefix', action='append', default=[],
                    help='restrict the tranche to keys under this prefix; '
                         'repeatable. The inverse of --bare-prefix-only, and '
                         'the way to sweep one writer\'s namespace: a laptop '
                         'prefix is written by exactly one machine, so that '
                         'machine\'s id set is authoritative for it in a way '
                         'no id set is authoritative for the shared root.')
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--undo-record', default=None)
    args = ap.parse_args()

    if len(args.ids_file) < args.require_databases:
        print('refusing to run: %d id file(s) given, --require-databases is %d. '
              'An unqueried database makes its objects look unreferenced.'
              % (len(args.ids_file), args.require_databases))
        return 2

    import boto3
    s3 = boto3.client('s3')

    # Objects FIRST, ids second -- see docstring, correction 3.
    objects = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=args.bucket):
        for o in page.get('Contents', []):
            objects.append(o)
    print('listed %d objects in s3://%s' % (len(objects), args.bucket))

    referenced, per_file = load_ids(args.ids_file)
    for p, n in per_file.items():
        print('  %-40s %d ids' % (os.path.basename(p), n))
    print('  referenced ids, union                    %d' % len(referenced))

    extra_keys = set()
    for p in args.extra_keys_file:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    extra_keys.add(line.split('amazonaws.com/')[-1].lstrip('/'))
    if extra_keys:
        print('  keys referenced by another channel       %d' % len(extra_keys))

    buckets = {'referenced': [], 'unreferenced': [], 'unclassifiable': []}
    for o in objects:
        key = o['Key']
        if key.endswith('/') or o['Size'] == 0:
            continue
        if key in extra_keys:
            buckets['referenced'].append((o, None))
            continue
        state, oid = classify(key, referenced)
        buckets[state].append((o, oid))

    G = 1024 ** 3
    print()
    for state in ('referenced', 'unreferenced', 'unclassifiable'):
        n = len(buckets[state])
        b = sum(o['Size'] for o, _ in buckets[state])
        print('  %-16s %5d objects  %8.2f GiB' % (state, n, b / G))

    candidates = buckets['unreferenced']
    if args.bare_prefix_only:
        before = len(candidates)
        candidates = [(o, i) for o, i in candidates
                      if OBJECT_ID_RE.fullmatch(o['Key'].split('/')[0] or '')]
        print('\n  --bare-prefix-only: %d -> %d (developer prefixes left alone)'
              % (before, len(candidates)))
    if args.key_prefix:
        before = len(candidates)
        candidates = restrict_to_prefixes(candidates, args.key_prefix)
        print('\n  --key-prefix %s: %d -> %d'
              % (', '.join(args.key_prefix), before, len(candidates)))
    if args.before:
        cutoff = datetime.datetime.strptime(args.before, '%Y-%m-%d').replace(
            tzinfo=datetime.timezone.utc)
        before = len(candidates)
        candidates = [(o, i) for o, i in candidates if created_at(i) < cutoff]
        print('  --before %s: %d -> %d' % (args.before, before, len(candidates)))

    candidates.sort(key=lambda oi: created_at(oi[1]))
    total = sum(o['Size'] for o, _ in candidates)
    print('\nTRANCHE: %d objects, %.2f GiB' % (len(candidates), total / G))
    print('%-64s %12s  %s' % ('key', 'bytes', 'id date'))
    for o, oid in candidates:
        print('%-64s %12d  %s' % (o['Key'][:64], o['Size'],
                                  created_at(oid).date()))

    if not args.execute:
        print('\nREPORT ONLY -- nothing deleted. Add --execute.')
        return 0

    if args.limit:
        candidates = candidates[:args.limit]
        print('\n--limit %d: acting on the %d oldest' % (args.limit, len(candidates)))

    record = {'bucket': args.bucket,
              'taken': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'deleted': []}
    for o, oid in candidates:
        resp = s3.delete_object(Bucket=args.bucket, Key=o['Key'])
        record['deleted'].append({
            'key': o['Key'], 'size': o['Size'], 'etag': o.get('ETag'),
            'last_modified': str(o.get('LastModified')),
            'delete_marker_version_id': resp.get('VersionId'),
            'project_id': oid,
        })
        print('deleted %s (%d bytes)' % (o['Key'], o['Size']))

    path = args.undo_record or ('s3-sweep-undo-%s.json'
                                % datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
    with open(path, 'w') as f:
        json.dump(record, f, indent=2)
    print('\ndeleted %d objects, %.2f GiB. Undo record: %s'
          % (len(record['deleted']),
             sum(d['size'] for d in record['deleted']) / G, path))
    print('Versioning is on with a 90-day noncurrent expiry, so each of these is '
          'recoverable by removing its delete marker until that window closes.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
