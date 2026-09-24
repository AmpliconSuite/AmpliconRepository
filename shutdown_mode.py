#!/usr/bin/env python
"""
Read or toggle shutdown mode from the command line.

Shutdown mode is the flag behind /admin-prepare-shutdown/: the field
`system_settings/{_id: system_flags}.shutdown_pending`, the same one
caper.context_processor.set_shutdown_pending() writes. While it is on, every
page carries the "server shutdown is pending" banner and the create-project
links refuse to start an upload. It stops no work already running.

The deploy runbooks in the GitHub wiki open with "confirm no background tasks
are running, enable shutdown mode" and close with "disable shutdown mode". This
is those two steps for someone without a browser session -- an agent on the
server, usually.

Usage (on the server, from the repo root, which the container mounts at /srv):
    docker exec -w /srv amplicon-dev /opt/venv/bin/python shutdown_mode.py --expect-db caper-dev status
    ... shutdown_mode.py --expect-db caper-dev on
    ... shutdown_mode.py --expect-db caper-dev wait --timeout 1800
    ... shutdown_mode.py --expect-db caper-dev off

Locally, `source caper/config.sh` first and run it with the conda python.

`on` and `off` are database writes. On prod (`--expect-db caper`) each one is
its own explicit ask, like any other write to production.

What the task check can and cannot see. Background tasks are rows in
`background_tasks` with `state: 'running'`; nothing refreshes them while they
run. The admin page relabels any row older than 20 minutes as 'stale' when it
is viewed, and the collection's TTL index deletes rows an hour after their last
update. So a real aggregation more than an hour old is invisible here, and one
between 20 and 60 minutes old is not listed on the admin page. This script
performs no relabelling and reports every 'running' row with its age; "no
tasks" still means "no task younger than about an hour".

Exit status: 0 done, 1 refused or timed out, 2 usage.
"""

import argparse
import datetime
import os
import sys
import time

from pymongo import MongoClient, ReadPreference

FLAGS_ID = 'system_flags'
FLAG = 'shutdown_pending'

# Mirrors background_tasks._STALE_THRESHOLD_SECONDS, used only to annotate.
ADMIN_PAGE_STALE_SECONDS = 20 * 60


def get_flag(settings_col):
    """The stored value, or None when the field has never been written.

    The application reads a missing field as False; the distinction is kept
    here so the before/after lines say what was actually in the database.
    """
    doc = settings_col.find_one({'_id': FLAGS_ID}) or {}
    return doc.get(FLAG)


def set_flag(settings_col, value):
    settings_col.update_one({'_id': FLAGS_ID}, {'$set': {FLAG: value}}, upsert=True)
    return get_flag(settings_col)


def running_tasks(tasks_col, now=None):
    """Every row still marked running, oldest first, with its age in seconds."""
    now = now or datetime.datetime.utcnow()
    tasks = []
    for doc in tasks_col.find({'state': 'running'}).sort('updated_at', 1):
        updated = doc.get('updated_at')
        age = (now - updated).total_seconds() if isinstance(updated, datetime.datetime) else None
        tasks.append({
            'id': doc['_id'],
            'label': doc.get('label', ''),
            'started_at': doc.get('started_at', ''),
            'worker_pid': doc.get('worker_pid'),
            'age_seconds': age,
        })
    return tasks


def describe_task(task):
    age = task['age_seconds']
    line = '  %s  label=%r  started=%s  pid=%s' % (
        task['id'], task['label'], task['started_at'], task['worker_pid'])
    if age is not None:
        line += '  age=%dm' % (age // 60)
        if age > ADMIN_PAGE_STALE_SECONDS:
            line += '  (the admin page would call this stale; it may still be alive)'
    return line


def report(db_name, flag, tasks):
    print('database:          %s' % db_name)
    print('shutdown_pending:  %r%s' % (flag, '  (never set; the site reads it as False)' if flag is None else ''))
    print('running tasks:     %d' % len(tasks))
    for task in tasks:
        print(describe_task(task))


def cmd_on(settings_col, tasks_col, allow_running):
    tasks = running_tasks(tasks_col)
    before = get_flag(settings_col)
    if tasks and not allow_running:
        print('REFUSED: %d background task(s) running. The runbook enables shutdown '
              'mode only once none are. Wait them out with `wait`, or pass '
              '--allow-running to raise the banner now (it blocks new uploads; it '
              'does not stop these).' % len(tasks))
        for task in tasks:
            print(describe_task(task))
        return 1
    if before is True:
        print('shutdown_pending already True; nothing written.')
        return 0
    after = set_flag(settings_col, True)
    print('shutdown_pending:  %r -> %r' % (before, after))
    return 0 if after is True else 1


def cmd_off(settings_col):
    before = get_flag(settings_col)
    if before is False:
        print('shutdown_pending already False; nothing written.')
        return 0
    after = set_flag(settings_col, False)
    print('shutdown_pending:  %r -> %r' % (before, after))
    return 0 if after is False else 1


def cmd_wait(tasks_col, timeout, interval, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        tasks = running_tasks(tasks_col)
        if not tasks:
            print('no running tasks.')
            return 0
        if clock() >= deadline:
            print('TIMED OUT after %ds with %d task(s) still running:' % (timeout, len(tasks)))
            for task in tasks:
                print(describe_task(task))
            return 1
        print('%d task(s) running; checking again in %ds.' % (len(tasks), interval))
        sys.stdout.flush()
        sleep(interval)


def connect(expect_db):
    db_name = os.environ.get('DB_NAME')
    uri = os.environ.get('DB_URI_SECRET')
    if db_name != expect_db:
        sys.exit('ABORT: DB_NAME is %r, expected %r. Nothing was read.' % (db_name, expect_db))
    if not uri:
        sys.exit('ABORT: DB_URI_SECRET is not set.')
    # Primary, not the replica the site reads from: the after-value must be
    # the write just made, and a task that just finished must not still show.
    client = MongoClient(uri, read_preference=ReadPreference.PRIMARY)
    return client[db_name]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--expect-db', required=True,
                        help="Abort unless DB_NAME is this. 'caper' is prod, 'caper-dev' is dev.")
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='print the flag and any running background tasks')
    on = sub.add_parser('on', help='enable shutdown mode (refuses while tasks run)')
    on.add_argument('--allow-running', action='store_true',
                    help='enable it even though tasks are running')
    sub.add_parser('off', help='disable shutdown mode')
    wait = sub.add_parser('wait', help='block until no background task is running')
    wait.add_argument('--timeout', type=int, default=1800, help='seconds (default 1800)')
    wait.add_argument('--interval', type=int, default=15, help='seconds between checks (default 15)')
    args = parser.parse_args(argv)

    db = connect(args.expect_db)
    settings_col, tasks_col = db['system_settings'], db['background_tasks']

    report(db.name, get_flag(settings_col), running_tasks(tasks_col))
    if args.command == 'on':
        return cmd_on(settings_col, tasks_col, args.allow_running)
    if args.command == 'off':
        return cmd_off(settings_col)
    if args.command == 'wait':
        return cmd_wait(tasks_col, args.timeout, args.interval)
    return 0


if __name__ == '__main__':
    sys.exit(main())
