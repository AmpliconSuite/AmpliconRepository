"""A web ACL shaped like the live one (2026-11-13), built by hand: no AWS, no copy of prod."""
import copy

import pytest

from amprepo_waf.acl import BOT_CONTROL, KNOWN_BAD_INPUTS, b64

ACCEPTED = {'Name', 'Scope', 'Id', 'DefaultAction', 'Description', 'Rules', 'VisibilityConfig',
            'DataProtectionConfig', 'LockToken', 'CustomResponseBodies', 'CaptchaConfig',
            'ChallengeConfig', 'TokenDomains', 'AssociationConfig', 'OnSourceDDoSProtectionConfig',
            'ApplicationConfig', 'MonetizationConfig'}

DEFAULTS = {'CategoryAI': 'Block', 'CategoryHttpLibrary': 'Block', 'CategorySearchEngine': 'Block',
            'SignalNonBrowserUserAgent': 'Block', 'SignalKnownBotDataCenter': 'Block',
            'TGT_VolumetricSession': 'Captcha', 'TGT_TokenReuseIp': 'Count'}


def _match(path, how='EXACTLY'):
    return {'ByteMatchStatement': {'SearchString': b64(path), 'FieldToMatch': {'UriPath': {}},
                                   'TextTransformations': [{'Priority': 0, 'Type': 'NONE'}],
                                   'PositionalConstraint': how}}


def _vis(name):
    return {'SampledRequestsEnabled': True, 'CloudWatchMetricsEnabled': True, 'MetricName': name}


def _rule(priority, name, statement, action=None, override=None):
    rule = {'Name': name, 'Priority': priority, 'Statement': statement, 'VisibilityConfig': _vis(name)}
    if override is not None:
        rule['OverrideAction'] = override
    else:
        rule['Action'] = action
    return rule


def live_like():
    return {
        'Name': 'Amplicon_WAF', 'Id': 'abc', 'ARN': 'arn:aws:wafv2:...', 'Capacity': 273,
        'ManagedByFirewallManager': False, 'RetrofittedByFirewallManager': False,
        'LabelNamespace': 'awswaf:123:webacl:Amplicon_WAF:',
        'DefaultAction': {'Allow': {}}, 'Description': '', 'VisibilityConfig': _vis('Amplicon_WAF'),
        'OnSourceDDoSProtectionConfig': {'ALBLowReputationMode': 'ACTIVE_UNDER_DDOS'},
        'CustomResponseBodies': {'api-rate-limited': {'ContentType': 'APPLICATION_JSON',
                                                      'Content': '{"detail": "slow down"}'}},
        'Rules': [
            _rule(0, 'AllowCurlToAPIEndpoints', _match('/upload_api/', 'STARTS_WITH'), {'Allow': {}}),
            _rule(1, 'RateLimitApiV1', {'RateBasedStatement': {
                'Limit': 600, 'EvaluationWindowSec': 300, 'AggregateKeyType': 'IP',
                'ScopeDownStatement': _match('/api/v1/', 'STARTS_WITH')}},
                {'Block': {'CustomResponse': {'ResponseCode': 429,
                                              'CustomResponseBodyKey': 'api-rate-limited'}}}),
            _rule(2, KNOWN_BAD_INPUTS, {'ManagedRuleGroupStatement': {
                'VendorName': 'AWS', 'Name': 'AWSManagedRulesKnownBadInputsRuleSet'}}, override={'None': {}}),
            _rule(3, 'AllowApiV1', _match('/api/v1/', 'STARTS_WITH'), {'Allow': {}}),
            _rule(4, 'AllowDiscoveryFiles', {'OrStatement': {'Statements': [
                _match('/robots.txt'), _match('/llms.txt')]}}, {'Allow': {}}),
            _rule(5, BOT_CONTROL, {'ManagedRuleGroupStatement': {
                'VendorName': 'AWS', 'Name': 'AWSManagedRulesBotControlRuleSet',
                'RuleActionOverrides': [{'Name': 'CategoryAI', 'ActionToUse': {'Count': {}}}],
                'ManagedRuleGroupConfigs': [{'AWSManagedRulesBotControlRuleSet': {'InspectionLevel': 'COMMON'}}]}},
                override={'None': {}}),
            _rule(6, 'BlockUnverifiedAI', {'LabelMatchStatement': {
                'Scope': 'LABEL', 'Key': 'awswaf:managed:aws:bot-control:bot:category:ai'}}, {'Block': {}}),
        ],
    }


class FakeAws:
    """Stands in for AwsCli: holds one ACL, records what was sent."""

    def __init__(self, web_acl=None, defaults=DEFAULTS, accepted=ACCEPTED, store=None):
        self.web_acl = web_acl or live_like()
        self.defaults = dict(defaults)
        self.accepted = set(accepted)
        self.sent = []
        self.store = store   # what the live ACL becomes after an update; default: what was sent
        self.token = 0

    def get_web_acl(self):
        return copy.deepcopy(self.web_acl), f'token-{self.token}'

    def accepted_fields(self):
        return set(self.accepted)

    def bot_control_defaults(self, _web_acl):
        return dict(self.defaults)

    def check_capacity(self, rules):
        return 273

    def update_web_acl(self, params):
        assert params['LockToken'] == f'token-{self.token}'
        self.sent.append(copy.deepcopy(params))
        new = self.store(params) if self.store else {
            k: v for k, v in params.items() if k not in ('Scope', 'LockToken')}
        for key in ('ARN', 'Capacity', 'LabelNamespace', 'ManagedByFirewallManager',
                    'RetrofittedByFirewallManager'):
            new[key] = self.web_acl[key]
        self.web_acl = copy.deepcopy(new)
        self.token += 1
        return {'NextLockToken': f'token-{self.token}'}


@pytest.fixture
def acl():
    return live_like()


@pytest.fixture
def fake():
    return FakeAws()
