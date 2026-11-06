"""Tests for ops_lock.py, against scratch collections in the configured database."""

import datetime
import multiprocessing
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid

import pytest
from django.test import RequestFactory
from pymongo import MongoClient

import ops_lock
from caper import context_processor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client():
    uri, db_name = os.getenv('DB_URI_SECRET'), os.getenv('DB_NAME')
    if not uri or not db_name:
        pytest.skip('DB_URI_SECRET and DB_NAME must both be set')
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command('ping')
    except Exception as exc:
        pytest.skip('no MongoDB available: %s' % exc)
    return client, db_name


@pytest.fixture
def cols():
    client, db_name = _client()
    suffix = uuid.uuid4().hex[:8]
    settings_col = client[db_name]['ops_lock_settings_test_%s' % suffix]
    log_col = client[db_name]['ops_lock_log_test_%s' % suffix]
    try:
        yield settings_col, log_col
    finally:
        settings_col.drop()
        log_col.drop()


@pytest.fixture
def lock(cols):
    return ops_lock.OpsLock(*cols)


def events(log_col):
    return [entry['event'] for entry in log_col.find().sort('_id', 1)]


def test_acquire_on_an_empty_lock_succeeds_and_starts_the_token(lock, cols):
    doc, event = lock.acquire('a', 'owner', 'why', 60, 'host')
    assert event == 'acquire' and doc['token'] == 1
    stored = lock.read()
    assert (stored['holder'], stored['owner'], stored['reason'], stored['token']) == ('a', 'owner', 'why', 1)
    assert events(cols[1]) == ['acquire']


def test_acquire_after_release_increments_the_token(lock):
    lock.acquire('a', 'o', 'r', 60)
    lock.release('a')
    assert lock.read()['holder'] is None
    assert lock.read()['previous_holder'] == 'a'
    assert lock.acquire('b', 'o', 'r', 60)[0]['token'] == 2


def test_acquire_against_another_holder_is_refused_and_writes_nothing(lock, cols):
    lock.acquire('a', 'o', 'r', 60)
    before, log_before = lock.read(), events(cols[1])
    with pytest.raises(ops_lock.LockHeld) as refused:
        lock.acquire('b', 'o', 'r', 60)
    assert refused.value.doc['holder'] == 'a'
    assert lock.read() == before and events(cols[1]) == log_before


def test_reacquiring_as_the_holder_renews_and_keeps_the_token(lock, cols):
    lock.acquire('a', 'o', 'r', 60)
    doc, event = lock.acquire('a', 'o', 'later step', 120)
    assert event == 'renew' and doc['token'] == 1 and doc['reason'] == 'later step'
    assert events(cols[1]) == ['acquire']


def test_an_expired_lease_is_taken_and_the_takeover_is_logged(lock, cols):
    lock.acquire('a', 'o', 'r', 1)
    time.sleep(1.1)
    assert lock.is_free()
    doc, event = lock.acquire('b', 'o', 'r', 60)
    assert event == 'expired_takeover' and doc['token'] == 2
    entry = cols[1].find_one({'event': 'expired_takeover'})
    assert entry['previous_holder'] == 'a' and entry['holder'] == 'b'


def _contend(holder, uri, db_name, settings_name, log_name, results):
    client = MongoClient(uri)
    lock = ops_lock.OpsLock(client[db_name][settings_name], client[db_name][log_name])
    try:
        lock.acquire(holder, 'o', 'r', 60)
        results.put('won')
    except ops_lock.LockHeld:
        results.put('held')


@pytest.mark.parametrize('released_first', [False, True])
def test_concurrent_acquires_have_exactly_one_winner(lock, cols, released_first):
    if released_first:
        lock.acquire('earlier', 'o', 'r', 60)
        lock.release('earlier')
    results = multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_contend, args=(
        'w%d' % i, os.environ['DB_URI_SECRET'], os.environ['DB_NAME'],
        cols[0].name, cols[1].name, results)) for i in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    outcomes = [results.get() for _ in workers]
    assert outcomes.count('won') == 1 and outcomes.count('held') == 5


def test_release_and_renew_are_refused_for_anyone_but_the_holder(lock, cols):
    lock.acquire('a', 'o', 'r', 60)
    with pytest.raises(ops_lock.LockNotHeld):
        lock.release('b')
    with pytest.raises(ops_lock.LockNotHeld):
        lock.renew('b', 60)
    assert lock.read()['holder'] == 'a'
    assert 'renew_failed' in events(cols[1])


def test_break_frees_the_lock_and_logs_who_and_why(lock, cols):
    lock.acquire('a', 'o', 'r', 60)
    assert lock.break_lock('z', 'a is gone')['holder'] == 'a'
    assert lock.is_free()
    entry = cols[1].find_one({'event': 'break'})
    assert (entry['holder'], entry['reason'], entry['previous_holder']) == ('z', 'a is gone', 'a')
    assert lock.break_lock('z', 'again') is None


def test_wait_returns_when_free_and_times_out_when_held(lock, capsys):
    assert ops_lock.cmd_wait(lock, timeout=0, interval=0) == 0
    lock.acquire('a', 'o', 'r', 60)
    assert ops_lock.cmd_wait(lock, timeout=0, interval=0) == 1
    assert 'TIMED OUT' in capsys.readouterr().out
    assert lock.read()['holder'] == 'a'


@pytest.mark.parametrize('text, seconds', [('90s', 90), ('15m', 900), ('1h', 3600), ('60', 60), ('6h', 21600)])
def test_ttl_parsing(text, seconds):
    assert ops_lock.parse_ttl(text) == seconds


@pytest.mark.parametrize('text', ['7h', '0', 'soon', '-1'])
def test_ttl_over_the_maximum_or_malformed_is_refused(text):
    with pytest.raises(Exception):
        ops_lock.parse_ttl(text)


def test_cli_ttl_over_the_maximum_exits_2():
    with pytest.raises(SystemExit) as stopped:
        ops_lock.main(['--expect-db', 'x', 'acquire', '--holder', 'a', '--owner', 'o',
                       '--reason', 'r', '--ttl', '7h'])
    assert stopped.value.code == 2


def test_expect_db_mismatch_aborts_before_any_read(monkeypatch):
    monkeypatch.setenv('DB_NAME', 'caper-dev')
    monkeypatch.setattr(ops_lock, 'MongoClient', lambda *a, **k: pytest.fail('connected'))
    with pytest.raises(SystemExit) as stopped:
        ops_lock.main(['--expect-db', 'caper', 'status'])
    assert 'expected' in str(stopped.value.code)


class _Args:
    def __init__(self, holder='runner', ttl=60):
        self.holder, self.owner, self.reason, self.ttl = holder, 'o', 'r', ttl


def test_run_passes_the_identity_and_releases_on_success(lock, tmp_path):
    out = tmp_path / 'env'
    status = ops_lock.cmd_run(lock, _Args(), [
        sys.executable, '-c',
        "import os,sys; open(sys.argv[1],'w').write(os.environ['OPS_LOCK_HOLDER']+' '+os.environ['OPS_LOCK_TOKEN'])",
        str(out)])
    assert status == 0
    assert out.read_text() == 'runner 1'
    assert lock.read()['holder'] is None


def test_run_returns_the_commands_failure_and_still_releases(lock):
    assert ops_lock.cmd_run(lock, _Args(), [sys.executable, '-c', 'raise SystemExit(7)']) == 7
    assert lock.read()['holder'] is None


def test_run_refuses_a_held_lock_without_starting_the_command(lock, tmp_path):
    lock.acquire('a', 'o', 'r', 60)
    marker = tmp_path / 'ran'
    status = ops_lock.cmd_run(lock, _Args(), [sys.executable, '-c', 'open(%r, "w")' % str(marker)])
    assert status == 1 and not marker.exists()


def test_run_renews_while_the_command_runs(lock):
    assert ops_lock.cmd_run(lock, _Args(ttl=2), [sys.executable, '-c', 'import time; time.sleep(3)']) == 0
    assert lock.read()['holder'] is None
    assert 'renew_failed' not in events(lock.log_col)
    assert 'expired_takeover' not in events(lock.log_col)


def test_run_exits_nonzero_if_the_lease_was_lost_and_lets_the_command_finish(lock, tmp_path):
    marker = tmp_path / 'finished'
    command = [sys.executable, '-c',
               'import sys,time; time.sleep(2); open(sys.argv[1], "w")', str(marker)]
    original = lock.acquire

    def acquire_then_lose(*args, **kwargs):
        result = original(*args, **kwargs)
        lock.settings.update_one({'_id': ops_lock.LOCK_ID}, {'$set': {'holder': 'someone else'}})
        return result

    lock.acquire = acquire_then_lose
    status = ops_lock.cmd_run(lock, _Args(ttl=2), command)
    assert status == 1 and marker.exists()
    assert lock.read()['holder'] == 'someone else'


def test_run_passes_sigterm_to_the_command_and_releases(cols, tmp_path):
    ready = tmp_path / 'ready'
    script = textwrap.dedent('''
        import os, sys
        from pymongo import MongoClient
        import ops_lock
        db = MongoClient(os.environ['DB_URI_SECRET'])[os.environ['DB_NAME']]
        lock = ops_lock.OpsLock(db[sys.argv[1]], db[sys.argv[2]])
        class Args:
            holder, owner, reason, ttl = 'runner', 'o', 'r', 60
        child = ("import sys,time,pathlib; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)")
        sys.exit(ops_lock.cmd_run(lock, Args, [sys.executable, '-c', child, sys.argv[3]]))
    ''')
    process = subprocess.Popen([sys.executable, '-c', script, cols[0].name, cols[1].name, str(ready)],
                               cwd=REPO_ROOT, stdout=subprocess.DEVNULL)
    deadline = time.time() + 30
    while not ready.exists():
        assert time.time() < deadline, 'the command never started'
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) == 128 + signal.SIGTERM
    assert cols[0].find_one({'_id': ops_lock.LOCK_ID})['holder'] is None


@pytest.fixture
def scratch_db(monkeypatch):
    """The configured database, with require() pointed at scratch collections."""
    client, db_name = _client()
    suffix = uuid.uuid4().hex[:8]
    monkeypatch.setattr(ops_lock, 'SETTINGS_COLLECTION', 'ops_lock_settings_test_%s' % suffix)
    monkeypatch.setattr(ops_lock, 'LOG_COLLECTION', 'ops_lock_log_test_%s' % suffix)
    monkeypatch.delenv('OPS_LOCK_HOLDER', raising=False)
    monkeypatch.delenv('OPS_LOCK_TOKEN', raising=False)
    db = client[db_name]
    try:
        yield db
    finally:
        db[ops_lock.SETTINGS_COLLECTION].drop()
        db[ops_lock.LOG_COLLECTION].drop()


def _hold(db, holder='runner', ttl=60):
    return ops_lock.OpsLock(db[ops_lock.SETTINGS_COLLECTION], db[ops_lock.LOG_COLLECTION]).acquire(
        holder, 'o', 'r', ttl)[0]


def test_require_passes_for_the_holder(scratch_db, monkeypatch, capsys):
    doc = _hold(scratch_db)
    monkeypatch.setenv('OPS_LOCK_HOLDER', 'runner')
    monkeypatch.setenv('OPS_LOCK_TOKEN', str(doc['token']))
    monkeypatch.setattr(ops_lock, '_is_shared', lambda db: True)
    ops_lock.require(scratch_db, 'tool --execute')
    assert capsys.readouterr().out == ''


def test_require_refuses_on_a_shared_database_without_the_lock(scratch_db, monkeypatch, capsys):
    monkeypatch.setattr(ops_lock, '_is_shared', lambda db: True)
    with pytest.raises(SystemExit) as stopped:
        ops_lock.require(scratch_db, 'tool --execute')
    assert stopped.value.code == 1
    assert 'REFUSED' in capsys.readouterr().out


def test_require_refuses_someone_elses_lock(scratch_db, monkeypatch, capsys):
    _hold(scratch_db, holder='someone else')
    monkeypatch.setenv('OPS_LOCK_HOLDER', 'runner')
    monkeypatch.setattr(ops_lock, '_is_shared', lambda db: True)
    with pytest.raises(SystemExit):
        ops_lock.require(scratch_db, 'tool --execute')
    assert 'someone else' in capsys.readouterr().out


def test_require_refuses_an_expired_lease_and_a_changed_token(scratch_db, monkeypatch):
    monkeypatch.setattr(ops_lock, '_is_shared', lambda db: True)
    doc = _hold(scratch_db, ttl=1)
    monkeypatch.setenv('OPS_LOCK_HOLDER', 'runner')
    monkeypatch.setenv('OPS_LOCK_TOKEN', str(doc['token'] + 1))
    with pytest.raises(SystemExit):
        ops_lock.require(scratch_db, 'tool --execute')
    monkeypatch.setenv('OPS_LOCK_TOKEN', str(doc['token']))
    time.sleep(1.1)
    with pytest.raises(SystemExit):
        ops_lock.require(scratch_db, 'tool --execute')


def test_require_only_warns_on_a_local_database(scratch_db, capsys):
    ops_lock.require(scratch_db, 'tool --execute')
    assert 'WARNING' in capsys.readouterr().out


def test_no_lock_passes_and_logs_the_reason(scratch_db, monkeypatch, capsys):
    monkeypatch.setattr(ops_lock, '_is_shared', lambda db: True)
    ops_lock.require(scratch_db, 'tool --execute', no_lock='the lock document is corrupt')
    entry = scratch_db[ops_lock.LOG_COLLECTION].find_one({'event': 'no_lock'})
    assert entry['reason'] == 'the lock document is corrupt' and entry['purpose'] == 'tool --execute'
    assert 'the lock document is corrupt' in capsys.readouterr().out


@pytest.mark.parametrize('name, host, shared', [
    ('caper', 'cluster.docdb.amazonaws.com', True),
    ('caper-dev', 'cluster.docdb.amazonaws.com', True),
    ('caper-dev', 'localhost', False),
    ('caper-ci-test', 'cluster.docdb.amazonaws.com', False),
])
def test_only_the_named_databases_away_from_this_machine_count_as_shared(name, host, shared):
    class Client:
        address = (host, 27017)

    class Db:
        client = Client()

    Db.name = name
    assert ops_lock._is_shared(Db) is shared


class _Staff:
    is_staff = True


class _Visitor:
    is_staff = False


def _notice(path, user, monkeypatch, collection):
    request = RequestFactory().get(path)
    request.user = user
    monkeypatch.setattr(context_processor, '_get_settings_collection', lambda: collection)
    return context_processor.ops_lock_notice(request)


def test_admin_pages_show_a_held_lock_to_staff_only(lock, cols, monkeypatch):
    lock.acquire('claude:x', 'jens', 'testing a backfill', 3600)
    notice = _notice('/admin-stats/', _Staff(), monkeypatch, cols[0])['OPS_LOCK']
    assert (notice['holder'], notice['owner'], notice['reason']) == ('claude:x', 'jens', 'testing a backfill')
    assert _notice('/admin-stats/', _Visitor(), monkeypatch, cols[0]) == {}
    assert _notice('/project/x', _Staff(), monkeypatch, cols[0]) == {}


def test_admin_pages_show_nothing_for_a_free_or_expired_lock(lock, cols, monkeypatch):
    assert _notice('/admin-stats/', _Staff(), monkeypatch, cols[0]) == {}
    lock.acquire('a', 'o', 'r', 1)
    assert _notice('/admin-stats/', _Staff(), monkeypatch, cols[0]) != {}
    cols[0].update_one({'_id': ops_lock.LOCK_ID},
                       {'$set': {'expires_at': datetime.datetime.utcnow() - datetime.timedelta(seconds=5)}})
    assert _notice('/admin-stats/', _Staff(), monkeypatch, cols[0]) == {}
    lock.release('a')
    assert _notice('/admin-stats/', _Staff(), monkeypatch, cols[0]) == {}


@pytest.mark.parametrize('tool', [
    'backfill_audit_chain_ids', 'backfill_create_events', 'backfill_gridfs_backlinks',
    'backfill_gridfs_sha256', 'backfill_project_status', 'cleanup_failed_upload_residue',
    'cleanup_orphaned_projects', 'clear_stale_uploads', 'dedupe_sample_level_copies',
    'purge-local-db', 'rebuild_version_chains', 'recover_deleted_version',
    'repair_head_chain_fields', 'repair_promoted_tombstone', 'shutdown_mode',
    'sweep_gridfs_unreferenced', 'sweep_s3_unreferenced', 'zero_carried_forward_views',
    'caper/caper/management/commands/create_project',
])
def test_every_write_tool_checks_the_lock_and_offers_the_escape_hatch(tool):
    with open(os.path.join(REPO_ROOT, tool + '.py')) as handle:
        source = handle.read()
    assert 'ops_lock.require' in source
    assert 'add_no_lock_argument' in source
