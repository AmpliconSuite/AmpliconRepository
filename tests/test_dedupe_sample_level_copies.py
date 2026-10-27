"""
dedupe_sample_level_copies.py, written as attempts to make it free a file
something still needs, or collapse copies that are not the same.

The script rewrites existing prod documents and then deletes GridFS files, so
every guard it has gets a test that tries to get past it. The data lives in
uniquely-prefixed collections inside DB_NAME, dropped afterwards -- a
least-privilege user cannot create or drop a database.
"""

import gzip
import io
import json
import os
import sys
import tarfile
import uuid

import gridfs
import pytest
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dedupe_sample_level_copies as dd   # noqa: E402

pytestmark = pytest.mark.integration

LIVE_MARKS = {'delete': False, 'current': True}


class _Namespace:
    """Collections the script names (projects, fs.files, ...) under one prefix."""

    def __init__(self, database, prefix):
        self.database, self.prefix = database, prefix

    def __getitem__(self, name):
        return self.database[f'{self.prefix}{name}']

    def fs(self):
        return gridfs.GridFS(self.database, collection=f'{self.prefix}fs')


@pytest.fixture
def db():
    database = MongoClient(os.environ['DB_URI_SECRET'])[os.environ['DB_NAME']]
    prefix = f'pytest_dedupe_{uuid.uuid4().hex[:8]}_'
    yield _Namespace(database, prefix)
    for name in database.list_collection_names():
        if name.startswith(prefix):
            database.drop_collection(name)


def _put(db, data, project_id, key, filename='f', md5=None):
    fs = db.fs()
    file_id = fs.put(data, filename=filename,
                     metadata={'project_id': project_id, 'feature_key': key})
    if md5 is not None:
        db['fs.files'].update_one({'_id': file_id}, {'$set': {'md5': md5}})
    return file_id


def _tar(name, content, mtime):
    """A gzipped tar of one file; same content, different gzip/tar times."""
    buf = io.BytesIO()
    # The gzip header's timestamp is set explicitly; tarfile's 'w:gz' stamps the
    # wall clock in whole seconds, which needed a 1.1 s sleep per archive.
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=mtime) as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            info = tarfile.TarInfo(f'{name}/graph.txt')
            info.size = len(content)
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _project(db, copies=3, **extra):
    """A LIVE document: one sample of `copies` features, each with its own copy."""
    pid = ObjectId()
    cnv = [_put(db, b'chr1\t1\t2\t3\n', pid, 'CNV_BED_file') for _ in range(copies)]
    meta = [_put(db, b'{"a": 1}', pid, 'Sample_metadata_JSON') for _ in range(copies)]
    recon = [_put(db, _tar('recon', b'graph', 1000 + i), pid, 'Reconstruction_directory')
             for i in range(copies)]
    png = _put(db, b'png', pid, 'AA_PNG_file')
    features = [{'Sample_name': 'S1', 'Feature_ID': f'S1_{i}', 'CNV_BED_file': cnv[i],
                 'Sample_metadata_JSON': meta[i], 'Reconstruction_directory': recon[i],
                 'AA_directory': recon[i], 'AA_PNG_file': png}
                for i in range(copies)]
    doc = dict(LIVE_MARKS, _id=pid, project_name='p', sample_count=1,
               runs={'sample_1': features}, **extra)
    db['projects'].insert_one(doc)
    return pid, {'cnv': cnv, 'meta': meta, 'recon': recon, 'png': png}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_the_keeper_is_what_the_first_feature_names(db):
    pid, ids = _project(db)
    groups = {g['key']: g for g in dd.plan_document(db['projects'].find_one({'_id': pid}))}

    assert set(groups) == {'CNV_BED_file', 'Sample_metadata_JSON',
                           'Reconstruction_directory', 'AA_directory'}
    assert groups['CNV_BED_file']['keeper'] == ids['cnv'][0]
    assert [old for _i, old in groups['CNV_BED_file']['changes']] == ids['cnv'][1:]
    # Amplicon-level files are out of scope.
    assert 'AA_PNG_file' not in groups


def test_a_first_feature_without_a_file_is_left_alone():
    a, b = ObjectId(), ObjectId()
    doc = {'runs': {'s': [{'CNV_BED_file': 'Not Provided'}, {'CNV_BED_file': a}, {'CNV_BED_file': b}]}}
    (group,) = dd.plan_document(doc)
    assert group['skip'] == 'first feature names no file' and group['changes'] == []


def test_a_run_key_that_is_not_a_field_path_is_left_alone():
    a, b = ObjectId(), ObjectId()
    doc = {'runs': {'TCGA.01': [{'CNV_BED_file': a}, {'CNV_BED_file': b}]}}
    (group,) = dd.plan_document(doc)
    assert group['skip'] == 'run key cannot be a field path'


def test_aliases_are_not_counted_twice(db):
    """The directory aliases name the same files: freed once, not twice."""
    pid, ids = _project(db)
    doc = db['projects'].find_one({'_id': pid})
    freed = dd.freed_ids(doc, dd.plan_document(doc))
    assert freed == set(ids['cnv'][1:] + ids['meta'][1:] + ids['recon'][1:])


def test_an_id_still_named_under_another_key_is_not_freed():
    keep, other = ObjectId(), ObjectId()
    doc = {'runs': {'s': [{'CNV_BED_file': keep, 'Run_metadata_JSON': other},
                          {'CNV_BED_file': other, 'Run_metadata_JSON': other}]}}
    groups = [g for g in dd.plan_document(doc) if g['key'] == 'CNV_BED_file']
    assert dd.freed_ids(doc, groups) == set()


def test_an_id_still_named_outside_runs_is_not_freed():
    keep, old = ObjectId(), ObjectId()
    doc = {'runs': {'s': [{'CNV_BED_file': keep}, {'CNV_BED_file': old}]},
           'features_list': [{'CNV_BED_file': old}]}
    assert dd.freed_ids(doc, dd.plan_document(doc)) == set()


# ---------------------------------------------------------------------------
# Proof of identity
# ---------------------------------------------------------------------------

def test_directory_copies_differ_in_bytes_and_are_proven_by_content(db):
    pid, ids = _project(db)
    raw = {db.fs().get(i).read() for i in ids['recon']}
    assert len(raw) == 3, 'fixture should reproduce the re-gzip difference'
    proven, method, _ = dd.prove_identical(db['fs.files'], db.fs(),
                                           'Reconstruction_directory', set(ids['recon']))
    assert proven and method == 'tar-content'


def test_different_content_is_not_collapsed(db):
    pid = ObjectId()
    a = _put(db, b'chr1\t1\t2\t3\n', pid, 'CNV_BED_file')
    b = _put(db, b'chr1\t1\t2\t4\n', pid, 'CNV_BED_file')
    proven, method, detail = dd.prove_identical(db['fs.files'], db.fs(), 'CNV_BED_file', {a, b})
    assert not proven and detail == 'contents differ'


def test_equal_length_is_not_proof(db):
    pid = ObjectId()
    a = _put(db, b'AAAA', pid, 'CNV_BED_file')
    b = _put(db, b'BBBB', pid, 'CNV_BED_file')
    assert not dd.prove_identical(db['fs.files'], db.fs(), 'CNV_BED_file', {a, b})[0]


def test_a_missing_copy_is_not_collapsed(db):
    pid = ObjectId()
    a = _put(db, b'x', pid, 'CNV_BED_file')
    proven, _m, detail = dd.prove_identical(db['fs.files'], db.fs(), 'CNV_BED_file', {a, ObjectId()})
    assert not proven and 'no fs.files row' in detail


def test_matching_md5_is_proof_without_reading(db, monkeypatch):
    pid = ObjectId()
    a = _put(db, b'x', pid, 'CNV_BED_file', md5='abc')
    b = _put(db, b'x', pid, 'CNV_BED_file', md5='abc')

    class NoReads:
        def get(self, _):
            raise AssertionError('md5 agreement should not read the files')
    assert dd.prove_identical(db['fs.files'], NoReads(), 'CNV_BED_file', {a, b}) == (True, 'md5', None)


def test_an_id_shared_with_another_document_is_found(db):
    pid, ids = _project(db)
    db['projects'].insert_one({'_id': ObjectId(), 'runs': {'x': [{'CNV_BED_file': ids['cnv'][1]}]}})
    shared = dd.named_by_other_documents(db['projects'], set(ids['cnv']))
    assert set(shared) == {ids['cnv'][1]}


def test_a_tombstones_pending_purge_counts_as_naming(db):
    pid, ids = _project(db)
    db['projects'].insert_one({'_id': ObjectId(), dd.PENDING_PAYLOAD_KEY: [ids['meta'][2]]})
    assert set(dd.named_by_other_documents(db['projects'], set(ids['meta']))) == {ids['meta'][2]}


# ---------------------------------------------------------------------------
# Rewrite and rollback
# ---------------------------------------------------------------------------

def _rewrite(db, pid, tmp_path):
    doc = db['projects'].find_one({'_id': pid})
    groups = dd.plan_document(doc)
    undo_path = tmp_path / 'undo.jsonl'
    with open(undo_path, 'x') as undo:
        applied = dd.rewrite_sample(db['projects'], pid, 'sample_1', groups, undo)
    return applied, undo_path


def test_rewrite_points_every_feature_at_the_keeper(db, tmp_path):
    pid, ids = _project(db)
    applied, undo_path = _rewrite(db, pid, tmp_path)

    features = db['projects'].find_one({'_id': pid})['runs']['sample_1']
    assert applied
    assert {f['CNV_BED_file'] for f in features} == {ids['cnv'][0]}
    assert {f['AA_directory'] for f in features} == {ids['recon'][0]}
    assert {f['AA_PNG_file'] for f in features} == {ids['png']}
    assert len(dd.read_undo(undo_path)) == 8      # 2 changes x 4 keys


def test_a_sample_changed_underneath_is_skipped_not_overwritten(db, tmp_path):
    pid, ids = _project(db)
    doc = db['projects'].find_one({'_id': pid})
    groups = dd.plan_document(doc)
    edited = ObjectId()
    db['projects'].update_one({'_id': pid}, {'$set': {'runs.sample_1.2.CNV_BED_file': edited}})
    with open(tmp_path / 'u.jsonl', 'x') as undo:
        assert not dd.rewrite_sample(db['projects'], pid, 'sample_1', groups, undo)

    features = db['projects'].find_one({'_id': pid})['runs']['sample_1']
    assert features[1]['CNV_BED_file'] == ids['cnv'][1]       # nothing written
    assert features[2]['CNV_BED_file'] == edited
    assert dd.read_undo(tmp_path / 'u.jsonl') == []          # a missed sample is not undone


def test_a_large_sample_is_rewritten_in_batches(db, tmp_path, monkeypatch):
    monkeypatch.setattr(dd, 'MAX_CHANGES_PER_UPDATE', 3)
    pid, ids = _project(db)
    before = db['projects'].find_one({'_id': pid})['runs']
    applied, undo_path = _rewrite(db, pid, tmp_path)

    outcomes = [json.loads(line) for line in open(undo_path) if '"repoint"' not in line]
    assert applied and [o['changes'] for o in outcomes] == [3, 3, 2]
    assert {f['CNV_BED_file'] for f in db['projects'].find_one({'_id': pid})['runs']['sample_1']} == {ids['cnv'][0]}
    assert dd.rollback(db['projects'], db['fs.files'], dd.read_undo(undo_path)) == (8, 0)
    assert db['projects'].find_one({'_id': pid})['runs'] == before


def test_a_batch_that_misses_stops_the_sample(db, tmp_path, monkeypatch):
    monkeypatch.setattr(dd, 'MAX_CHANGES_PER_UPDATE', 3)
    pid, _ids = _project(db)
    groups = dd.plan_document(db['projects'].find_one({'_id': pid}))
    last_key, (last_index, _old) = groups[-1]['key'], groups[-1]['changes'][-1]
    db['projects'].update_one({'_id': pid}, {'$set': {f'runs.sample_1.{last_index}.{last_key}': ObjectId()}})
    with open(tmp_path / 'u.jsonl', 'x') as undo:
        assert not dd.rewrite_sample(db['projects'], pid, 'sample_1', groups, undo)

    outcomes = [json.loads(line)['op'] for line in open(tmp_path / 'u.jsonl') if '"repoint"' not in line]
    assert outcomes == ['applied', 'applied', 'missed']
    assert len(dd.read_undo(tmp_path / 'u.jsonl')) == 6       # only the applied batches


def test_rollback_restores_exactly(db, tmp_path):
    pid, _ids = _project(db)
    before = db['projects'].find_one({'_id': pid})['runs']
    _applied, undo_path = _rewrite(db, pid, tmp_path)

    restored, refused = dd.rollback(db['projects'], db['fs.files'], dd.read_undo(undo_path))
    assert (restored, refused) == (8, 0)
    assert db['projects'].find_one({'_id': pid})['runs'] == before


def test_rollback_refuses_an_id_already_deleted(db, tmp_path):
    pid, ids = _project(db)
    _applied, undo_path = _rewrite(db, pid, tmp_path)
    db.fs().delete(ids['cnv'][1])

    restored, refused = dd.rollback(db['projects'], db['fs.files'], dd.read_undo(undo_path))
    assert refused == 1 and restored == 7
    assert db['projects'].find_one({'_id': pid})['runs']['sample_1'][1]['CNV_BED_file'] == ids['cnv'][0]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_takes_only_what_the_record_freed(db, tmp_path):
    pid, ids = _project(db)
    _applied, undo_path = _rewrite(db, pid, tmp_path)
    safe, refused = dd.delete_candidates(db['projects'], db['fs.files'], dd.read_undo(undo_path))

    assert {i for i, _n in safe} == set(ids['cnv'][1:] + ids['meta'][1:] + ids['recon'][1:])
    assert ids['png'] not in {i for i, _n in safe} and not refused


def test_delete_refuses_a_copy_something_names_again(db, tmp_path):
    pid, ids = _project(db)
    _applied, undo_path = _rewrite(db, pid, tmp_path)
    db['projects'].insert_one({'_id': ObjectId(), 'runs': {'x': [{'CNV_BED_file': ids['cnv'][2]}]}})

    safe, refused = dd.delete_candidates(db['projects'], db['fs.files'], dd.read_undo(undo_path))
    assert ids['cnv'][2] not in {i for i, _n in safe}
    assert refused[str(ids['cnv'][2])] == 'still named by a document'


def test_delete_refuses_a_backlink_to_another_document(db, tmp_path):
    pid, ids = _project(db)
    _applied, undo_path = _rewrite(db, pid, tmp_path)
    db['fs.files'].update_one({'_id': ids['meta'][1]}, {'$set': {'metadata.project_id': ObjectId()}})

    safe, refused = dd.delete_candidates(db['projects'], db['fs.files'], dd.read_undo(undo_path))
    assert refused[str(ids['meta'][1])] == 'backlink names another document'


def test_delete_refuses_a_copy_the_same_document_names_outside_runs(db, tmp_path):
    """caper-dev, 2026-10-14: a 2023 document's top-level features_list repeated
    its feature rows, so 9,036 old ids stayed named after runs was rewritten."""
    pid, ids = _project(db)
    _applied, undo_path = _rewrite(db, pid, tmp_path)
    db['projects'].update_one({'_id': pid},
                              {'$set': {'features_list': [{'CNV_BED_file': ids['cnv'][1]}]}})

    safe, refused = dd.delete_candidates(db['projects'], db['fs.files'], dd.read_undo(undo_path))
    assert ids['cnv'][1] not in {i for i, _n in safe}
    assert refused[str(ids['cnv'][1])] == 'still named by a document'


def test_the_report_does_not_count_a_copy_the_document_names_outside_runs(db, monkeypatch):
    """prod, 2026-10-15: the report counted 60 ids as freed that a legacy
    features_list still named; the delete phase would have refused them."""
    import gridfs as gridfs_module
    from types import SimpleNamespace
    pid, ids = _project(db)
    db['projects'].update_one({'_id': pid},
                              {'$set': {'features_list': [{'CNV_BED_file': ids['cnv'][1]}]}})
    bucket = db.fs()
    monkeypatch.setattr(gridfs_module, 'GridFS', lambda *_a, **_k: bucket)
    args = SimpleNamespace(phase='report', execute=False, only=[str(pid)], limit=None, undo_file=None)

    totals = dd.run_plan(db, args)

    # cnv, meta and the shared directory each free two copies: 6, less the one
    # features_list still names.
    assert totals['files'] == 5


# ---------------------------------------------------------------------------
# The command line's own guards
# ---------------------------------------------------------------------------

def test_an_existing_undo_file_is_never_overwritten(tmp_path, monkeypatch):
    undo = tmp_path / 'undo.jsonl'
    undo.write_text('{"op": "repoint"}\n')
    monkeypatch.setenv('DB_NAME', 'caper-dev')
    with pytest.raises(SystemExit) as stop:
        dd.main(['--expect-db', 'caper-dev', '--expect-host', 'local', '--phase', 'rewrite',
                 '--undo-file', str(undo), '--execute'])
    assert 'another run' in str(stop.value)
    assert undo.read_text() == '{"op": "repoint"}\n'


def test_writing_needs_shutdown_mode(db):
    db['system_settings'].insert_one({'_id': 'system_flags', 'shutdown_pending': False})
    with pytest.raises(SystemExit) as stop:
        dd.require_quiet(db)
    assert 'shutdown mode is off' in str(stop.value)


def test_the_wrong_database_reads_nothing(monkeypatch):
    monkeypatch.setenv('DB_NAME', 'caper-dev')
    with pytest.raises(SystemExit) as stop:
        dd.connect('caper', 'docdb')
    assert 'nothing read' in str(stop.value)
