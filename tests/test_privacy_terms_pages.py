"""
The Privacy and Terms pages, and the footer links that lead to them.

Both pages are static prose, so there is not much behaviour to test. What is
worth pinning is that they stay reachable at their canonical URLs, that they
stay reachable *without* an account (a privacy notice you have to log in to read
is no notice at all), and that the footer keeps pointing at them.
"""

import re

import pytest
from django.test import Client
from django.urls import reverse


pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    return Client(HTTP_HOST='localhost')


def _text(client, path):
    """Page content with runs of whitespace collapsed to single spaces.

    The prose is hard-wrapped in the template, so a phrase the spec asks for is
    as likely as not to straddle a newline. Collapsing first means these
    assertions test the wording rather than where the lines happen to break.
    """
    return re.sub(r'\s+', ' ', client.get(path).content.decode())


@pytest.mark.parametrize('url_name,path', [('privacy', '/privacy/'),
                                           ('terms', '/terms/')])
def test_page_is_reachable_anonymously(client, url_name, path):
    assert reverse(url_name) == path

    response = client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize('path,expected', [
    ('/privacy', '/privacy/'),
    ('/terms', '/terms/'),
])
def test_slashless_url_redirects(client, path, expected):
    """These get typed and pasted by hand, so the bare form has to land."""
    response = client.get(path)

    assert response.status_code == 301
    assert response['Location'].endswith(expected)


# ---------------------------------------------------------------------------
# Content the spec requires to be present
# ---------------------------------------------------------------------------

def test_privacy_page_covers_the_required_topics(client):
    body = _text(client, '/privacy/')

    # Operator, stated without claiming who the legal controller is.
    assert 'Bafna Lab' in body
    assert 'University of California San Diego' in body

    for topic in ('username', 'email address', 'Globus', 'Google',
                  'CSRF', 'CAPTCHA', 'IP address', 'user-agent',
                  'provenance', 'API'):
        assert topic in body, f"privacy page does not mention {topic!r}"

    # The promises that have to be plain.
    assert 'does not serve targeted advertising' in body
    assert "does not sell users' personal information" in body

    # Retention is deliberately vague, because the repository does not control
    # every layer that logs a request.
    assert 'retained for a limited period' in body
    assert not re.search(r'retained for \d+ (day|week|month|year)', body)

    # Contact route for privacy questions, corrections, and deletion.
    assert 'mailto:jluebeck@ucsd.edu' in body
    assert 'account deleted' in body

    # Institutional statement, linked as additional information.
    assert 'https://ucsd.edu/about/privacy.html' in body


def test_terms_page_covers_the_required_topics(client):
    body = _text(client, '/terms/')

    assert 'academic' in body
    assert 'as is' in body                      # no warranty
    assert 'not medical advice' in body.lower()
    assert 'clinical' in body
    assert 'authorization to upload' in body    # user submissions
    assert 'security controls' in body          # abuse
    assert 'external' in body.lower()
    assert 'https://ucsd.edu/about/terms-of-use.html' in body


def test_pages_link_to_each_other(client):
    assert '/terms/' in client.get('/privacy/').content.decode()
    assert '/privacy/' in client.get('/terms/').content.decode()


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/', '/privacy/', '/terms/'])
def test_footer_links_appear_sitewide(client, path):
    body = client.get(path).content.decode()

    footer = body[body.index('<div class="footer'):]
    assert '>Privacy</a>' in footer
    assert '>Terms</a>' in footer
    assert 'href="/privacy/"' in footer
    assert 'href="/terms/"' in footer


def test_footer_links_are_not_a_banner_or_modal():
    """The spec asks for footer links and explicitly not for a consent banner."""
    from pathlib import Path

    footer = (Path(__file__).parents[1] / 'caper' / 'templates' / 'includes' /
              'footer.html').read_text().lower()

    for unwanted in ('modal', 'cookie-consent', 'cookiebanner', 'cookie banner',
                     'onetrust', 'cookiebot'):
        assert unwanted not in footer
