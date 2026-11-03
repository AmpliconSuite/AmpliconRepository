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


# ---------------------------------------------------------------------------
# Batch sample download: only the emailed-link path runs in the background
# ---------------------------------------------------------------------------

@pytest.fixture
def logged_in(client, monkeypatch, flag):
    from django.contrib.auth.models import User

    monkeypatch.setattr('caper.views.get_shutdown_pending', lambda: flag['on'])
    username = 'pytest_shutdown_guard'
    User.objects.filter(username=username).delete()
    user = User.objects.create_user(username=username, email=f'{username}@example.com')
    client.force_login(user)
    try:
        yield client
    finally:
        user.delete()


_NO_SUCH_SAMPLE = '000000000000000000000000:nobody'


def test_an_emailed_batch_is_refused_with_its_reason(flag, logged_in):
    flag['on'] = True
    response = logged_in.post('/batch-sample-download/',
                              data={'samples': [_NO_SUCH_SAMPLE], 'emailResults': 'true'},
                              HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    assert response.status_code == 503
    assert response['Retry-After']
    assert json.loads(response.content) == {'success': False, 'message': SHUTDOWN_MESSAGE}


def test_an_emailed_batch_from_a_form_is_sent_back_with_the_message(flag, logged_in):
    flag['on'] = True
    response = logged_in.post('/batch-sample-download/',
                              data={'samples': [_NO_SUCH_SAMPLE], 'emailResults': 'true'})

    assert response.status_code == 302
    assert [str(m) for m in response.wsgi_request._messages] == [SHUTDOWN_MESSAGE]


def test_a_batch_streamed_in_the_request_is_still_served(flag, logged_in):
    flag['on'] = True
    response = logged_in.post('/batch-sample-download/',
                              data={'samples': [_NO_SUCH_SAMPLE]})

    assert response.status_code == 200
    assert 'zip' in response['Content-Type']
