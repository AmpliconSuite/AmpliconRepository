"""
Signing in has to finish the trip a private link started.

Private projects are shared as links: a member mails a colleague the project
URL, or a sample URL, or a link to one amplicon.  The recipient has no session
yet, so the site sends them to the login form -- and the only thing that makes
that useful is carrying the page they were trying to reach through the login
and back out the other side.

Four things are pinned here:

  * every private page attaches ?next= with the page that was asked for,
  * allauth honours it, through the password form and through the Globus and
    Google buttons,
  * a visitor who is signed in and still has no access gets a 404 rather than
    the login form, which for them is a redirect loop,
  * the amplicon page is gated at all -- it was not.
"""

import re
import uuid

import pytest
from bson.objectid import ObjectId
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.integration

PASSWORD = 'PrivateLinkPassword!123'


@pytest.fixture
def outsider():
    """A real Django user who is not a member of the project."""
    user_model = get_user_model()
    suffix = uuid.uuid4().hex
    user = user_model.objects.create_user(
        username=f'private_link_{suffix}',
        email=f'private_link_{suffix}@example.com',
        password=PASSWORD,
    )
    try:
        yield user
    finally:
        user.delete()


@pytest.fixture
def private_project(mongo_collection):
    """A private project owned by somebody else, with one sample and feature."""
    doc = {
        'project_name': f'PrivateLinkTest_{uuid.uuid4().hex[:8]}',
        'description': 'Private project used to test post-login redirects',
        'private': 'private',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'previous_versions': [],
        'runs': {
            'run1': [{
                'Sample_name': 'Sample_001',
                'Feature_ID': 'Sample_001_amplicon1_ecDNA_1',
                'Feature_BED_file': 'Not Provided',
                'Classification': 'ecDNA',
                'Oncogenes': [],
            }],
        },
        'project_members': ['somebody_else@example.com'],
        'views': 0,
        'downloads': 0,
        'date': '2024-01-01',
        'sample_count': 1,
    }
    result = mongo_collection.insert_one(doc)
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})
    try:
        yield project_id
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


@pytest.fixture
def anonymous_client():
    return Client(HTTP_HOST='localhost')


def _private_urls(project_id):
    """The three kinds of page a private link can point at."""
    return {
        'project': f'/project/{project_id}',
        'sample': f'/project/{project_id}/sample/Sample_001',
        'feature': (f'/project/{project_id}/sample/Sample_001'
                    f'/feature/Sample_001_amplicon1_ecDNA_1'),
    }


@pytest.mark.parametrize('page', ['project', 'sample', 'feature'])
def test_private_page_sends_a_stranger_to_login_with_the_page_attached(
        anonymous_client, private_project, page):
    url = _private_urls(private_project)[page]

    response = anonymous_client.get(url)

    assert response.status_code == 302, \
        f'{page} page did not redirect an anonymous visitor'
    assert response.url.startswith('/accounts/login/'), \
        f'{page} page redirected to {response.url!r}, not the login form'
    # The whole point: the page asked for survives into the login form.
    assert f'next={url}' in response.url.replace('%2F', '/'), \
        f'{page} page lost the destination: {response.url!r}'


@pytest.mark.parametrize('page', ['project', 'sample', 'feature'])
def test_signing_in_lands_on_the_page_the_link_pointed_at(
        anonymous_client, private_project, outsider, page):
    """The round trip, not just the ?next= on the way in.

    The user signing in is not a member, so the destination answers 404 once
    they get there.  What is pinned is that allauth sends them to the
    destination at all, rather than to /accounts/profile/.
    """
    url = _private_urls(private_project)[page]

    login_url = anonymous_client.get(url).url
    login = anonymous_client.get(login_url)
    assert login.status_code == 200
    # allauth carries ?next= into the form as a hidden field; without it the
    # POST below would land on LOGIN_REDIRECT_URL instead.
    assert f'value="{url}"' in login.content.decode(), \
        'The login form did not carry the destination as a hidden field'

    response = anonymous_client.post(
        login_url,
        {'login': outsider.username, 'password': PASSWORD, 'next': url},
    )

    assert response.status_code == 302, \
        f'Login POST returned {response.status_code}, not a redirect'
    assert response.url == url, \
        f'Signing in landed on {response.url!r} instead of {url!r}'


def test_oauth_buttons_carry_the_destination_too(
        anonymous_client, private_project):
    """Most people here sign in with Globus or Google, not with a password."""
    url = _private_urls(private_project)['project']

    login = anonymous_client.get(f'/accounts/login/?next={url}')

    assert login.status_code == 200
    provider_links = re.findall(r'href="([^"]*/accounts/(?:globus|google)/login/[^"]*)"',
                                login.content.decode())
    assert provider_links, 'No provider login links on the login page'
    for link in provider_links:
        assert 'next=' in link, \
            f'Provider button drops the destination: {link!r}'


@pytest.mark.parametrize('page', ['project', 'sample', 'feature'])
def test_a_signed_in_non_member_is_refused_rather_than_sent_round_again(
        private_project, outsider, page):
    """Sending them to the login form loops: allauth bounces an authenticated
    visitor straight back to ?next=, which bounces them back to the form."""
    client = Client(HTTP_HOST='localhost')
    client.force_login(outsider)

    response = client.get(_private_urls(private_project)[page])

    assert response.status_code == 404, \
        f'{page} page answered {response.status_code}, expected 404'


def test_an_alias_with_an_ampersand_survives_into_next(
        anonymous_client, mongo_collection, private_project):
    """An alias is user-supplied text and ?next= is a query string, so it has
    to be encoded -- unencoded, everything after the & is a separate parameter
    and the destination is truncated."""
    alias = f'private&link_{uuid.uuid4().hex[:6]}'
    mongo_collection.update_one({'_id': ObjectId(private_project)},
                                {'$set': {'alias_name': alias}})

    response = anonymous_client.get(f'/project/{alias}')

    assert response.status_code == 302
    assert '%26' in response.url, \
        f'The ampersand in the alias was not encoded: {response.url!r}'
