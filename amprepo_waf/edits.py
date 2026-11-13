"""Named changes to the web ACL. Every WAF change is one function here, reviewed in git.

An edit takes a copy of the live ACL and the Bot Control defaults, and changes the copy in
place. It must be idempotent: run against an ACL that already has it, it changes nothing,
and the tool reports "already applied". Edits stay in this file after they ship, as the
record of what was done and why; delete one only together with its effect.
"""
from .acl import BOT_CONTROL, bot_control_statement, rules_by_name

EDITS = {}


def edit(name, summary):
    def register(func):
        EDITS[name] = (func, summary)
        return func
    return register


# -- automated-client hint (2026-11-13) ----------------------------------------------------

HINT_KEY = 'automated-client-hint'

# Relative paths: one ACL serves dev and prod, so the body cannot name a host.
HINT_TEXT = """403 Forbidden: this web page is not served to automated clients.

AmpliconRepository has a public JSON API for scripts and AI agents. It needs no key.
These paths are on this same host:

  /llms.txt              how to use the API, written for agents
  /api/v1/               API index
  /api/v1/openapi.json   OpenAPI schema

Please use the API rather than the HTML pages.

If you see this in an ordinary web browser, please report it at
https://github.com/AmpliconSuite/AmpliconRepository/issues
"""

HINT_RESPONSE = {'ResponseCode': 403, 'CustomResponseBodyKey': HINT_KEY}


@edit('automated-client-hint',
      'Blocked automated requests get a 403 that points at /llms.txt and the API. '
      'Every Bot Control sub-rule AWS ships as Block, and BlockUnverifiedAI, keep blocking '
      'but answer with this text instead of the bare AWS page. Overrides already set to '
      'something else (CategoryAI -> Count) are left alone.')
def automated_client_hint(web_acl, bot_control_defaults):
    web_acl.setdefault('CustomResponseBodies', {})[HINT_KEY] = {
        'ContentType': 'TEXT_PLAIN', 'Content': HINT_TEXT}

    rules_by_name(web_acl)['BlockUnverifiedAI']['Action'] = {
        'Block': {'CustomResponse': dict(HINT_RESPONSE)}}

    statement = bot_control_statement(web_acl)
    if statement is None:
        raise ValueError(f'{BOT_CONTROL} is not in the ACL')
    overrides = statement.setdefault('RuleActionOverrides', [])
    present = {o['Name']: o for o in overrides}
    for name, default in sorted(bot_control_defaults.items()):
        # TGT_ rules run only at the TARGETED inspection level; this ACL uses COMMON.
        if default != 'Block' or name.startswith('TGT_'):
            continue
        if name in present:
            if 'Block' in present[name]['ActionToUse']:
                present[name]['ActionToUse'] = {'Block': {'CustomResponse': dict(HINT_RESPONSE)}}
            continue
        overrides.append({'Name': name, 'ActionToUse': {'Block': {'CustomResponse': dict(HINT_RESPONSE)}}})


# -- let Google's verified user-triggered fetcher in (2026-11-13) ---------------------------

BOT_LABEL = 'awswaf:managed:aws:bot-control:bot:'
GOOGLE_FETCHER_RULE = 'BlockMiscBotsExceptGoogleFetcher'


def _label(suffix):
    return {'LabelMatchStatement': {'Scope': 'LABEL', 'Key': BOT_LABEL + suffix}}


@edit('allow-verified-google-fetcher',
      'Google\'s user-triggered fetcher (what Gemini and other Google products send when a '
      'person asks about a page) is verified by AWS but blocked by Bot Control\'s '
      'CategoryMiscellaneous: 18 blocks in the 30 days to 2026-11-13. Set that sub-rule to '
      'Count and add a rule after Bot Control that blocks the same category again, with the '
      'hint, except requests labelled both google_user_triggered_fetcher and '
      'user_triggered:verified. The same shape as CategoryAI / BlockUnverifiedAI.')
def allow_verified_google_fetcher(web_acl, bot_control_defaults):
    statement = bot_control_statement(web_acl)
    overrides = statement.setdefault('RuleActionOverrides', [])
    present = [o for o in overrides if o['Name'] == 'CategoryMiscellaneous']
    if present:
        present[0]['ActionToUse'] = {'Count': {}}     # in place, so the diff stays small
    else:
        overrides.append({'Name': 'CategoryMiscellaneous', 'ActionToUse': {'Count': {}}})

    rules = web_acl['Rules']
    existing = rules_by_name(web_acl).get(GOOGLE_FETCHER_RULE)
    priority = existing['Priority'] if existing else max(r['Priority'] for r in rules) + 1
    rule = {
        'Name': GOOGLE_FETCHER_RULE,
        'Priority': priority,
        'Statement': {'AndStatement': {'Statements': [
            _label('category:miscellaneous'),
            {'NotStatement': {'Statement': {'AndStatement': {'Statements': [
                _label('name:google_user_triggered_fetcher'),
                _label('user_triggered:verified'),
            ]}}}},
        ]}},
        'Action': {'Block': {'CustomResponse': dict(HINT_RESPONSE)}},
        'VisibilityConfig': {'SampledRequestsEnabled': True, 'CloudWatchMetricsEnabled': True,
                             'MetricName': GOOGLE_FETCHER_RULE},
    }
    rules[:] = [r for r in rules if r['Name'] != GOOGLE_FETCHER_RULE] + [rule]
