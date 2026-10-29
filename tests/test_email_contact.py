"""The site's mail tells people where a question goes, and a reply reaches them.

The sending account is not one anybody answers from (Jens, 2026-10-29), so the
Reply-To and the footer point at SITE_CONTACT_EMAIL and the issue tracker.
"""

from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from caper.email_contact import contact_context, contact_footer_text, reply_to

CONTACTS = Path(__file__).resolve().parents[1] / 'caper' / 'templates' / 'contacts'
CAPER = Path(__file__).resolve().parents[1] / 'caper' / 'caper'


def test_every_notification_template_carries_the_footer():
    templates = sorted(p.name for p in CONTACTS.glob('*.html') if not p.name.startswith('_'))
    assert templates
    for name in templates:
        html = render_to_string(f'contacts/{name}', {**contact_context(), 'body': ''})
        assert settings.SITE_ISSUES_URL in html, name
        assert f'mailto:{settings.SITE_CONTACT_EMAIL}' in html, name


def test_plain_text_footer_and_reply_to():
    assert settings.SITE_CONTACT_EMAIL in contact_footer_text()
    assert settings.SITE_ISSUES_URL in contact_footer_text()
    assert reply_to() == [settings.SITE_CONTACT_EMAIL]


def test_no_notification_replies_to_the_sending_account():
    for path in CAPER.glob('*.py'):
        source = path.read_text()
        assert 'reply_to=[settings.EMAIL_HOST_USER' not in source, path.name
