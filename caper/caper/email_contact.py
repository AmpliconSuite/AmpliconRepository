"""Where the site's mail tells people to go with a question.

The mail is sent from ``EMAIL_HOST_USER``, an account nobody answers from, so a
reply to it reached no one.  Every notification now sets Reply-To to
``SITE_CONTACT_EMAIL`` and ends with that address and the issue tracker; the
values live in settings so the templates and the plain-text mail cannot
disagree about them.
"""

from django.conf import settings


def contact_context():
    """Template variables for ``contacts/_contact_footer.html``."""
    return {'CONTACT_EMAIL': settings.SITE_CONTACT_EMAIL,
            'ISSUES_URL': settings.SITE_ISSUES_URL}


def reply_to():
    return [settings.SITE_CONTACT_EMAIL]


def contact_footer_text():
    """The footer for mail sent as plain text."""
    return (f"Questions or problems? Open an issue at {settings.SITE_ISSUES_URL}\n"
            f"or email {settings.SITE_CONTACT_EMAIL}.")
