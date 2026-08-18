"""
The site's outgoing mail.

Two senders, and they had drifted apart.  The project's own notifications --
added to a team, removed from a team, a subscribed project updated -- pass
EMAIL_HOST_USER_SECRET explicitly.  allauth's do not: password resets, email
confirmations and email-change notices go through its adapter, which uses
DEFAULT_FROM_EMAIL.  That was never set, so those went out as Django's default,
webmaster@localhost, which fails SPF for this domain -- the way a password reset
lands in a spam folder or is refused outright.

Delivery itself cannot be pinned in a test without mailing somebody, so these
use Django's locmem backend and check what would have been sent.  Whether the
credentials still work against smtp.gmail.com is a question for the deployment,
not for the suite.
"""

import pytest
from django.conf import settings
from django.core import mail
from django.test import override_settings

pytestmark = pytest.mark.integration


def _skip_without_mail_credentials():
    """CI has no mail credentials, and an unset sender is not a regression.

    EMAIL_HOST_USER comes from config.sh, which is not in the repository.  With
    it unset there is no address for DEFAULT_FROM_EMAIL to take, so it falls
    back to Django's placeholder legitimately and there is nothing to check.
    """
    if not settings.EMAIL_HOST_USER:
        pytest.skip('EMAIL_HOST_USER is unset; source caper/config.sh to run this')


def test_the_site_has_a_from_address_of_its_own():
    """Anything Django or allauth sends without naming a sender uses this."""
    _skip_without_mail_credentials()

    assert settings.DEFAULT_FROM_EMAIL == settings.EMAIL_HOST_USER
    assert settings.DEFAULT_FROM_EMAIL != 'webmaster@localhost', \
        'DEFAULT_FROM_EMAIL is still Django\'s placeholder'
    assert '@' in settings.DEFAULT_FROM_EMAIL
    assert not settings.DEFAULT_FROM_EMAIL.endswith('localhost')


def test_allauth_sends_password_resets_from_that_address():
    """The path a locked-out user depends on, and the one nobody watches."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    user_model = get_user_model()
    user = user_model.objects.create_user(
        username='email_from_test',
        email='email_from_test@example.com',
        password='CurrentPassword!123',
    )
    try:
        with override_settings(
                EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            mail.outbox = []
            client = Client(HTTP_HOST='localhost')
            response = client.post('/accounts/password/reset/',
                                   {'email': user.email})

            assert response.status_code in (200, 302)
            assert len(mail.outbox) == 1, 'No password reset email was sent'
            assert mail.outbox[0].from_email == settings.DEFAULT_FROM_EMAIL
    finally:
        user.delete()


def test_membership_notifications_name_the_site_as_the_sender():
    from caper.user_preferences import send_project_membership_changed_email

    # Pin the sender rather than reading it from config.sh, so this runs the
    # same way in CI, where there are no mail credentials.
    sender = 'notifications@example.org'

    with override_settings(
            EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
            EMAIL_HOST_USER_SECRET=sender):
        mail.outbox = []
        send_project_membership_changed_email(
            'You have been added to a project',
            'contacts/project_shared_mail_template.html',
            'recipient@example.com',
            'sharer@example.com',
            'A project',
            '000000000000000000000000',
        )

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.from_email == sender
    assert message.to == ['recipient@example.com']
    # Sent as HTML, and the template has to have rendered something -- an
    # unrendered template variable in an email cannot be corrected afterwards.
    assert message.content_subtype == 'html'
    assert 'A project' in message.body
    assert '{{' not in message.body
