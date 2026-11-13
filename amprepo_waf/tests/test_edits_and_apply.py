import argparse
import copy
import json
import os

from amprepo_waf import acl as A
from amprepo_waf import probe
from amprepo_waf.cli import change, restore_transform
from amprepo_waf.edits import EDITS, HINT_KEY, HINT_TEXT
from amprepo_waf.tests.conftest import DEFAULTS, FakeAws

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def args(**kw):
    base = {'apply': False, 'record_dir': None, 'lock_held_by': None, 'no_lock': None}
    return argparse.Namespace(**{**base, **kw})


def hint_edit():
    return EDITS['automated-client-hint'][0]


def _apply(web_acl):
    out = copy.deepcopy(web_acl)
    hint_edit()(out, DEFAULTS)
    return out


def test_hint_edit_passes_checks_and_is_idempotent(acl):
    once = _apply(acl)
    assert A.check(once, DEFAULTS) == []
    assert _apply(once) == once


def test_hint_edit_keeps_blocking_and_leaves_other_overrides(acl):
    after = _apply(acl)
    overrides = {o['Name']: o['ActionToUse']
                 for o in A.bot_control_statement(after)['RuleActionOverrides']}
    assert overrides['CategoryAI'] == {'Count': {}}
    hinted = {n for n, a in overrides.items()
              if a.get('Block', {}).get('CustomResponse', {}).get('CustomResponseBodyKey') == HINT_KEY}
    assert hinted == {'CategoryHttpLibrary', 'CategoryMiscellaneous', 'CategorySearchEngine', 'SignalNonBrowserUserAgent',
                      'SignalKnownBotDataCenter'}   # Block by default, not TGT_, not already Count
    unverified = A.rules_by_name(after)['BlockUnverifiedAI']['Action']
    assert unverified['Block']['CustomResponse'] == {'ResponseCode': 403, 'CustomResponseBodyKey': HINT_KEY}


def test_hint_text_fits_and_names_no_host():
    assert len(HINT_TEXT.encode()) < 4096          # AWS's limit for one body
    assert probe.HINT_MARKER in HINT_TEXT
    assert 'ampliconrepository.org' not in HINT_TEXT  # one ACL serves dev and prod


def test_dry_run_sends_nothing(fake):
    assert change(fake, 'h', hint_edit(), args(), out=lambda *_: None) == 0
    assert fake.sent == []


def test_apply_needs_a_record_dir_and_a_lock_statement(fake, tmp_path):
    quiet = lambda *_: None
    assert change(fake, 'h', hint_edit(), args(apply=True), out=quiet) == 1
    assert change(fake, 'h', hint_edit(), args(apply=True, record_dir=str(tmp_path)), out=quiet) == 1
    assert fake.sent == []


def test_apply_sends_the_whole_acl_and_keeps_an_undo_record(fake, tmp_path):
    before = copy.deepcopy(fake.web_acl)
    code = change(fake, 'h', hint_edit(), args(apply=True, record_dir=str(tmp_path), lock_held_by='me'),
                  out=lambda *_: None)
    assert code == 0 and len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent['CustomResponseBodies']['api-rate-limited'] == before['CustomResponseBodies']['api-rate-limited']
    assert sent['OnSourceDDoSProtectionConfig'] == before['OnSourceDDoSProtectionConfig']
    [record] = os.listdir(tmp_path)
    files = set(os.listdir(tmp_path / record))
    assert files == {'before.json', 'proposed.json', 'diff.txt', 'meta.json', 'after.json'}
    assert json.loads((tmp_path / record / 'meta.json').read_text())['lock_held_by'] == 'me'

    # Running it again changes nothing, and restore puts the original back.
    assert change(fake, 'h', hint_edit(), args(), out=lambda *_: None) == 0 and len(fake.sent) == 1
    restore = restore_transform(str(tmp_path / record / 'before.json'))
    assert change(fake, 'restore', restore, args(apply=True, record_dir=str(tmp_path), no_lock='test'),
                  out=lambda *_: None) == 0
    assert A.comparable(fake.web_acl, fake.accepted) == A.comparable(before, fake.accepted)


def test_an_edit_that_breaks_a_check_is_refused(fake, tmp_path):
    def bad(web_acl, _defaults):
        web_acl.pop('CustomResponseBodies')
    lines = []
    code = change(fake, 'bad', bad, args(apply=True, record_dir=str(tmp_path), lock_held_by='me'),
                  out=lines.append)
    assert code == 1 and fake.sent == [] and os.listdir(tmp_path) == []
    assert any('api-rate-limited' in line for line in lines)


def test_a_live_result_that_differs_from_what_was_sent_fails_loudly(tmp_path):
    def drops_bodies(params):
        return {k: v for k, v in params.items() if k not in ('Scope', 'LockToken', 'CustomResponseBodies')}
    fake = FakeAws(store=drops_bodies)
    lines = []
    code = change(fake, 'h', hint_edit(), args(apply=True, record_dir=str(tmp_path), lock_held_by='me'),
                  out=lines.append)
    assert code == 1 and any('DIFFERS FROM WHAT WAS SENT' in line for line in lines)


def test_probe_counts_rows_that_misbehave(capsys):
    def site(url, _ua, hint=True):
        path = url[len('https://x'):]
        row = next(r for r in probe.ROWS if r[1] == path and r[2] == _ua)
        if row[3] == 200:
            return 200, 'page'
        return 403, (HINT_TEXT if hint and row[4] else 'Forbidden')
    assert probe.run('https://x', expect_hint=True, fetch=site) == 0
    assert probe.run('https://x', expect_hint=False, fetch=site) == 3
    assert probe.run('https://x', expect_hint=False, fetch=lambda u, a: site(u, a, hint=False)) == 0


def test_agents_md_sends_waf_changes_through_this_tool():
    """The pointer is how a fresh session learns the tool exists; it must not drift away."""
    with open(os.path.join(REPO, 'AGENTS.md'), encoding='utf-8') as handle:
        text = handle.read()
    assert 'python -m amprepo_waf' in text


def test_google_fetcher_edit_counts_and_reblocks_the_category(acl):
    edit = EDITS['allow-verified-google-fetcher'][0]
    after = _apply(acl)          # the hint is already on the live ACL
    edit(after, DEFAULTS)
    assert A.check(after, DEFAULTS) == []
    once = copy.deepcopy(after)
    edit(after, DEFAULTS)
    assert after == once         # idempotent
    overrides = {o['Name']: o['ActionToUse'] for o in A.bot_control_statement(after)['RuleActionOverrides']}
    assert overrides['CategoryMiscellaneous'] == {'Count': {}}
    rule = A.rules_by_name(after)['BlockMiscBotsExceptGoogleFetcher']
    assert rule['Priority'] > A.rules_by_name(after)[A.BOT_CONTROL]['Priority']
    assert rule['Action']['Block']['CustomResponse']['CustomResponseBodyKey'] == HINT_KEY
    keys = {n['LabelMatchStatement']['Key'] for n in A._walk(rule) if 'LabelMatchStatement' in n}
    assert keys == {'awswaf:managed:aws:bot-control:bot:category:miscellaneous',
                    'awswaf:managed:aws:bot-control:bot:name:google_user_triggered_fetcher',
                    'awswaf:managed:aws:bot-control:bot:user_triggered:verified'}


def test_counting_a_category_without_a_reblock_rule_is_refused(acl):
    A.bot_control_statement(acl)['RuleActionOverrides'].append(
        {'Name': 'CategoryMiscellaneous', 'ActionToUse': {'Count': {}}})
    assert any('CategoryMiscellaneous is set to Count' in p for p in A.check(acl))
    acl['Rules'] = [r for r in acl['Rules'] if r['Name'] != 'BlockUnverifiedAI']
    assert any('CategoryAI is set to Count' in p for p in A.check(acl))
