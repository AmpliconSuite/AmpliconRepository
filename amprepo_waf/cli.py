"""python -m amprepo_waf: read, check, change and probe the site's WAF web ACL.

Every change is a dry run unless --apply is given. See amprepo_waf/README.md.
"""
import argparse
import copy
import datetime
import json
import os
import sys

from . import acl, probe
from .aws import AwsCli, AwsError
from .edits import EDITS


def _write(directory, name, payload):
    with open(os.path.join(directory, name), 'w', encoding='utf-8') as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle, indent=1, sort_keys=True)


def change(aws, label, transform, args, out=print):
    """Plan a change, show it, and with args.apply make it. Returns an exit code."""
    live, token = aws.get_web_acl()
    accepted = aws.accepted_fields()
    defaults = aws.bot_control_defaults(live)

    for problem in acl.check(live, defaults):
        out(f'note: the live ACL already fails a check: {problem}')

    proposed = copy.deepcopy(live)
    transform(proposed, defaults)
    if acl.comparable(proposed, accepted) == acl.comparable(live, accepted):
        out(f'{label}: already applied, nothing to change.')
        return 0

    problems = acl.check(proposed, defaults)
    if problems:
        out(f'{label}: refused, the result would fail these checks:')
        for problem in problems:
            out(f'  - {problem}')
        return 1
    try:
        params = acl.update_input(proposed, token, accepted)
    except acl.UnknownField as error:
        out(f'{label}: refused. {error}')
        return 1
    capacity = aws.check_capacity(proposed['Rules'])

    out(acl.diff(live, proposed))
    out(f'capacity: {live.get("Capacity")} -> {capacity} WCU; AWS accepted the rules.')
    if not args.apply:
        out('Dry run: nothing was sent. Add --apply (see the README for what it needs).')
        return 0

    if not args.record_dir or not os.path.isdir(args.record_dir):
        out('--apply needs --record-dir pointing at an existing directory for the undo record.')
        return 1
    if not (args.lock_held_by or args.no_lock):
        out('--apply needs --lock-held-by HOLDER (the operator lock is held) or --no-lock "<reason>".')
        return 1

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    record = os.path.join(args.record_dir, f'{stamp}-{label}')
    os.makedirs(record)
    _write(record, 'before.json', {'WebACL': live, 'LockToken': token})
    _write(record, 'proposed.json', {'WebACL': proposed})
    _write(record, 'diff.txt', acl.diff(live, proposed))
    _write(record, 'meta.json', {'change': label, 'started_utc': stamp,
                                 'lock_held_by': args.lock_held_by, 'no_lock': args.no_lock})

    try:
        aws.update_web_acl(params)
    except AwsError as error:
        _write(record, 'FAILED.txt', f'{error}\n')
        out(f'{label}: the update was refused: {error}\nRun `show` to confirm the live ACL; '
            f'the record is in {record}')
        return 1
    after, _ = aws.get_web_acl()
    _write(record, 'after.json', {'WebACL': after})
    if acl.comparable(after, accepted) != acl.comparable(proposed, accepted):
        out(f'APPLIED, BUT THE LIVE ACL DIFFERS FROM WHAT WAS SENT. Compare {record}/after.json '
            f'with proposed.json before doing anything else.')
        return 1
    out(f'Applied. Undo record: {record}')
    out(f'To undo: python -m amprepo_waf restore {record}/before.json --apply ...')
    return 0


def restore_transform(path):
    with open(path, encoding='utf-8') as handle:
        target = json.load(handle)['WebACL']

    def transform(proposed, _defaults):
        for key in list(proposed):
            if key not in acl.READ_ONLY_FIELDS:
                proposed.pop(key)
        proposed.update({k: copy.deepcopy(v) for k, v in target.items()
                         if k not in acl.READ_ONLY_FIELDS})
    return transform


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m amprepo_waf', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--profile', default=os.environ.get('AWS_PROFILE', 'amprepo'))
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('show', help='print the live rules in evaluation order')
    sub.add_parser('check', help='run the checks against the live ACL')
    sub.add_parser('list-edits', help='the named changes this tool can make')

    def writer(p):
        p.add_argument('--apply', action='store_true', help='send it (default: dry run)')
        p.add_argument('--record-dir', help='existing directory; the undo record goes in a new subdirectory')
        lock = p.add_mutually_exclusive_group()
        lock.add_argument('--lock-held-by', metavar='HOLDER', help='who holds the prod operator lock')
        lock.add_argument('--no-lock', metavar='REASON', help='emergency only; recorded')

    p_edit = sub.add_parser('edit', help='plan or apply one named edit')
    p_edit.add_argument('name', choices=sorted(EDITS))
    writer(p_edit)
    p_restore = sub.add_parser('restore', help='put back an ACL from an undo record')
    p_restore.add_argument('before_json')
    writer(p_restore)

    p_probe = sub.add_parser('probe', help='requests against the live site, with expected results')
    p_probe.add_argument('--host', default='https://ampliconrepository.org')
    hint = p_probe.add_mutually_exclusive_group()
    hint.add_argument('--expect-hint', dest='expect_hint', action='store_true', default=None)
    hint.add_argument('--expect-no-hint', dest='expect_hint', action='store_false')

    args = parser.parse_args(argv)

    if args.command == 'list-edits':
        for name, (_, text) in sorted(EDITS.items()):
            print(f'{name}\n    {text}')
        return 0
    if args.command == 'probe':
        print(f'== {args.host}  {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}')
        return 1 if probe.run(args.host, args.expect_hint) else 0

    aws = AwsCli(profile=args.profile)
    try:
        if args.command == 'show':
            live, _ = aws.get_web_acl()
            print(acl.summary(live))
            return 0
        if args.command == 'check':
            live, _ = aws.get_web_acl()
            problems = acl.check(live, aws.bot_control_defaults(live))
            for problem in problems:
                print(f'- {problem}')
            print('ok' if not problems else f'{len(problems)} problem(s)')
            return 1 if problems else 0
        if args.command == 'edit':
            return change(aws, args.name, EDITS[args.name][0], args)
        if args.command == 'restore':
            return change(aws, 'restore', restore_transform(args.before_json), args)
    except AwsError as error:
        print(error, file=sys.stderr)
        return 2
    return 1
