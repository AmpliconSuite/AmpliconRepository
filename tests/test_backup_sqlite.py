"""Tests for backup_sqlite.py.

These need no MongoDB, no AWS and no Django: the script is deliberately
runnable when the application cannot start, because that is when a backup
matters most, and the tests hold that property by never importing Django.

The test that matters most is the one asserting the live database is never
modified. Everything else this script does is a read or a write to a temporary
file; there is exactly one destructive statement in it, and it has to land on a
copy.
"""

import gzip
import os
import sqlite3
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import backup_sqlite  # noqa: E402


def _make_db(path, sessions=5, users=3):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE django_session (session_key TEXT PRIMARY KEY, '
                 'session_data TEXT, expire_date TEXT)')
    conn.execute('CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username TEXT)')
    for i in range(sessions):
        conn.execute('INSERT INTO django_session VALUES (?,?,?)',
                     ('key%d' % i, 'x' * 2000, '2026-01-01'))
    for i in range(users):
        conn.execute('INSERT INTO auth_user VALUES (?,?)', (i, 'user%d' % i))
    conn.commit()
    conn.close()
    return path


@pytest.mark.integration
def test_sessions_are_dropped_and_users_survive(tmp_path):
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    copy = str(tmp_path / 'copy.sqlite3')

    backup_sqlite.snapshot(src, copy)
    rows, before, after = backup_sqlite.strip_sessions(copy, src)

    assert rows == 5
    conn = sqlite3.connect(copy)
    assert conn.execute('SELECT COUNT(*) FROM django_session').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0] == 3
    conn.close()
    assert after < before, 'VACUUM INTO should shrink the file'


@pytest.mark.integration
def test_the_live_database_is_never_modified(tmp_path):
    """The one destructive statement must land on a copy.

    Checked two ways: the guard rejects the source path outright, and a full
    run leaves the original's session rows and byte size untouched.
    """
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    size_before = os.path.getsize(src)

    with pytest.raises(RuntimeError, match='refusing to modify the live database'):
        backup_sqlite.strip_sessions(src, src)

    copy = str(tmp_path / 'copy.sqlite3')
    backup_sqlite.snapshot(src, copy)
    backup_sqlite.strip_sessions(copy, src)

    conn = sqlite3.connect(src)
    assert conn.execute('SELECT COUNT(*) FROM django_session').fetchone()[0] == 5
    conn.close()
    assert os.path.getsize(src) == size_before


@pytest.mark.integration
def test_the_guard_sees_through_a_symlink(tmp_path):
    """Comparing paths as strings would let a symlink past."""
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    link = str(tmp_path / 'link.sqlite3')
    os.symlink(src, link)
    with pytest.raises(RuntimeError):
        backup_sqlite.strip_sessions(link, src)


@pytest.mark.integration
def test_snapshot_copies_a_database_that_is_open_for_writing(tmp_path):
    """The online backup API is the reason this is safe on a live site."""
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    writer = sqlite3.connect(src)
    writer.execute("INSERT INTO auth_user VALUES (99, 'late')")
    writer.commit()

    copy = str(tmp_path / 'copy.sqlite3')
    backup_sqlite.snapshot(src, copy)
    writer.close()

    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT username FROM auth_user WHERE id=99").fetchone()[0] == 'late'
    conn.close()


@pytest.mark.integration
def test_identical_content_hashes_and_compresses_identically(tmp_path):
    """Change-detection depends on both halves of this.

    The hash is taken on the uncompressed file, so it is not affected by gzip
    framing -- but gzip stores an mtime, so two archives of unchanged content
    would otherwise differ byte-for-byte and mislead anyone comparing objects.
    """
    a = _make_db(str(tmp_path / 'a.sqlite3'), sessions=0)
    b = _make_db(str(tmp_path / 'b.sqlite3'), sessions=0)
    assert backup_sqlite.content_hash(a) == backup_sqlite.content_hash(b)

    ga, gb = str(tmp_path / 'a.gz'), str(tmp_path / 'b.gz')
    backup_sqlite.compress(a, ga)
    backup_sqlite.compress(b, gb)
    assert open(ga, 'rb').read() == open(gb, 'rb').read()
    with gzip.open(ga, 'rb') as f:
        assert f.read() == open(a, 'rb').read()


@pytest.mark.integration
def test_default_db_path_follows_caper_root(monkeypatch):
    monkeypatch.setenv('CAPER_ROOT', '/somewhere/repo')
    assert backup_sqlite.default_db_path() == '/somewhere/repo/caper/caper.sqlite3'
    monkeypatch.delenv('CAPER_ROOT')
    assert backup_sqlite.default_db_path().endswith('caper/caper.sqlite3')


@pytest.mark.integration
def test_reports_by_default_and_refuses_to_guess_the_environment(tmp_path):
    """Two failures of the old script, held by tests.

    Defect 5 was an S3 path hardcoded to 'prod/' while dev's config said
    'dev' -- had it ever worked, dev would have overwritten prod's backups. So
    an unset environment is an error, never a default.
    """
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    env = dict(os.environ)
    env.pop('AMPLICON_ENV', None)

    out = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, 'backup_sqlite.py'), '--db', src],
        capture_output=True, text=True, env=env)
    assert out.returncode == 2
    assert 'Refusing to guess' in out.stdout

    out = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, 'backup_sqlite.py'),
         '--db', src, '--env', 'dev'],
        capture_output=True, text=True, env=env)
    assert out.returncode == 0
    assert 'REPORT ONLY' in out.stdout
    assert 'dev/sqlite/' in out.stdout


@pytest.mark.integration
def test_the_environment_names_the_prefix(tmp_path):
    """dev must never write into prod's prefix -- the old script's defect 5."""
    src = _make_db(str(tmp_path / 'live.sqlite3'))
    for env_name in ('dev', 'prod'):
        out = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, 'backup_sqlite.py'),
             '--db', src, '--env', env_name],
            capture_output=True, text=True)
        assert '%s/sqlite/' % env_name in out.stdout
        other = 'prod' if env_name == 'dev' else 'dev'
        assert '%s/sqlite/' % other not in out.stdout


@pytest.mark.integration
def test_end_to_end_local_backup_is_restorable(tmp_path):
    """The point of the whole script: what lands on disk must open and read.

    Counting bytes is not the same as having a database, which is the same
    mistake the cluster-snapshot posture made until someone restored one.
    """
    src = _make_db(str(tmp_path / 'live.sqlite3'), sessions=50, users=7)
    keep = str(tmp_path / 'out')
    out = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, 'backup_sqlite.py'),
         '--db', src, '--env', 'dev', '--execute', '--no-upload',
         '--keep-local', keep],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr

    produced = [os.path.join(keep, n) for n in os.listdir(keep)]
    assert len(produced) == 1
    restored = str(tmp_path / 'restored.sqlite3')
    with gzip.open(produced[0], 'rb') as f, open(restored, 'wb') as g:
        g.write(f.read())

    conn = sqlite3.connect(restored)
    assert conn.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0] == 7
    assert conn.execute('SELECT COUNT(*) FROM django_session').fetchone()[0] == 0
    conn.close()


class _FakeS3:
    """Just enough S3 to exercise latest_uploaded_hash()."""

    def __init__(self, contents, head=None, head_raises=None):
        self._contents = contents
        self._head = head or {}
        self._raises = head_raises
        self.headed = []

    def get_paginator(self, _name):
        contents = self._contents

        class P:
            def paginate(self, **kw):
                return [{'Contents': contents}]
        return P()

    def head_object(self, Bucket, Key):
        self.headed.append(Key)
        if self._raises:
            raise self._raises
        return self._head


@pytest.mark.integration
def test_folder_markers_are_not_mistaken_for_backups():
    """`s3://amprepo-backups/` has held zero-byte markers since 2025-08-08.

    They list like objects. Heading one was what broke the first real run.
    """
    import datetime
    s3 = _FakeS3([
        {'Key': 'prod/sqlite/', 'Size': 0,
         'LastModified': datetime.datetime(2026, 1, 1)},
    ])
    assert backup_sqlite.latest_uploaded_hash(s3, 'b', 'prod/sqlite/') is None
    assert s3.headed == [], 'a folder marker must never be headed'


@pytest.mark.integration
def test_an_unreadable_previous_backup_means_upload_not_crash(capsys):
    """Change detection is an optimisation; it must never destroy the backup.

    The first real run built the whole thing and then died on a 403 from
    HeadObject, which is the worst possible ordering.
    """
    import datetime

    class Denied(Exception):
        pass

    s3 = _FakeS3(
        [{'Key': 'prod/sqlite/caper-x.sqlite3.gz', 'Size': 100,
          'LastModified': datetime.datetime(2026, 1, 1)}],
        head_raises=Denied('403 Forbidden'))
    assert backup_sqlite.latest_uploaded_hash(s3, 'b', 'prod/sqlite/') is None
    assert 'cannot skip' in capsys.readouterr().out


@pytest.mark.integration
def test_a_matching_hash_is_reported_so_the_run_can_skip():
    import datetime
    s3 = _FakeS3(
        [{'Key': 'prod/sqlite/caper-x.sqlite3.gz', 'Size': 100,
          'LastModified': datetime.datetime(2026, 1, 1)},
         {'Key': 'prod/sqlite/caper-y.sqlite3.gz', 'Size': 100,
          'LastModified': datetime.datetime(2026, 6, 1)}],
        head={'Metadata': {backup_sqlite.HASH_METADATA_KEY: 'abc123'}})
    assert backup_sqlite.latest_uploaded_hash(s3, 'b', 'prod/sqlite/') == 'abc123'
    assert s3.headed == ['prod/sqlite/caper-y.sqlite3.gz'], 'must head the newest'
