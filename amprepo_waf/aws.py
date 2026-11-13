"""The AWS calls, through the installed `aws` CLI so the tool needs no Python packages.

Going through the CLI also keeps the base64 trap closed: get-web-acl emits match strings
base64-encoded, the CLI's JSON input expects them that way, and the ACL makes the round
trip without being re-encoded.
"""
import json
import os
import subprocess
import tempfile

from .acl import ACL_NAME, BOT_CONTROL, REGION, SCOPE


class AwsError(Exception):
    pass


class AwsCli:
    def __init__(self, profile=None, region=REGION):
        self.profile = profile
        self.region = region

    def _run(self, *args, json_file=None, with_region=True):
        cmd = ['aws', 'wafv2', *args, '--output', 'json']
        if with_region:
            cmd += ['--region', self.region]
        if self.profile:
            cmd += ['--profile', self.profile]
        path = None
        try:
            if json_file is not None:
                flag, payload = json_file
                fd, path = tempfile.mkstemp(suffix='.json')
                with os.fdopen(fd, 'w') as handle:
                    json.dump(payload, handle)
                cmd += [flag, f'file://{path}']
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        finally:
            if path:
                os.unlink(path)
        if done.returncode != 0:
            raise AwsError(f'aws wafv2 {args[0]} failed: {done.stderr.strip()}')
        return json.loads(done.stdout) if done.stdout.strip() else {}

    def get_web_acl(self, name=ACL_NAME):
        """(web_acl, lock_token). The token is AWS's guard against two writers."""
        listed = self._run('list-web-acls', '--scope', SCOPE)
        ids = [acl['Id'] for acl in listed.get('WebACLs', []) if acl['Name'] == name]
        if len(ids) != 1:
            raise AwsError(f'expected one web ACL named {name}, found {len(ids)}')
        got = self._run('get-web-acl', '--name', name, '--id', ids[0], '--scope', SCOPE)
        return got['WebACL'], got['LockToken']

    def accepted_fields(self):
        """The top-level fields update-web-acl accepts, from this CLI's own skeleton."""
        done = subprocess.run(['aws', 'wafv2', 'update-web-acl', '--generate-cli-skeleton', 'input'],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise AwsError(f'cannot read the update-web-acl skeleton: {done.stderr.strip()}')
        return set(json.loads(done.stdout))

    def bot_control_defaults(self, web_acl):
        """Each Bot Control sub-rule and AWS's own action for it, at the version in use."""
        statement = next(r['Statement']['ManagedRuleGroupStatement'] for r in web_acl['Rules']
                         if r['Name'] == BOT_CONTROL)
        args = ['describe-managed-rule-group', '--vendor-name', statement['VendorName'],
                '--name', statement['Name'], '--scope', SCOPE]
        if statement.get('Version'):
            args += ['--version-name', statement['Version']]
        described = self._run(*args)
        return {rule['Name']: next(iter(rule['Action'])) for rule in described['Rules']}

    def check_capacity(self, rules):
        """AWS validates the rules and prices them in WCUs. Raises if it rejects them."""
        return self._run('check-capacity', '--scope', SCOPE, json_file=('--rules', rules))['Capacity']

    def update_web_acl(self, params):
        return self._run('update-web-acl', json_file=('--cli-input-json', params))
