"""Pure functions over the site's WAF web ACL, as `aws wafv2 get-web-acl` returns it.

Nothing here calls AWS, so all of it is unit-tested. The checks encode what the site
depends on; each says why, so that a future edit which trips one can tell whether the
check or the edit is wrong.
"""
import base64
import copy
import difflib
import json

ACL_NAME = 'Amplicon_WAF'
REGION = 'us-east-1'     # the ACL and its log group live here, not in us-east-2
SCOPE = 'REGIONAL'

KNOWN_BAD_INPUTS = 'AWS-AWSManagedRulesKnownBadInputsRuleSet'
BOT_CONTROL = 'AWS-AWSManagedRulesBotControlRuleSet'

# (earlier, later, why). Each rule named here must exist, and `earlier` must have the lower
# priority number. A terminating rule stops evaluation, so these orders are behaviour.
ORDER = [
    ('AllowCurlToAPIEndpoints', KNOWN_BAD_INPUTS,
     'KnownBadInputs inspects request bodies; aggregator uploads are tarballs full of log '
     'text and paths, and it would reject legitimate uploads'),
    ('RateLimitApiV1', 'AllowApiV1',
     'AllowApiV1 terminates; a rate limit placed after it never counts an API request'),
    (KNOWN_BAD_INPUTS, 'AllowApiV1',
     'exploit payloads sent to the open API must still be inspected'),
    (KNOWN_BAD_INPUTS, 'AllowDiscoveryFiles',
     'a Log4j payload aimed at /robots.txt must still be blocked'),
    ('AllowApiV1', BOT_CONTROL,
     'the API is open to any client, including curl and scripts'),
    ('AllowDiscoveryFiles', BOT_CONTROL,
     'robots.txt, llms.txt, sitemap.xml and .well-known must reach every crawler and agent'),
    (BOT_CONTROL, 'BlockUnverifiedAI',
     'BlockUnverifiedAI matches labels that exist only after Bot Control has run'),
]

# Bot Control sub-rules whose action the site overrides on purpose, and to what.
# CategoryAI blocked the verified ChatGPT-User fetcher that a person's question sends;
# it is set to Count and BlockUnverifiedAI blocks only the unverified ones (2026-09-15).
REQUIRED_OVERRIDES = {'CategoryAI': 'Count'}

# Fields get-web-acl returns that describe the ACL but cannot be sent back.
READ_ONLY_FIELDS = {
    'ARN', 'Capacity', 'LabelNamespace', 'ManagedByFirewallManager',
    'RetrofittedByFirewallManager', 'PreProcessFirewallManagerRuleGroups',
    'PostProcessFirewallManagerRuleGroups',
}


class UnknownField(Exception):
    """get-web-acl returned a field this tool does not know how to carry forward."""


def b64(text):
    """Encode a match string the way the CLI's JSON input wants it (and get-web-acl emits it)."""
    return base64.b64encode(text.encode()).decode()


def rules_by_name(web_acl):
    return {rule['Name']: rule for rule in web_acl.get('Rules', [])}


def bot_control_statement(web_acl):
    rule = rules_by_name(web_acl).get(BOT_CONTROL)
    if rule is None:
        return None
    return rule['Statement'].get('ManagedRuleGroupStatement')


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def referenced_body_keys(web_acl):
    """Every CustomResponseBodyKey any rule, override or the default action points at."""
    return {node['CustomResponseBodyKey'] for node in _walk(web_acl)
            if 'CustomResponseBodyKey' in node}


def check(web_acl, bot_control_defaults=None):
    """Return a list of problems with this ACL; empty means it is safe to send.

    `bot_control_defaults` maps each Bot Control sub-rule to AWS's own action for it
    (from describe-managed-rule-group). Without it the override checks are skipped.
    """
    problems = []
    rules = web_acl.get('Rules', [])
    by_name = rules_by_name(web_acl)

    priorities = [rule['Priority'] for rule in rules]
    if len(priorities) != len(set(priorities)):
        problems.append(f'two rules share a priority: {sorted(priorities)}')
    if len(by_name) != len(rules):
        problems.append('two rules share a name')

    for earlier, later, why in ORDER:
        missing = [name for name in (earlier, later) if name not in by_name]
        if missing:
            problems.append(f'rule missing: {", ".join(missing)} (needed because {why})')
        elif by_name[earlier]['Priority'] >= by_name[later]['Priority']:
            problems.append(f'{earlier} must come before {later}: {why}')

    bodies = set(web_acl.get('CustomResponseBodies', {}))
    for key in sorted(referenced_body_keys(web_acl) - bodies):
        problems.append(f'custom response body {key!r} is used but not defined; '
                        'the rule using it would fail')

    statement = bot_control_statement(web_acl)
    if statement is not None:
        overrides = {o['Name']: o['ActionToUse'] for o in statement.get('RuleActionOverrides', [])}
        for name, action in REQUIRED_OVERRIDES.items():
            if action not in overrides.get(name, {}):
                problems.append(f'Bot Control override {name} -> {action} is gone')
        if bot_control_defaults is not None:
            for name, action in sorted(overrides.items()):
                if name not in bot_control_defaults:
                    problems.append(f'Bot Control has no sub-rule {name} any more; '
                                    'its override does nothing')
                elif 'Block' in action and bot_control_defaults[name] != 'Block':
                    problems.append(
                        f'Bot Control override pins {name} to Block, but AWS now ships it as '
                        f'{bot_control_defaults[name]}; decide which is wanted')
    return problems


def update_input(web_acl, lock_token, accepted_fields):
    """The update-web-acl input that leaves everything not edited exactly as it was.

    update-web-acl replaces the whole ACL: a field left out is deleted. So everything
    get-web-acl returned is carried forward, and a field this tool has never seen stops
    the run instead of being dropped. `accepted_fields` comes from the installed CLI's
    own skeleton, so a field AWS adds later is carried without a code change.
    """
    unknown = set(web_acl) - set(accepted_fields) - READ_ONLY_FIELDS
    if unknown:
        raise UnknownField(
            f'get-web-acl returned {sorted(unknown)}, which update-web-acl does not accept '
            'and this tool does not know to be read-only. Find out what it is before '
            'sending an update: it would be dropped.')
    params = {key: copy.deepcopy(value) for key, value in web_acl.items()
              if key in accepted_fields}
    params['Scope'] = SCOPE
    params['LockToken'] = lock_token
    return params


def comparable(web_acl, accepted_fields):
    """The parts of an ACL an update sets, for comparing intended with live."""
    return {key: value for key, value in web_acl.items()
            if key in accepted_fields and key not in ('Scope', 'LockToken')}


def _readable(web_acl):
    """A copy with base64 match strings decoded, for a diff a person can read."""
    out = copy.deepcopy(web_acl)
    for node in _walk(out):
        value = node.get('SearchString')
        if isinstance(value, str):
            try:
                node['SearchString'] = f'{base64.b64decode(value, validate=True).decode()} (b64)'
            except (ValueError, UnicodeDecodeError):
                pass
    for key in READ_ONLY_FIELDS:
        out.pop(key, None)
    return out


def diff(before, after):
    def lines(acl):
        return json.dumps(_readable(acl), indent=1, sort_keys=True).splitlines(keepends=True)
    return ''.join(difflib.unified_diff(lines(before), lines(after), 'live', 'proposed', n=4))


def summary(web_acl):
    """One line per rule, in evaluation order."""
    out = []
    for rule in sorted(web_acl.get('Rules', []), key=lambda r: r['Priority']):
        action = rule.get('Action') or rule.get('OverrideAction') or {}
        verb = next(iter(action), '?')
        response = action.get(verb, {}).get('CustomResponse') if isinstance(action.get(verb), dict) else None
        detail = f' -> body {response["CustomResponseBodyKey"]}' if response and 'CustomResponseBodyKey' in response else ''
        out.append(f'{rule["Priority"]:>3}  {rule["Name"]:<45} {verb}{detail}')
        group = rule['Statement'].get('ManagedRuleGroupStatement')
        for override in (group or {}).get('RuleActionOverrides', []):
            use = override['ActionToUse']
            o_verb = next(iter(use))
            o_resp = (use[o_verb] or {}).get('CustomResponse') or {}
            o_detail = f' -> body {o_resp["CustomResponseBodyKey"]}' if 'CustomResponseBodyKey' in o_resp else ''
            out.append(f'       override {override["Name"]:<36} {o_verb}{o_detail}')
    for key in sorted(web_acl.get('CustomResponseBodies', {})):
        out.append(f'  body {key}')
    return '\n'.join(out)
