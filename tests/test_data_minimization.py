"""
Regression tests for the things this codebase deliberately stopped collecting.

Each of these is easy to reintroduce by accident -- a template copied from
before the change, a scope pasted back from a Globus example, a debugging
``print`` that names the user it is about. They are cheap to assert and
expensive to notice going missing, so they are asserted.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / 'caper' / 'templates'


# ---------------------------------------------------------------------------
# Google Analytics
# ---------------------------------------------------------------------------

GA_MARKERS = ('gtag', 'googletagmanager', 'G-RLJSFEY3H0', 'google-analytics',
              'dataLayer', 'analytics.js')


def test_no_google_analytics_in_any_template():
    offenders = []
    for template in TEMPLATE_DIR.rglob('*.html'):
        text = template.read_text(errors='ignore')
        for marker in GA_MARKERS:
            if marker in text:
                offenders.append(f'{template.relative_to(REPO_ROOT)}: {marker}')

    assert not offenders, 'Google Analytics markers found: ' + '; '.join(offenders)


def test_no_google_analytics_in_settings():
    settings_text = (REPO_ROOT / 'caper' / 'caper' / 'settings.py').read_text()

    for marker in GA_MARKERS:
        assert marker not in settings_text


@pytest.mark.integration
@pytest.mark.parametrize('path', ['/', '/privacy/', '/terms/', '/accounts/login/'])
def test_no_google_analytics_in_rendered_pages(path):
    """The templates are the source, but the rendered page is the thing shipped.

    Both base templates carried the tag, so this covers the account pages that
    extend the second one as well as the main site.
    """
    from django.test import Client

    body = Client(HTTP_HOST='localhost').get(path, follow=True).content.decode()

    for marker in GA_MARKERS:
        assert marker not in body, f'{marker!r} present in rendered {path}'


def test_privacy_page_does_not_describe_analytics_cookies():
    """With GA gone there are no analytics cookies left to disclose."""
    privacy = (TEMPLATE_DIR / 'pages' / 'privacy.html').read_text()

    assert 'analytics' in privacy  # it says there are none...
    assert 'does not use analytics' in re.sub(r'\s+', ' ', privacy)


# ---------------------------------------------------------------------------
# Globus OAuth scopes
# ---------------------------------------------------------------------------

def test_globus_scope_is_authentication_only():
    """The Transfer scope grants access to the user's Globus endpoints.

    Nothing in the site calls the Transfer API, so requesting it asked every
    person signing in to hand over far more than a login needs.
    """
    from django.conf import settings

    scopes = settings.SOCIALACCOUNT_PROVIDERS['globus']['SCOPE']

    assert sorted(scopes) == ['email', 'openid', 'profile']
    assert not any('transfer' in scope for scope in scopes)


def test_no_globus_transfer_scope_in_any_active_code():
    """Comments may still name the scope -- settings.py explains why it went.

    What must not come back is a live reference, so each line is checked with its
    comment stripped off. This file and the spec are excluded for the obvious
    reason that they have to spell the scope out to talk about it.
    """
    from conftest import tracked_python_files

    offenders = []
    for relative in tracked_python_files(REPO_ROOT):
        path = REPO_ROOT / relative
        if {'site-packages', 'tests'} & set(path.parts):
            continue
        for lineno, line in enumerate(path.read_text(errors='ignore').splitlines(), 1):
            code = line.split('#', 1)[0]
            if 'transfer.api.globus.org' in code:
                offenders.append(f'{path.relative_to(REPO_ROOT)}:{lineno}')

    assert not offenders, f'Globus Transfer scope still requested at {offenders}'


def test_globus_login_is_still_offered():
    """Scope reduction must not remove the provider itself."""
    from django.conf import settings

    assert 'allauth.socialaccount.providers.globus' in settings.INSTALLED_APPS
    assert settings.SOCIALACCOUNT_PROVIDERS['globus']['APP']['client_id']


@pytest.mark.integration
def test_globus_login_url_still_resolves():
    """An application-level check that the provider still wires up, no creds needed."""
    from django.test import Client

    body = Client(HTTP_HOST='localhost').get('/accounts/login/').content.decode()

    assert '/accounts/globus/login/' in body


# ---------------------------------------------------------------------------
# Access log retention
# ---------------------------------------------------------------------------

def test_logrotate_keeps_about_three_months():
    config = (REPO_ROOT / 'logrotate-ampliconrepo.conf').read_text()

    assert re.search(r'^\s*weekly\s*$', config, re.MULTILINE)
    assert re.search(r'^\s*rotate 12\s*$', config, re.MULTILINE)
    assert 'rotate 52' not in config

    # Compression and the copytruncate behaviour are load-bearing and unrelated
    # to retention; changing the count must not quietly drop them.
    for directive in ('compress', 'copytruncate', 'missingok', 'notifempty'):
        assert re.search(rf'^\s*{directive}\s*$', config, re.MULTILINE)


def test_no_documentation_still_promises_a_year_of_logs():
    readme = (REPO_ROOT / 'README.md').read_text()

    assert 'keeps 52 compressed archives' not in readme
    assert 'keeps 12 compressed archives' in readme


# ---------------------------------------------------------------------------
# Personal information in application logs
# ---------------------------------------------------------------------------

def test_notification_code_does_not_log_addresses():
    """user_preferences.py decides who to email, so nearly every local is an address.

    The rule for this module is that identifiers go in the project document and
    the audit log, never into stdout.
    """
    source = (REPO_ROOT / 'caper' / 'caper' / 'user_preferences.py').read_text()

    # No print() at all here: this module used them for exactly the values that
    # must not be logged, so the whole idiom is retired rather than policed.
    assert 'print(' not in source

    log_calls = re.findall(r'logger\.\w+\((.*?)\)\n', source, re.DOTALL)
    assert log_calls, 'expected the module to log something'
    for call in log_calls:
        assert '.email' not in call, f'log call passes an email: {call.strip()[:80]}'
        assert 'subscriber_email' not in call, (
            f'log call passes a subscriber address: {call.strip()[:80]}')


def test_audit_log_line_does_not_repeat_the_submitter():
    """The submitter belongs in the audit record, not in the application log too."""
    source = (REPO_ROOT / 'caper' / 'caper' / 'views.py').read_text()

    # Still recorded in the structured audit entry -- that is the provenance.
    assert "'user_email': user_email," in source

    audit_line = re.search(
        r'logging\.info\(f"Audit log written for project.*?\)\n', source, re.DOTALL)
    assert audit_line, 'audit log confirmation line not found'
    assert 'user_email' not in audit_line.group(0)
