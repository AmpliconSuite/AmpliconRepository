#!/usr/bin/env python
"""
Take, renew and release the operator lock for one database.

The lock tells other people and agents that someone is changing a shared
environment. It is one document, `system_settings/{_id: ops_lock}`, with a lease:
it expires unless renewed, so a crashed session can't hold it forever. It doesn't
lock MongoDB. The site and anyone who ignores it carry on as usual. It works because
the repo's write tools check it (`require()` below) and AGENTS.md tells agents to.
Users are kept out by shutdown mode, a separate mechanism.

Usage (on the server, from the Django project directory, /srv/caper in the container):
    docker exec -w /srv/caper amplicon-dev /opt/venv/bin/python -m caper.ops_lock --expect-db caper-dev status
    ... -m caper.ops_lock --expect-db caper-dev run --holder H --owner O --reason "..." -- <command...>
    ... -m caper.ops_lock --expect-db caper-dev acquire --holder H --owner O --reason "..." [--ttl 1h]
    ... -m caper.ops_lock --expect-db caper-dev renew --holder H [--ttl 1h]
    ... -m caper.ops_lock --expect-db caper-dev release --holder H
    ... -m caper.ops_lock --expect-db caper-dev wait [--timeout 1800] [--interval 30]
    ... -m caper.ops_lock --expect-db caper-dev break --holder H --reason "..."

Locally, `source caper/config.sh` first and run it with the conda python.

`run` is the normal way to use it: it acquires, renews while the command runs, and
releases even if the command fails, so a forgotten release isn't possible. Use
`acquire`, `renew` and `release` for interactive work with several steps, and pick
a TTL for the next stretch of work, not the whole day.

Holders: `claude:<session id>`, `agent:<tool>:<id>` or `human:<username>`. The owner
is always the person accountable. `--holder` defaults to $OPS_LOCK_HOLDER.

If someone else holds the lock, report who and why to your user and use `wait`.
`break` is for a holder known to be gone, and on prod it's its own explicit ask, like
any other write to production. So are `acquire`, `renew`, `release` and `run` there.

Times come from the database server's clock (`hello.localTime`), because operators'
machines disagree. MongoDB's `$$NOW` would be simpler, but the deployed DocumentDB
refuses it ("Feature not supported: $$NOW", engine 5.0.0, 2026-11-06). The time is
read just before the conditional write, so a lease could be judged expired up to one
round trip late, which doesn't matter with leases of an hour.

Write tools call `require()`. On `caper` and `caper-dev` they refuse without the
lock, and on a local MongoDB with the same names they only warn. A local database is
recognised by its host, so a tunnel to the cluster through localhost also only warns.

Exit status: 0 done, 1 refused or timed out, 2 usage.
"""

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta

from pymongo import MongoClient, ReadPreference, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

LOCK_ID = 'ops_lock'
SETTINGS_COLLECTION = 'system_settings'
LOG_COLLECTION = 'ops_lock_log'
DEFAULT_TTL_SECONDS = 60 * 60
MAX_TTL_SECONDS = 6 * 60 * 60

SHARED_DB_NAMES = ('caper', 'caper-dev')
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1', 'mongodb', 'amprepo-mongo')

ACQUIRE_ATTEMPTS = 3


class LockHeld(Exception):
    """Someone else holds an unexpired lease. Carries their document."""

    def __init__(self, doc):
        super().__init__(str(doc.get('holder')))
        self.doc = doc


class LockNotHeld(Exception):
    """The caller doesn't hold the lock, so it may have lost its lease."""

    def __init__(self, doc):
        super().__init__(str((doc or {}).get('holder')))
        self.doc = doc


def server_now(client):
    """The database server's clock, as the naive UTC datetime pymongo reads dates as."""
    return client.admin.command('hello')['localTime']


class OpsLock:
    """The lock document and its log. Every state change is one conditional write."""

    def __init__(self, settings_col, log_col, now=None):
        self.settings = settings_col
        self.log_col = log_col
        self.db_name = settings_col.database.name
        self._now = now or (lambda: server_now(settings_col.database.client))

    def read(self):
        return self.settings.find_one({'_id': LOCK_ID})

    def log(self, event, holder=None, owner=None, reason=None, previous_holder=None,
            token=None, purpose=None):
        entry = {'at': self._now(), 'event': event, 'db': self.db_name, 'holder': holder,
                 'owner': owner, 'reason': reason, 'previous_holder': previous_holder,
                 'token': token}
        if purpose:
            entry['purpose'] = purpose
        self.log_col.insert_one(entry)

    def acquire(self, holder, owner, reason, ttl_seconds, host=None):
        """Take the lock, or extend it if the caller already holds it.

        Returns the document and what happened: 'renew', 'acquire' or
        'expired_takeover'. Raises LockHeld if someone else has an unexpired lease.
        """
        for _attempt in range(ACQUIRE_ATTEMPTS):
            try:
                return self._acquire_once(holder, owner, reason, ttl_seconds, host)
            except DuplicateKeyError:
                # The filter missed and the upsert collided on _id, so someone holds
                # it, unless they released it in between. Then the next try takes it.
                doc = self.read()
                if doc and doc.get('holder') and doc['holder'] != holder \
                        and doc['expires_at'] > self._now():
                    raise LockHeld(doc)
        raise LockHeld(self.read() or {})

    def _acquire_once(self, holder, owner, reason, ttl_seconds, host):
        now = self._now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        renewed = self.settings.find_one_and_update(
            {'_id': LOCK_ID, 'holder': holder},
            {'$set': {'renewed_at': now, 'expires_at': expires_at, 'reason': reason,
                      'owner': owner, 'host': host}},
            return_document=ReturnDocument.AFTER)
        if renewed:
            return renewed, 'renew'

        previous = self.settings.find_one_and_update(
            {'_id': LOCK_ID,
             '$or': [{'holder': None}, {'expires_at': {'$lte': now}}]},
            {'$set': {'holder': holder, 'owner': owner, 'reason': reason, 'host': host,
                      'acquired_at': now, 'renewed_at': now, 'expires_at': expires_at},
             '$inc': {'token': 1}},
            upsert=True, return_document=ReturnDocument.BEFORE)

        previous = previous or {}
        replaced = previous.get('holder')
        event = 'expired_takeover' if replaced else 'acquire'
        doc = dict(previous, _id=LOCK_ID, holder=holder, owner=owner, reason=reason, host=host,
                   acquired_at=now, renewed_at=now, expires_at=expires_at,
                   token=previous.get('token', 0) + 1)
        self.log(event, holder, owner, reason, previous_holder=replaced, token=doc['token'])
        return doc, event

    def renew(self, holder, ttl_seconds):
        """Extend the caller's lease. Raises LockNotHeld if it has gone."""
        now = self._now()
        doc = self.settings.find_one_and_update(
            {'_id': LOCK_ID, 'holder': holder},
            {'$set': {'renewed_at': now, 'expires_at': now + timedelta(seconds=ttl_seconds)}},
            return_document=ReturnDocument.AFTER)
        if not doc:
            current = self.read()
            self.log('renew_failed', holder,
                     previous_holder=(current or {}).get('holder'),
                     token=(current or {}).get('token'))
            raise LockNotHeld(current)
        return doc

    def release(self, holder):
        """Free the lock, which only the holder can do. Raises LockNotHeld otherwise."""
        doc = self.settings.find_one_and_update(
            {'_id': LOCK_ID, 'holder': holder},
            {'$set': {'holder': None, 'released_at': self._now(), 'previous_holder': holder}},
            return_document=ReturnDocument.BEFORE)
        if not doc:
            raise LockNotHeld(self.read())
        self.log('release', holder, doc.get('owner'), doc.get('reason'), token=doc.get('token'))
        return doc

    def break_lock(self, by, reason):
        """Free someone else's lock. Returns the previous document, or None if it was free."""
        doc = self.settings.find_one_and_update(
            {'_id': LOCK_ID, 'holder': {'$ne': None}},
            {'$set': {'holder': None, 'released_at': self._now(), 'broken_by': by,
                      'previous_holder': None}},
            return_document=ReturnDocument.BEFORE)
        if not doc:
            return None
        self.settings.update_one({'_id': LOCK_ID}, {'$set': {'previous_holder': doc['holder']}})
        self.log('break', by, reason=reason, previous_holder=doc['holder'], token=doc.get('token'))
        return doc

    def is_free(self):
        doc = self.read()
        return not doc or not doc.get('holder') or doc['expires_at'] <= self._now()


def describe(doc, now):
    """The lock's state in one or two lines, for status output and refusals."""
    if not doc or not doc.get('holder'):
        line = 'free'
        if doc and doc.get('previous_holder'):
            line += ' (last held by %s)' % doc['previous_holder']
        return line
    remaining = (doc['expires_at'] - now).total_seconds()
    age = (now - doc['acquired_at']).total_seconds()
    state = 'expires in %dm' % (remaining // 60) if remaining > 0 else \
        'EXPIRED %dm ago, anyone may take it' % (-remaining // 60)
    return 'held by %s (owner %s), on %s, for %dm, %s\n  reason: %s' % (
        doc['holder'], doc.get('owner'), doc.get('host'), age // 60, state, doc.get('reason'))


def add_no_lock_argument(parser):
    """The escape hatch every write tool offers."""
    parser.add_argument('--no-lock', metavar='REASON',
                        help='Run without the operator lock. Emergencies only; the reason is logged.')


def _is_shared(db):
    if db.name not in SHARED_DB_NAMES:
        return False
    return db.client.address[0] not in LOCAL_HOSTS


def require(db, purpose, no_lock=None):
    """Stop a write tool unless this process holds the lock for `db`.

    Call it in write mode only, never in a dry run. `run` passes the holder and token
    to its child, and an interactive holder exports OPS_LOCK_HOLDER. The token shows
    a lease that was lost and retaken while the tool ran. `no_lock` is the reason from
    `--no-lock`. Refuses on a shared database and warns on a local one.
    """
    # The primary, whatever this tool's connection prefers: a replica could still be
    # missing the acquire that `run` made a moment ago.
    settings = db.get_collection(SETTINGS_COLLECTION, read_preference=ReadPreference.PRIMARY)
    lock = OpsLock(settings, db[LOG_COLLECTION])
    doc = lock.read()
    now = server_now(db.client)
    holder = os.environ.get('OPS_LOCK_HOLDER')
    token = os.environ.get('OPS_LOCK_TOKEN')
    if (doc and holder and doc.get('holder') == holder and doc['expires_at'] > now
            and (not token or str(doc.get('token')) == token)):
        return
    if no_lock:
        lock.log('no_lock', holder, reason=no_lock, purpose=purpose)
        print('WARNING: running %s without the operator lock: %s' % (purpose, no_lock))
        return
    state = describe(doc, now)
    if _is_shared(db):
        print('REFUSED: %s needs the operator lock on %s, which is %s\n'
              'For this one command, wrap it in `python -m caper.ops_lock --expect-db %s run ... -- <command>`. '
              'For a longer job such as a deploy, `acquire` first, because a restart kills '
              '`run` inside the container. If someone else holds it, tell your user and use '
              '`wait`.' % (purpose, db.name, state, db.name))
        sys.exit(1)
    print('WARNING: %s is running without the operator lock on local database %s, which is %s'
          % (purpose, db.name, state))


def require_for_environment(purpose, no_lock=None):
    """`require()` for a tool that has no database connection of its own."""
    db_name, uri = os.environ.get('DB_NAME'), os.environ.get('DB_URI_SECRET')
    if not db_name or not uri:
        if no_lock:
            print('WARNING: running %s without the operator lock: %s (not logged: DB_NAME and '
                  'DB_URI_SECRET are not set)' % (purpose, no_lock))
            return
        print('REFUSED: %s needs DB_NAME and DB_URI_SECRET to check the operator lock.' % purpose)
        sys.exit(1)
    require(MongoClient(uri)[db_name], purpose, no_lock)


def parse_ttl(text):
    """'90s', '15m', '1h' or a bare number of seconds."""
    match = re.fullmatch(r'(\d+)([smh]?)', text.strip())
    if not match:
        raise argparse.ArgumentTypeError('%r is not a duration like 90s, 15m or 1h' % text)
    seconds = int(match.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match.group(2)]
    if seconds <= 0:
        raise argparse.ArgumentTypeError('the TTL must be positive')
    if seconds > MAX_TTL_SECONDS:
        raise argparse.ArgumentTypeError('the TTL can be at most 6h, so that a forgotten '
                                         'lock clears within a working day')
    return seconds


def print_held(lock, error):
    print('REFUSED: the lock is %s' % describe(error.doc, lock._now()))
    print('Report this to your user and use `wait`. Do not break it.')


def cmd_acquire(lock, args):
    try:
        doc, event = lock.acquire(args.holder, args.owner, args.reason, args.ttl, socket.gethostname())
    except LockHeld as error:
        print_held(lock, error)
        return 1
    if event == 'expired_takeover':
        print('Replaced an expired lease.')
    print('%s: token %s, lease %ds. export OPS_LOCK_HOLDER=%s OPS_LOCK_TOKEN=%s'
          % (event, doc['token'], args.ttl, args.holder, doc['token']))
    return 0


def cmd_renew(lock, args):
    try:
        lock.renew(args.holder, args.ttl)
    except LockNotHeld as error:
        print('REFUSED: you do not hold the lock, so your lease may have been lost. It is %s'
              % describe(error.doc, lock._now()))
        return 1
    print('renewed for %ds.' % args.ttl)
    return 0


def cmd_release(lock, args):
    try:
        lock.release(args.holder)
    except LockNotHeld as error:
        print('REFUSED: you do not hold the lock, so your lease may have been lost. It is %s'
              % describe(error.doc, lock._now()))
        return 1
    print('released.')
    return 0


def cmd_break(lock, args):
    doc = lock.break_lock(args.holder, args.reason)
    if not doc:
        print('the lock was already free; nothing written.')
        return 0
    print('broke the lock held by %s (owner %s, reason: %s).'
          % (doc['holder'], doc.get('owner'), doc.get('reason')))
    return 0


def cmd_wait(lock, timeout, interval, sleep=time.sleep, clock=time.monotonic):
    """Wait until the lock is free. Doesn't take it: the caller's `acquire` settles that."""
    deadline = clock() + timeout
    while True:
        if lock.is_free():
            print('the lock is free.')
            return 0
        state = describe(lock.read(), lock._now())
        if clock() >= deadline:
            print('TIMED OUT after %ds. The lock is %s' % (timeout, state))
            return 1
        print('the lock is %s\nchecking again in %ds.' % (state, interval))
        sys.stdout.flush()
        sleep(interval)


def cmd_run(lock, args, command):
    """Hold the lock for the length of a command, and return the command's exit status."""
    try:
        doc, event = lock.acquire(args.holder, args.owner, args.reason, args.ttl, socket.gethostname())
    except LockHeld as error:
        print_held(lock, error)
        return 1
    print('%s: token %s. Running: %s' % (event, doc['token'], ' '.join(command)))
    sys.stdout.flush()

    env = dict(os.environ, OPS_LOCK_HOLDER=args.holder, OPS_LOCK_TOKEN=str(doc['token']))
    try:
        # Its own session, so a Ctrl-C reaches it only through the handler below,
        # once, rather than from the terminal as well.
        child = subprocess.Popen(command, env=env, start_new_session=True)
    except OSError as error:
        print('could not start the command: %s' % error)
        lock.release(args.holder)
        return 1

    def forward(signum, _frame):
        child.send_signal(signum)

    old_handlers = {signum: signal.signal(signum, forward)
                    for signum in (signal.SIGINT, signal.SIGTERM)}
    stop_renewing = threading.Event()
    lost = threading.Event()

    def keep_renewing():
        while not stop_renewing.wait(args.ttl / 4):
            try:
                lock.renew(args.holder, args.ttl)
            except LockNotHeld as error:
                lost.set()
                print('WARNING: THE OPERATOR LOCK WAS LOST while the command is still running. '
                      'It is %s' % describe(error.doc, lock._now()))
                sys.stdout.flush()
                return
            except PyMongoError as error:
                # Worth another try at the next tick: the lease outlasts three misses.
                print('WARNING: could not renew the operator lock: %s' % error)
                sys.stdout.flush()

    renewer = threading.Thread(target=keep_renewing, daemon=True)
    renewer.start()
    try:
        # Interrupting a write halfway is worse than a short overlap, so a lost lease
        # doesn't stop the child. It only makes the exit status non-zero.
        status = child.wait()
    finally:
        stop_renewing.set()
        renewer.join(timeout=5)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        try:
            lock.release(args.holder)
            print('released.')
        except LockNotHeld as error:
            lost.set()
            print('WARNING: the operator lock was already gone at release. It is %s'
                  % describe(error.doc, lock._now()))
        except PyMongoError as error:
            print('WARNING: could not release the operator lock, so it will expire on its '
                  'own: %s' % error)

    if status < 0:
        status = 128 - status
    return status if status else (1 if lost.is_set() else 0)


def connect(expect_db):
    db_name = os.environ.get('DB_NAME')
    uri = os.environ.get('DB_URI_SECRET')
    if db_name != expect_db:
        sys.exit('ABORT: DB_NAME is %r, expected %r. Nothing was read.' % (db_name, expect_db))
    if not uri:
        sys.exit('ABORT: DB_URI_SECRET is not set.')
    # Primary, not the replica the site reads from: every decision here depends on
    # the write just made, including someone else's.
    return MongoClient(uri, read_preference=ReadPreference.PRIMARY)[db_name]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--expect-db', required=True,
                        help="Abort unless DB_NAME is this. 'caper' is prod, 'caper-dev' is dev.")
    sub = parser.add_subparsers(dest='command', required=True)

    def add(name, help_text, holder=True, owner=False, reason=False, ttl=False):
        command = sub.add_parser(name, help=help_text)
        if holder:
            command.add_argument('--holder', default=os.environ.get('OPS_LOCK_HOLDER'),
                                 help='claude:<id>, agent:<tool>:<id> or human:<name>; '
                                      'defaults to $OPS_LOCK_HOLDER')
        if owner:
            command.add_argument('--owner', required=True, help='the person accountable')
        if reason:
            command.add_argument('--reason', required=True, help='what this is for')
        if ttl:
            command.add_argument('--ttl', type=parse_ttl, default=DEFAULT_TTL_SECONDS,
                                 help='lease length, like 90s, 15m or 1h (default 1h, at most 6h)')
        return command

    sub.add_parser('status', help='print who holds the lock')
    add('acquire', 'take the lock', owner=True, reason=True, ttl=True)
    add('renew', 'extend your lease', ttl=True)
    add('release', 'free your lock')
    wait = sub.add_parser('wait', help='block until the lock is free, without taking it')
    wait.add_argument('--timeout', type=int, default=1800, help='seconds (default 1800)')
    wait.add_argument('--interval', type=int, default=30, help='seconds between checks (default 30)')
    run = add('run', 'hold the lock while a command runs', owner=True, reason=True, ttl=True)
    run.add_argument('run_command', nargs=argparse.REMAINDER, metavar='-- command...')
    add('break', "free someone else's lock; for a holder known to be gone", reason=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in ('status', 'wait') and not args.holder:
        parser.error('--holder is required, or set OPS_LOCK_HOLDER')
    command = None
    if args.command == 'run':
        command = args.run_command[1:] if args.run_command[:1] == ['--'] else args.run_command
        if not command:
            parser.error('run needs a command after --')

    db = connect(args.expect_db)
    lock = OpsLock(db[SETTINGS_COLLECTION], db[LOG_COLLECTION])
    print('database:  %s' % db.name)
    print('lock:      %s' % describe(lock.read(), lock._now()))
    sys.stdout.flush()
    if args.command == 'acquire':
        return cmd_acquire(lock, args)
    if args.command == 'renew':
        return cmd_renew(lock, args)
    if args.command == 'release':
        return cmd_release(lock, args)
    if args.command == 'wait':
        return cmd_wait(lock, args.timeout, args.interval)
    if args.command == 'run':
        return cmd_run(lock, args, command)
    if args.command == 'break':
        return cmd_break(lock, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
