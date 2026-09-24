"""Tests for shutdown_mode.py, against scratch collections in the configured database."""

import datetime
import os
import uuid

import pytest
from pymongo import MongoClient

import shutdown_mode
from caper.context_processor import get_shutdown_pending


@pytest.fixture
def cols():
    uri = os.getenv('DB_URI_SECRET')
    db_name = os.getenv('DB_NAME')
    if not uri or not db_name:
        pytest.skip('DB_URI_SECRET and DB_NAME must both be set')
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command('ping')
    except Exception as exc:
        pytest.skip('no MongoDB available: %s' % exc)
    suffix = uuid.uuid4().hex[:8]
    settings_col = client[db_name]['shutdown_settings_test_%s' % suffix]
    tasks_col = client[db_name]['shutdown_tasks_test_%s' % suffix]
    try:
        yield settings_col, tasks_col
    finally:
        settings_col.drop()
        tasks_col.drop()


def add_task(tasks_col, state='running', minutes_ago=1):
    tasks_col.insert_one({
        '_id': uuid.uuid4().hex,
        'label': 'upload',
        'state': state,
        'started_at': 'x',
        'updated_at': datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes_ago),
        'worker_pid': 1,
    })


def test_writes_the_field_the_site_reads(cols, monkeypatch):
    settings_col, tasks_col = cols
    monkeypatch.setattr('caper.context_processor._get_settings_collection', lambda: settings_col)

    assert shutdown_mode.get_flag(settings_col) is None
    assert get_shutdown_pending() is False

    assert shutdown_mode.cmd_on(settings_col, tasks_col, allow_running=False) == 0
    assert get_shutdown_pending() is True

    assert shutdown_mode.cmd_off(settings_col) == 0
    assert get_shutdown_pending() is False


def test_on_refuses_while_a_task_runs(cols):
    settings_col, tasks_col = cols
    add_task(tasks_col)

    assert shutdown_mode.cmd_on(settings_col, tasks_col, allow_running=False) == 1
    assert shutdown_mode.get_flag(settings_col) is None

    assert shutdown_mode.cmd_on(settings_col, tasks_col, allow_running=True) == 0
    assert shutdown_mode.get_flag(settings_col) is True


def test_finished_tasks_do_not_block_but_old_running_ones_do(cols):
    settings_col, tasks_col = cols
    add_task(tasks_col, state='completed')
    add_task(tasks_col, state='stale', minutes_ago=30)
    assert shutdown_mode.running_tasks(tasks_col) == []

    # Past the admin page's 20-minute cutoff, but nothing has proven it dead.
    add_task(tasks_col, minutes_ago=45)
    assert len(shutdown_mode.running_tasks(tasks_col)) == 1
    assert shutdown_mode.cmd_on(settings_col, tasks_col, allow_running=False) == 1


def test_wait_returns_when_tasks_finish_and_times_out_when_they_do_not(cols):
    _, tasks_col = cols
    add_task(tasks_col)

    def finish(_):
        tasks_col.update_many({}, {'$set': {'state': 'completed'}})

    assert shutdown_mode.cmd_wait(tasks_col, timeout=60, interval=1, sleep=finish) == 0

    add_task(tasks_col)
    ticks = iter([0, 0, 100])
    assert shutdown_mode.cmd_wait(tasks_col, timeout=60, interval=1,
                                  sleep=lambda _: None, clock=lambda: next(ticks)) == 1


def test_refuses_the_wrong_database(monkeypatch):
    monkeypatch.setenv('DB_NAME', 'caper-dev')
    with pytest.raises(SystemExit) as exc:
        shutdown_mode.connect('caper')
    assert 'expected' in str(exc.value)
