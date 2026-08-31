"""Tests for dump_metadata.py, the tier-3 off-AWS metadata copy.

Run against the local MongoDB in a scratch collection namespace, the same way
the other database-touching tests here do -- no scratch *database*, because
the deployed application users are scoped to one database and cannot create
another.
"""

import gzip
import json
import os
import subprocess
import sys
import uuid

import pytest
from bson import ObjectId, json_util

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import dump_metadata  # noqa: E402


@pytest.fixture
def scratch_db():
    """A real database handle whose scratch collections are dropped after."""
    from caper.utils import db_handle
    created = []

    class Namespaced:
        """Presents scratch collections under ordinary-looking names."""
        def __init__(self, db, suffix):
            self._db = db
            self._suffix = suffix
            self.name = db.name

        def _real(self, name):
            return '%s_%s' % (name.replace('.', '_'), self._suffix)

        def __getitem__(self, name):
            # dump_collection() is handed names that came back from
            # list_collection_names(), which are already real. Only map a name
            # that has not been mapped yet, or the suffix gets applied twice.
            real = name if name.endswith('_' + self._suffix) else self._real(name)
            created.append(real)
            return self._db[real]

        def list_collection_names(self):
            return [n for n in self._db.list_collection_names()
                    if n.endswith('_' + self._suffix)]

        def command(self, *a, **kw):
            return self._db.command(*a, **kw)

    ns = Namespaced(db_handle, uuid.uuid4().hex[:8])
    try:
        yield ns
    finally:
        for name in set(created) | set(ns.list_collection_names()):
            db_handle[name].drop()


@pytest.mark.integration
def test_the_payload_collection_is_excluded():
    """The whole point of the tool: 361.90 GiB stays where it is."""
    class FakeDB:
        def list_collection_names(self):
            return ['projects', 'fs.files', 'fs.chunks', 'project_audit_log']

    names = dump_metadata.collections_to_dump(FakeDB())
    assert 'fs.chunks' not in names
    assert names == ['fs.files', 'project_audit_log', 'projects']


@pytest.mark.integration
def test_collections_are_discovered_not_listed():
    """A collection added later must be included without editing this file."""
    class FakeDB:
        def list_collection_names(self):
            return ['projects', 'fs.chunks', 'something_invented_next_year']

    assert 'something_invented_next_year' in dump_metadata.collections_to_dump(FakeDB())


@pytest.mark.integration
def test_object_ids_and_dates_survive_the_round_trip(scratch_db, tmp_path):
    """A dump that stringifies an ObjectId cannot be restored from."""
    import datetime
    oid = ObjectId()
    when = datetime.datetime(2026, 8, 31, 12, 0, 0)
    scratch_db['projects'].insert_one(
        {'_id': oid, 'project_name': 'round trip', 'created': when,
         'tarfile': ObjectId()})

    out = str(tmp_path)
    count, size, digest = dump_metadata.dump_collection(
        scratch_db, scratch_db._real('projects'), out)
    assert count == 1

    path = os.path.join(out, '%s.jsonl.gz' % scratch_db._real('projects'))
    with gzip.open(path, 'rb') as f:
        doc = json_util.loads(f.readline().decode('utf-8'))
    assert doc['_id'] == oid and isinstance(doc['_id'], ObjectId)
    assert doc['created'] == when
    assert isinstance(doc['tarfile'], ObjectId)


@pytest.mark.integration
def test_verify_catches_corruption_and_truncation(scratch_db, tmp_path):
    """The two ways a dump goes wrong fail differently, so both are checked."""
    for i in range(5):
        scratch_db['projects'].insert_one({'n': i})
    real = scratch_db._real('projects')
    out = str(tmp_path / 'dump')
    os.makedirs(out)
    count, size, digest = dump_collection = dump_metadata.dump_collection(
        scratch_db, real, out)

    manifest = {'database': 'x', 'taken': 'y', 'excluded': ['fs.chunks'],
                'collections': {real: {'documents': count, 'bytes': size,
                                       'sha256': digest}}}
    mpath = os.path.join(out, dump_metadata.MANIFEST_NAME)
    with open(mpath, 'w') as f:
        json.dump(manifest, f)

    assert dump_metadata.verify(out) == []

    # Corruption: same length, different bytes.
    path = os.path.join(out, '%s.jsonl.gz' % real)
    data = bytearray(open(path, 'rb').read())
    data[-1] ^= 0xFF
    open(path, 'wb').write(bytes(data))
    problems = dump_metadata.verify(out)
    assert problems and 'sha256 mismatch' in problems[0]

    # Truncation: rewrite honestly, then lie about the count in the manifest.
    dump_metadata.dump_collection(scratch_db, real, out)
    _, size2, digest2 = None, os.path.getsize(path), None
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    manifest['collections'][real] = {'documents': 99, 'bytes': size2,
                                     'sha256': h.hexdigest()}
    with open(mpath, 'w') as f:
        json.dump(manifest, f)
    problems = dump_metadata.verify(out)
    assert problems and 'manifest says 99' in problems[0]


@pytest.mark.integration
def test_it_refuses_a_database_it_was_not_pointed_at():
    """dev and prod share a cluster, so the name is asserted, never trusted."""
    target = os.path.join(os.environ.get('CLAUDE_JOB_DIR', '/tmp'),
                          'should-not-exist-%s' % uuid.uuid4().hex[:8])
    out = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, 'dump_metadata.py'),
         '--expect-db', 'definitely-not-this-one', '--out', target],
        capture_output=True, text=True)
    assert out.returncode == 2, out.stdout
    assert '--expect-db says' in out.stdout
    # And it must not have written anything before deciding.
    assert not os.path.exists(target), 'created a dump directory it should have refused'


@pytest.mark.integration
def test_expect_db_selects_nothing():
    """--expect-db must not be usable as the instruction of where to connect.

    If it were, the guard would agree with itself for any value: the tool would
    read whatever it was told to read and then confirm that it had. The real
    target comes from DB_NAME; --expect-db only says whether that was intended.
    """
    source = open(os.path.join(REPO_ROOT, 'dump_metadata.py')).read()
    body = source[source.index('def main('):]
    assert 'client[configured]' in body
    assert 'client[args.expect_db' not in body


@pytest.mark.integration
def test_there_is_no_write_path():
    """Read-only is a property of the file, so it is checked as one."""
    source = open(os.path.join(REPO_ROOT, 'dump_metadata.py')).read()
    for forbidden in ('insert_one', 'insert_many', 'update_one', 'update_many',
                      'delete_one', 'delete_many', 'drop(', 'replace_one'):
        assert forbidden not in source, (
            '%s appears in a tool documented as read-only' % forbidden)
