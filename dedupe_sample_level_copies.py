#!/usr/bin/env python
"""Collapse the per-feature copies of sample-level files to one per sample.

Until v4.4.4 the upload loop stored every file a run.json row named, once per
row, and run.json has one row per *feature* with the sample's columns repeated.
So a sample's CNV BED, metadata JSONs and reconstruction directory were stored
once per feature. Measured 2026-10-14: 232,659 extra files (136.9 GiB) on prod,
99,512 (72.9 GiB) on dev. New uploads store one copy; this script fixes the
documents written before.

Every reader takes a sample's first feature, or the id in a URL, and no URL
carries a sample-level id. The copy the first feature names is therefore the
one every reader already uses, and it is the one kept.

Phases, run in this order, each its own decision:

  report    (default) what would change, and the predicted counts. Reads only.
  rewrite   point every feature at the copy its sample's first feature names.
            One guarded update per sample; the undo line is written before the
            write. Exactly reversible while the old copies still exist.
  rollback  write the old ids back from a rewrite's undo record.
  delete    remove the copies a rewrite recorded and nothing names any more.
            The only undo is a snapshot restore.

A group -- one document, one sample, one key -- is collapsed only when every
copy is proven identical: equal md5 where every copy has one, otherwise the
sha256 of its bytes, or for a directory archive the hash of its members'
names, sizes and bytes (each gzip stamps its own time, so raw bytes differ). A
mismatch, a missing file or an unreadable copy skips the group, reported.

Scope: LIVE and SUPERSEDED documents; the sample-level keys below in both
spellings. Amplicon- and feature-level files, archives and S3 are untouched.

Writes require --execute, shutdown mode on and no running background task.

Usage (from the repository root, config.sh sourced):

  python dedupe_sample_level_copies.py --expect-db caper-dev --expect-host local
  python dedupe_sample_level_copies.py ... --phase rewrite --undo-file U.jsonl --execute [--limit N | --only ID]
  python dedupe_sample_level_copies.py ... --phase rollback --undo-file U.jsonl --execute
  python dedupe_sample_level_copies.py ... --phase delete --undo-file U.jsonl --delete-log D.jsonl --execute [--budget-seconds S]
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import tarfile
import time

from bson import ObjectId
from pymongo import MongoClient, ReadPreference, uri_parser

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper'))

from caper.project_status import LIVE, STATUS_QUERIES, SUPERSEDED   # noqa: E402
from caper.project_version_cleanup import (                           # noqa: E402
    DIRECTORY_FILE_KEYS, FEATURE_FILE_KEYS, PENDING_PAYLOAD_KEY,
    delete_gridfs_file_in_batches, iter_gridfs_file_ids,
)

# Sample-level keys, taken from the canonical lists so a renamed key fails here
# rather than being silently skipped.
_SAMPLE_LEVEL = ('CNV BED file', 'Run metadata JSON', 'Sample metadata JSON')
assert set(_SAMPLE_LEVEL) <= set(FEATURE_FILE_KEYS)
SAMPLE_LEVEL_KEYS = frozenset(
    spelling for key in _SAMPLE_LEVEL + tuple(DIRECTORY_FILE_KEYS)
    for spelling in (key, key.replace(' ', '_')))
DIRECTORY_KEYS = frozenset(
    spelling for key in DIRECTORY_FILE_KEYS for spelling in (key, key.replace(' ', '_')))

IN_SCOPE = {'$or': [STATUS_QUERIES[LIVE], STATUS_QUERIES[SUPERSEDED]]}

# Measured on prod (2026-08-31): what one GridFS delete costs.
DELETE_SECONDS_PER_GIB = 96.4
DELETE_SECONDS_PER_FILE = 0.035

# On caper-dev (2026-10-14) one update repointing 480 features of a 1,411-sample
# document was refused with "Operation terminated due to low available memory";
# 320 had gone through. A sample's changes are split into updates this size.
MAX_CHANGES_PER_UPDATE = 100


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def plan_document(doc):
    """The groups in one document: one per (run key, key) naming 2+ distinct ids.

    Each group: run_key, key, keeper, changes [(feature index, old id)], ids
    (every distinct id in the group), skip (None or the reason it is left).
    """
    groups = []
    for run_key, features in (doc.get('runs') or {}).items():
        if not isinstance(features, list) or len(features) < 2:
            continue
        keys = {k for feature in features if isinstance(feature, dict)
                for k in feature if k in SAMPLE_LEVEL_KEYS}
        for key in sorted(keys):
            values = [f.get(key) if isinstance(f, dict) else None for f in features]
            ids = {v for v in values if isinstance(v, ObjectId)}
            if len(ids) < 2:
                continue
            group = {'run_key': run_key, 'key': key, 'keeper': values[0],
                     'ids': ids, 'changes': [], 'skip': None}
            if not isinstance(values[0], ObjectId):
                group['skip'] = 'first feature names no file'
            elif '.' in run_key or run_key.startswith('$'):
                group['skip'] = 'run key cannot be a field path'
            else:
                group['changes'] = [(i, v) for i, v in enumerate(values)
                                    if isinstance(v, ObjectId) and v != values[0]]
            groups.append(group)
    return groups


def _tar_digest(stream):
    """Hash of an archive's members -- names, sizes, bytes -- in name order."""
    members = {}
    with tarfile.open(fileobj=stream, mode='r|gz') as tar:
        for member in tar:
            digest = hashlib.sha256()
            if member.isfile():
                handle = tar.extractfile(member)
                for block in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(block)
            members[member.name] = (member.size, digest.hexdigest())
    return hashlib.sha256(json.dumps(sorted(members.items())).encode()).hexdigest()


def _sha256(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        digest.update(block)
    return digest.hexdigest()


def prove_identical(files, bucket, key, ids):
    """(proven, method, detail) for one group's distinct ids.

    ``files`` is fs.files; ``bucket`` opens a file by id (gridfs.GridFS).
    """
    rows = {r['_id']: r for r in files.find({'_id': {'$in': list(ids)}},
                                            {'md5': 1, 'length': 1})}
    missing = [i for i in ids if i not in rows]
    if missing:
        return False, None, f'{len(missing)} copy(ies) have no fs.files row'
    md5s = {rows[i].get('md5') for i in ids}
    lengths = {rows[i].get('length') for i in ids}
    if None not in md5s and len(md5s) == 1 and len(lengths) == 1:
        return True, 'md5', None
    method = 'tar-content' if key in DIRECTORY_KEYS else 'sha256'
    hashes = set()
    for file_id in ids:
        try:
            stream = bucket.get(file_id)
            hashes.add(_tar_digest(stream) if method == 'tar-content' else _sha256(stream))
        except Exception as error:
            return False, method, f'{file_id} unreadable: {type(error).__name__}: {error}'
        if len(hashes) > 1:
            return False, method, 'contents differ'
    return True, method, None


def named_ids(doc):
    """Every GridFS id *doc* names, wherever in the document it is.

    The whole document, not just ``runs`` and ``tarfile``: on caper-dev
    (2026-10-14) a 2023 document's top-level ``features_list`` repeated its
    feature rows and named 9,036 old ids after ``runs`` had stopped naming them.
    A check confined to ``runs`` called those files deletable.
    """
    found = set(iter_gridfs_file_ids(doc))
    found.update(i for i in (doc.get(PENDING_PAYLOAD_KEY) or []) if isinstance(i, ObjectId))
    return found


def freed_ids(doc, proven_groups):
    """Ids the document would name no longer once *proven_groups* are applied.

    Computed on a copy rather than per group: the two directory aliases name the
    same files, and an id one key gives up can still be named under another --
    or elsewhere in the document entirely.
    """
    import copy
    after_doc = copy.deepcopy(doc)
    runs = after_doc.get('runs') or {}
    for g in proven_groups:
        for index, _old in g['changes']:
            runs[g['run_key']][index][g['key']] = g['keeper']
    return named_ids(doc) - named_ids(after_doc)


def named_by_other_documents(projects, candidates):
    """{id: [document ids]} for candidate ids named by more than one document.

    Walks every document, any status, including a tombstone's pending purge
    list: a file one document names and another also names is not this
    script's to free.
    """
    owners = {}
    progress = Progress('checking documents for shared ids', projects.estimated_document_count())
    for done, doc in enumerate(projects.find({}), 1):
        progress.tick(done)
        for file_id in named_ids(doc) & candidates:
            owners.setdefault(file_id, set()).add(doc['_id'])
    return {i: sorted(map(str, d)) for i, d in owners.items() if len(d) > 1}


def scope_documents(projects, only=None, limit=None):
    """In-scope document ids, smallest sample count first, then by id."""
    query = IN_SCOPE if not only else {'$and': [IN_SCOPE, {'_id': {'$in': [ObjectId(o) for o in only]}}]}
    rows = list(projects.find(query, {'sample_count': 1}))
    rows.sort(key=lambda r: (r.get('sample_count') or 0, r['_id']))
    ids = [r['_id'] for r in rows]
    return ids[:limit] if limit else ids


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _path(run_key, index, key):
    return f'runs.{run_key}.{index}.{key}'


def _write_line(handle, record):
    handle.write(json.dumps(record, default=str) + '\n')
    handle.flush()
    os.fsync(handle.fileno())


def rewrite_sample(projects, doc_id, run_key, groups, undo):
    """Guarded updates for one sample. Returns True when every one was applied.

    The changes go in batches of at most MAX_CHANGES_PER_UPDATE. Each batch's
    filter names every old id it replaces, so a sample changed since it was read
    is skipped, not overwritten, and the batches after a missed one are not
    tried. Each batch writes its intent before the write and its outcome after,
    so the record is right whichever happens.
    """
    changes = [(g['key'], i, old, g['keeper']) for g in groups for i, old in g['changes']]
    for start in range(0, len(changes), MAX_CHANGES_PER_UPDATE):
        batch = changes[start:start + MAX_CHANGES_PER_UPDATE]
        query = {'_id': doc_id}
        update = {}
        for key, index, old, keeper in batch:
            query[_path(run_key, index, key)] = old
            update[_path(run_key, index, key)] = keeper
        for key, index, old, keeper in batch:
            _write_line(undo, {'op': 'repoint', 'doc': str(doc_id), 'run_key': run_key, 'index': index,
                               'key': key, 'old': str(old), 'keeper': str(keeper)})
        result = projects.update_one(query, {'$set': update})
        applied = result.matched_count == 1 and result.modified_count == 1
        _write_line(undo, {'op': 'applied' if applied else 'missed', 'doc': str(doc_id),
                           'run_key': run_key, 'changes': len(batch)})
        if not applied:
            return False
    return True


def read_undo(path):
    """The repoint lines whose sample's update was applied."""
    pending, applied = {}, []
    with open(path) as handle:
        for line in handle:
            record = json.loads(line)
            sample = (record['doc'], record['run_key'])
            if record['op'] == 'repoint':
                pending.setdefault(sample, []).append(record)
            elif record['op'] == 'applied':
                applied.extend(pending.pop(sample, []))
            elif record['op'] == 'missed':
                pending.pop(sample, None)
    return applied


def rollback(projects, files, repoints):
    """Write each old id back where the keeper now stands. Returns (restored, refused)."""
    restored = refused = 0
    for record in repoints:
        old = ObjectId(record['old'])
        if files.count_documents({'_id': old}, limit=1) == 0:
            refused += 1          # already deleted by phase 2: writing it back would dangle
            continue
        path = _path(record['run_key'], record['index'], record['key'])
        result = projects.update_one({'_id': ObjectId(record['doc']), path: ObjectId(record['keeper'])},
                                     {'$set': {path: old}})
        restored += result.modified_count
        refused += 1 - result.modified_count
    return restored, refused


def delete_candidates(projects, files, repoints):
    """Old ids from the record that are safe to delete, and why the rest are not.

    Safe means: no document names it, its fs.files row exists, and its backlink
    names the document the rewrite took it from.
    """
    expected = {}
    for record in repoints:
        expected[ObjectId(record['old'])] = ObjectId(record['doc'])
    still_named = set()
    for doc in projects.find({}):
        still_named |= named_ids(doc) & set(expected)
    safe, refused = [], {}
    rows = {r['_id']: r for r in files.find({'_id': {'$in': list(expected)}},
                                            {'length': 1, 'metadata.project_id': 1})}
    for file_id, doc_id in expected.items():
        if file_id in still_named:
            refused[str(file_id)] = 'still named by a document'
        elif file_id not in rows:
            refused[str(file_id)] = 'already gone'
        elif (rows[file_id].get('metadata') or {}).get('project_id') != doc_id:
            refused[str(file_id)] = 'backlink names another document'
        else:
            safe.append((file_id, rows[file_id].get('length') or 0))
    return safe, refused


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def connect(expect_db, expect_host):
    uri = os.getenv('DB_URI_SECRET')
    if not uri:
        sys.exit('DB_URI_SECRET is not set. Run: source caper/config.sh')
    db_name = os.getenv('DB_NAME', 'caper')
    if db_name != expect_db:
        sys.exit(f'DB_NAME is {db_name!r}, --expect-db said {expect_db!r}; nothing read')
    hosts = uri_parser.parse_uri(uri)['nodelist']
    kind = 'docdb' if all(h.endswith('.docdb.amazonaws.com') for h, _ in hosts) else 'local'
    if kind != expect_host:
        sys.exit(f'connected to a {kind} host, --expect-host said {expect_host}; nothing read')
    print(f'target: {db_name!r} on {kind}')
    # PRIMARY throughout: every read here decides a write, and the cluster's
    # default sends reads to a replica that can be behind.
    return MongoClient(uri, read_preference=ReadPreference.PRIMARY)[db_name]


def require_quiet(db):
    import shutdown_mode
    if shutdown_mode.get_flag(db['system_settings']) is not True:
        sys.exit('REFUSED: shutdown mode is off. Turn it on (shutdown_mode.py on) before writing.')
    tasks = shutdown_mode.running_tasks(db['background_tasks'])
    if tasks:
        sys.exit(f'REFUSED: {len(tasks)} background task(s) running.')


def reindexer():
    """reindex_project, with Django set up and S3 static sync off."""
    os.environ['S3_STATIC_FILES'] = 'FALSE'
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
    import django
    django.setup()
    from caper.project_events import reindex_project
    return reindex_project


def gib(n):
    return f'{n / 2**30:,.2f} GiB'


class Progress:
    """A timestamped line at most every ``every`` seconds, so a long run can be
    watched (and a stalled one noticed) from its log."""

    def __init__(self, label, total, every=30.0):
        self.label, self.total, self.every = label, total, every
        self.started = self.last = time.time()

    def tick(self, done, extra='', force=False):
        now = time.time()
        if not force and now - self.last < self.every:
            return
        self.last = now
        elapsed = now - self.started
        eta = ''
        if 0 < done < self.total:
            eta = f', about {elapsed / done * (self.total - done) / 60:,.1f} min left'
        print(f'  [{datetime.datetime.utcnow():%H:%M:%S}] {self.label}: {done:,}/{self.total:,}'
              f'{", " + extra if extra else ""} ({elapsed / 60:,.1f} min{eta})')


def run_plan(db, args):
    """Plan, prove and (on rewrite --execute) apply. Prints the report."""
    import gridfs
    projects, files, bucket = db['projects'], db['fs.files'], gridfs.GridFS(db)
    doc_ids = scope_documents(projects, args.only, args.limit)
    print(f'documents in scope: {len(doc_ids)} (LIVE + SUPERSEDED)'
          + (f', limited to {args.limit}' if args.limit else ''))

    plans, candidates = [], set()
    progress = Progress('planning documents', len(doc_ids))
    for done, doc_id in enumerate(doc_ids, 1):
        doc = projects.find_one({'_id': doc_id}, {'runs': 1, 'project_name': 1, 'tarfile': 1})
        groups = plan_document(doc)
        if groups:
            plans.append((doc_id, doc, groups))
            for g in groups:
                candidates |= g['ids']
        progress.tick(done, f'{len(plans):,} with copies, {len(candidates):,} ids')
    print(f'documents with copies: {len(plans):,}; checking {len(candidates):,} ids against every document')
    shared = named_by_other_documents(projects, candidates) if candidates else {}
    print(f'ids in groups: {len(candidates):,}; named by more than one document: {len(shared)}')
    if shared:
        for file_id, owners in list(shared.items())[:10]:
            print(f'  {file_id}: {owners}')
        sys.exit('REFUSED: an id is shared between documents; freeing it would break the other.')

    undo = None
    if args.phase == 'rewrite' and args.execute:
        undo = open(args.undo_file, 'x')
        reindex = reindexer()
    totals = dict(groups=0, proven_md5=0, proven_read=0, skipped=0, features=0,
                  files=0, bytes=0, samples_applied=0, samples_missed=0, read_bytes=0)
    skipped = []
    started = time.time()
    progress = Progress('proving groups', sum(len(groups) for _d, _doc, groups in plans))
    for doc_id, doc, groups in plans:
        by_sample = {}
        proven_groups = []
        for g in groups:
            totals['groups'] += 1
            if g['skip'] is None:
                proven, method, detail = prove_identical(files, bucket, g['key'], g['ids'])
                if not proven:
                    g['skip'] = f'{method or "check"}: {detail}'
                else:
                    totals['proven_md5' if method == 'md5' else 'proven_read'] += 1
                    if method != 'md5':
                        totals['read_bytes'] += sum(r['length'] for r in files.find(
                            {'_id': {'$in': list(g['ids'])}}, {'length': 1}))
                    totals['features'] += len(g['changes'])
                    proven_groups.append(g)
                    by_sample.setdefault(g['run_key'], []).append(g)
            if g['skip'] is not None:
                totals['skipped'] += 1
                skipped.append((str(doc_id), g['run_key'], g['key'], g['skip']))
            progress.tick(totals['groups'], f"{gib(totals['read_bytes'])} read, "
                                            f"{totals['skipped']:,} skipped")
        # The whole document, not the runs/tarfile projection above: a legacy
        # top-level features_list can still name an old id (prod, 2026-10-15: 60
        # ids in two documents), and the delete phase will refuse those.
        freed = freed_ids(projects.find_one({'_id': doc_id}), proven_groups)
        totals['files'] += len(freed)
        totals['bytes'] += sum(r['length'] for r in files.find({'_id': {'$in': list(freed)}}, {'length': 1}))
        if undo is not None and by_sample:
            for run_key, sample_groups in by_sample.items():
                if rewrite_sample(projects, doc_id, run_key, sample_groups, undo):
                    totals['samples_applied'] += 1
                else:
                    totals['samples_missed'] += 1
            reindex(doc_id)

    print(f"\ngroups: {totals['groups']:,}  proven by md5: {totals['proven_md5']:,}  "
          f"proven by reading: {totals['proven_read']:,} ({gib(totals['read_bytes'])} read)  "
          f"skipped: {totals['skipped']:,}")
    print(f"features to repoint: {totals['features']:,}")
    print(f"copies freed: {totals['files']:,} files, {gib(totals['bytes'])}")
    print(f"elapsed: {time.time() - started:,.1f} s")
    for row in skipped[:20]:
        print('  skipped', *row)
    if undo is not None:
        undo.close()
        print(f"\nAPPLIED: {totals['samples_applied']:,} sample update(s); missed (changed underneath): "
              f"{totals['samples_missed']:,}. Undo record: {args.undo_file}")
        print(f"Prediction to check: the orphan report's residue/unreferenced rises by "
              f"{totals['files']:,} files, {gib(totals['bytes'])}.")
    else:
        print('\nNothing was written.' if args.phase == 'report' or not args.execute else '')
    return totals


def run_delete(db, args):
    projects, files = db['projects'], db['fs.files']
    repoints = read_undo(args.undo_file)
    safe, refused = delete_candidates(projects, files, repoints)
    done = set()
    if os.path.exists(args.delete_log):
        with open(args.delete_log) as handle:
            done = {json.loads(line)['id'] for line in handle if '"deleted"' in line}
    todo = [(i, n) for i, n in safe if str(i) not in done]
    total = sum(n for _, n in todo)
    estimate = DELETE_SECONDS_PER_GIB * total / 2**30 + DELETE_SECONDS_PER_FILE * len(todo)
    print(f'recorded copies: {len(set(r["old"] for r in repoints)):,}; safe to delete: {len(safe):,}; '
          f'already done by an earlier run: {len(safe) - len(todo):,}; refused: {len(refused):,}')
    for file_id, reason in list(refused.items())[:20]:
        print('  refused', file_id, reason)
    print(f'to delete now: {len(todo):,} files, {gib(total)}; estimated {estimate / 3600:,.2f} h')
    if not args.execute:
        print('\nNothing was deleted.')
        return
    started, deleted = time.time(), 0
    progress = Progress('deleting files', len(todo))
    with open(args.delete_log, 'a') as log:
        for file_id, length in todo:
            if args.budget_seconds and time.time() - started > args.budget_seconds:
                print(f'budget reached after {deleted:,} file(s); run again to resume')
                break
            delete_gridfs_file_in_batches(files, db['fs.chunks'], file_id)
            _write_line(log, {'op': 'deleted', 'id': str(file_id), 'length': length,
                              'at': datetime.datetime.utcnow().isoformat()})
            deleted += 1
            progress.tick(deleted)
    print(f'DELETED {deleted:,} file(s) in {time.time() - started:,.1f} s')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--expect-db', required=True)
    parser.add_argument('--expect-host', choices=['local', 'docdb'], required=True)
    parser.add_argument('--phase', choices=['report', 'rewrite', 'rollback', 'delete'], default='report')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--only', action='append', metavar='ID')
    parser.add_argument('--limit', type=int, metavar='N')
    parser.add_argument('--undo-file')
    parser.add_argument('--delete-log')
    parser.add_argument('--budget-seconds', type=float)
    args = parser.parse_args(argv)
    # Runs on a server go to a log file, where stdout is block-buffered: a
    # report that has run for an hour would otherwise show an empty log.
    sys.stdout.reconfigure(line_buffering=True)

    if args.phase in ('rewrite', 'rollback', 'delete') and not args.undo_file:
        parser.error(f'--phase {args.phase} needs --undo-file')
    if args.phase == 'rewrite' and args.execute and os.path.exists(args.undo_file):
        sys.exit(f'{args.undo_file} exists: it is another run\'s undo record. Choose a new name.')
    if args.phase == 'delete' and not args.delete_log:
        parser.error('--phase delete needs --delete-log (appended to; it is how a run resumes)')

    db = connect(args.expect_db, args.expect_host)
    print('mode:', 'EXECUTE' if args.execute else 'REPORT, nothing is written', '| phase:', args.phase)
    if args.execute:
        require_quiet(db)

    if args.phase in ('report', 'rewrite'):
        run_plan(db, args)
    elif args.phase == 'rollback':
        repoints = read_undo(args.undo_file)
        print(f'applied repoints in the record: {len(repoints):,}')
        if args.execute:
            restored, refused = rollback(db['projects'], db['fs.files'], repoints)
            reindex = reindexer()
            for doc_id in {r['doc'] for r in repoints}:
                reindex(doc_id)
            print(f'RESTORED {restored:,}; refused {refused:,}')
    else:
        run_delete(db, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
