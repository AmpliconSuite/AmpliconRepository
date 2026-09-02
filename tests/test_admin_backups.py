"""The two backups an administrator can take off AWS.

These call the real view functions, so the assertions cover the wiring as well
as the builders. A test that called ``admin_backups.build_sqlite()`` directly
would prove the builder works and say nothing about whether the page reaches
it, which is a failure this repository has already shipped once.
"""

import os
import tarfile

import pytest

from caper import admin_backups, views


def _get(request_factory, user, path):
    # HTTP_HOST because the rejection path builds an absolute redirect URI,
    # which goes through ALLOWED_HOSTS; RequestFactory's 'testserver' is not
    # in it.
    request = request_factory.get(path, HTTP_HOST='localhost')
    request.user = user
    return request


@pytest.fixture
def no_downloads_recorded():
    admin_backups._records(primary=True).delete_many({})
    yield
    admin_backups._records(primary=True).delete_many({})


def test_the_page_and_the_downloads_are_staff_only(request_factory, non_member_user):
    for view, path in ((views.admin_backups, '/admin-backups/'),
                       (views.admin_backup_download,
                        '/admin-backups/download/sqlite/')):
        request = _get(request_factory, non_member_user, path)
        response = (view(request) if view is views.admin_backups
                    else view(request, 'sqlite'))
        assert response.status_code in (301, 302)
        assert '/notfound/' in response['Location']


def test_the_page_says_never_before_the_first_download(
        request_factory, admin_user, no_downloads_recorded):
    request = _get(request_factory, admin_user, '/admin-backups/')
    response = views.admin_backups(request)
    assert response.status_code == 200
    body = response.render().content.decode() if hasattr(response, 'render') \
        else response.content.decode()
    assert 'never' in body
    for kind in admin_backups.KINDS:
        assert '/admin-backups/download/%s/' % kind in body


def test_downloading_the_accounts_database_records_it(
        request_factory, admin_user, no_downloads_recorded):
    request = _get(request_factory, admin_user, '/admin-backups/download/sqlite/')
    response = views.admin_backup_download(request, 'sqlite')
    assert response.status_code == 200

    body = b''.join(response.streaming_content)
    response.close()
    assert body[:2] == b'\x1f\x8b', 'not gzip'
    assert len(body) == int(response['Content-Length'])

    recorded = admin_backups.last_download('sqlite')
    assert recorded is not None
    assert recorded['username'] == admin_user.username
    assert recorded['sha256'] == response['X-Content-SHA256']
    assert recorded['totals'], 'no totals recorded, so nothing to compare against later'


def test_the_metadata_download_verifies_against_its_own_manifest(
        request_factory, admin_user, tmp_path, no_downloads_recorded):
    request = _get(request_factory, admin_user, '/admin-backups/download/metadata/')
    response = views.admin_backup_download(request, 'metadata')
    assert response.status_code == 200

    archive = tmp_path / 'dump.tar.gz'
    archive.write_bytes(b''.join(response.streaming_content))
    response.close()

    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path)
    root = next(p for p in tmp_path.iterdir()
                if p.is_dir() and p.name.startswith('metadata-'))

    # The reason the manifest ships inside the archive: what the browser
    # received can be checked with the same command that checks the
    # six-monthly dump, without trusting this test's own reimplementation.
    import dump_metadata
    assert dump_metadata.verify(str(root)) == []
    assert 'fs.chunks.jsonl.gz' not in {p.name for p in root.iterdir()}, \
        'the payload must never be in a metadata dump'


def test_a_download_leaves_no_scratch_directory_behind(
        request_factory, admin_user, no_downloads_recorded):
    """The scratch directory has no other collector -- only this code knows it."""
    scratch_root = os.path.dirname(admin_backups.workspace())
    before = {n for n in os.listdir(scratch_root) if n.startswith('admin-backup-')}

    request = _get(request_factory, admin_user, '/admin-backups/download/sqlite/')
    response = views.admin_backup_download(request, 'sqlite')
    b''.join(response.streaming_content)
    response.close()

    after = {n for n in os.listdir(scratch_root) if n.startswith('admin-backup-')}
    assert after == before, 'scratch left behind: %s' % (after - before)


def test_the_recorded_totals_drive_the_changed_since_line(
        request_factory, admin_user, no_downloads_recorded):
    """Recording totals is what makes the next page load able to say anything."""
    request = _get(request_factory, admin_user, '/admin-backups/download/sqlite/')
    response = views.admin_backup_download(request, 'sqlite')
    b''.join(response.streaming_content)
    response.close()

    recorded = admin_backups.last_download('sqlite')
    unchanged = admin_backups.compare(recorded['totals'], recorded['totals'])
    assert unchanged['added'] == 0 and unchanged['removed'] == 0

    moved = dict(recorded['totals'])
    key = sorted(moved)[0]
    moved[key] = moved[key] + 3
    assert admin_backups.compare(recorded['totals'], moved)['added'] == 3


def test_compare_reports_both_directions():
    delta = admin_backups.compare({'a': 5, 'b': 2}, {'a': 7, 'b': 0, 'c': 1})
    assert delta['added'] == 3
    assert delta['removed'] == 2
    assert dict(delta['changed']) == {'a': 2, 'b': -2, 'c': 1}


def test_compare_says_nothing_when_there_is_no_previous_download():
    assert admin_backups.compare(None, {'a': 1}) is None
