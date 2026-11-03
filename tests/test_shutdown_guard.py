"""
Shutdown mode refuses new uploads on the server, not only in the page's JavaScript.

Before caper/shutdown_guard.py the flag reached the templates alone: on dev,
2026-11-02, with it on, a /create-project/ POST and three run_uploader.py calls
to /add_samples_to_project_api/ were all accepted and aggregated.
"""

import json

import pytest
from django.test import Client

from caper.shutdown_guard import SHUTDOWN_MESSAGE


@pytest.fixture
def flag(monkeypatch):
    """Set the flag the guard reads, and count how often it is read."""
    state = {'on': False, 'reads': 0}

    def _read():
        state['reads'] += 1
        return state['on']

    monkeypatch.setattr('caper.shutdown_guard.get_shutdown_pending', _read)
    return state


@pytest.fixture
def client():
    return Client(SERVER_NAME='localhost')


@pytest.mark.parametrize('path', ['/add_samples_to_project_api/', '/upload_api/'])
def test_pipeline_uploads_are_refused_with_a_retry(flag, client, path):
    flag['on'] = True
    response = client.post(path, data={'project_uuid': 'x', 'project_key': 'y',
                                       'username': 'z'})

    assert response.status_code == 503
    assert response['Retry-After']
    assert json.loads(response.content) == {'error': SHUTDOWN_MESSAGE}


def test_pipeline_uploads_reach_the_view_when_the_flag_is_off(flag, client):
    response = client.post('/add_samples_to_project_api/',
                           data={'project_uuid': '000000000000000000000000',
                                 'project_key': 'y', 'username': 'z'})

    assert flag['reads'] == 1
    assert response.status_code == 404, 'The view did not answer'


@pytest.mark.parametrize('path, back_to', [
    ('/create-project/', '/create-project/'),
    ('/project/000000000000000000000000/edit', '/project/000000000000000000000000/edit'),
    ('/create-empty-project/', '/accounts/profile/'),
])
def test_site_uploads_are_sent_back_with_the_banner_message(flag, client, path, back_to):
    flag['on'] = True
    response = client.post(path, data={'project_name': 'x'})

    assert response.status_code == 302
    assert response['Location'] == back_to
    stored = [str(m) for m in response.wsgi_request._messages]
    assert stored == [SHUTDOWN_MESSAGE]


def test_other_requests_never_read_the_flag(flag, client):
    flag['on'] = True
    client.get('/create-project/')
    client.get('/add_samples_to_project_api/')
    client.post('/search_results/', data={})

    assert flag['reads'] == 0
