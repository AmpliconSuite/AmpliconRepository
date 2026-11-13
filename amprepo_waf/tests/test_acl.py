import copy

import pytest

from amprepo_waf import acl as A
from amprepo_waf.tests.conftest import ACCEPTED, DEFAULTS


def _set_priority(web_acl, name, priority):
    A.rules_by_name(web_acl)[name]['Priority'] = priority


def test_the_live_shape_passes(acl):
    assert A.check(acl, DEFAULTS) == []


@pytest.mark.parametrize('earlier, later, _why', A.ORDER)
def test_each_required_order_is_enforced(acl, earlier, later, _why):
    a, b = A.rules_by_name(acl)[earlier]['Priority'], A.rules_by_name(acl)[later]['Priority']
    _set_priority(acl, earlier, b)
    _set_priority(acl, later, a)
    assert any(f'{earlier} must come before {later}' in p for p in A.check(acl))


def test_a_missing_rule_is_reported(acl):
    acl['Rules'] = [r for r in acl['Rules'] if r['Name'] != 'AllowDiscoveryFiles']
    assert any('rule missing: AllowDiscoveryFiles' in p for p in A.check(acl))


def test_duplicate_priorities_are_reported(acl):
    _set_priority(acl, 'AllowApiV1', 4)
    assert any('share a priority' in p for p in A.check(acl))


def test_dropping_the_body_a_rule_uses_is_caught(acl):
    """The trap from 2026-09-05: an update without CustomResponseBodies breaks RateLimitApiV1."""
    del acl['CustomResponseBodies']
    assert any("'api-rate-limited' is used but not defined" in p for p in A.check(acl))


def test_losing_the_categoryai_override_is_caught(acl):
    A.bot_control_statement(acl)['RuleActionOverrides'] = []
    assert any('CategoryAI -> Count is gone' in p for p in A.check(acl))


def test_an_override_that_now_disagrees_with_aws_is_caught(acl):
    A.bot_control_statement(acl)['RuleActionOverrides'].append(
        {'Name': 'CategoryHttpLibrary', 'ActionToUse': {'Block': {}}})
    assert A.check(acl, DEFAULTS) == []
    assert any('AWS now ships it as Count' in p
               for p in A.check(acl, {**DEFAULTS, 'CategoryHttpLibrary': 'Count'}))
    gone = {k: v for k, v in DEFAULTS.items() if k != 'CategoryHttpLibrary'}
    assert any('no sub-rule CategoryHttpLibrary' in p for p in A.check(acl, gone))


def test_update_input_carries_every_field_forward(acl):
    params = A.update_input(acl, 'tok', ACCEPTED)
    assert params['LockToken'] == 'tok' and params['Scope'] == 'REGIONAL'
    assert params['CustomResponseBodies'] == acl['CustomResponseBodies']
    assert params['OnSourceDDoSProtectionConfig'] == acl['OnSourceDDoSProtectionConfig']
    assert params['Rules'] == acl['Rules']   # match strings stay base64, byte for byte
    assert not set(params) & A.READ_ONLY_FIELDS
    assert set(acl) - A.READ_ONLY_FIELDS <= set(params)


def test_a_field_aws_adds_later_stops_the_update(acl):
    acl['SomethingNew'] = {'x': 1}
    with pytest.raises(A.UnknownField, match='SomethingNew'):
        A.update_input(acl, 'tok', ACCEPTED)


def test_a_field_the_cli_accepts_is_carried_without_a_code_change(acl):
    acl['SomethingNew'] = {'x': 1}
    assert A.update_input(acl, 'tok', ACCEPTED | {'SomethingNew'})['SomethingNew'] == {'x': 1}


def test_diff_shows_decoded_match_strings(acl):
    after = copy.deepcopy(acl)
    after['Rules'][4]['Statement']['OrStatement']['Statements'].append(
        {'ByteMatchStatement': {'SearchString': A.b64('/sitemap.xml')}})
    assert '/sitemap.xml (b64)' in A.diff(acl, after)
