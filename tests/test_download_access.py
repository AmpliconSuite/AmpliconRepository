"""
The download routes answer a visitor exactly as the project's pages do.

Measured locally on 2026-10-14, before this was enforced: a signed-in
non-member downloaded a private project's archive (project_download), its
sample zip (sample_download), its sample metadata (sample_metadata_download)
and its files (feature_download, pdf_download), and png_download served any
GridFS id at all, whatever project the URL named.

The rule is the pages' rule: a private project is for its members, a
hidden_public one for anyone holding the link, a public one for everyone.
The per-feature file routes also serve only an id the named sample holds.
"""

import io
import os
import shutil
import uuid

import pytest
from bson.objectid import ObjectId
from django.http import Http404

from caper.download_gate import grant_download_pass

EXTERNAL_ADDR = '203.0.113.9'      # TEST-NET-3: not a loopback self-call
SAMPLE = 'S1'
FEATURE = 'S1_amplicon1_ecDNA_1'


class _Anon:
    is_authenticated = False
    is_staff = False
    is_superuser = False
    username = ''
    email = ''


class _Member:
    username = 'pytest_access_member'
    email = 'pytest_access_member@example.com'
    is_authenticated = True
    is_staff = False
    is_superuser = False


class _Stranger:
    username = 'pytest_access_stranger'
    email = 'pytest_access_stranger@example.com'
    is_authenticated = True
    is_staff = False
    is_superuser = False


def _put(fs, data, project_id):
    from caper.gridfs_backlinks import put_with_backlink
    return put_with_backlink(fs, io.BytesIO(data), project_id=project_id,
                             sample_name=SAMPLE, feature_key='test')


@pytest.fixture
def make_project(mongo_collection):
    """Insert minimal project documents straight into Mongo; remove them after."""
    from caper.utils import fs_handle, delete_gridfs_file
    from caper.project_version_cleanup import delete_gridfs_payload_for_project
    made = []

    def _make(visibility):
        pid = ObjectId()
        files = {key: _put(fs_handle, f'{key} of {pid}'.encode(), pid)
                 for key in ('Feature_BED_file', 'AA_PNG_file', 'AA_PDF_file', 'CNV_BED_file')}
        files['Sample_metadata_JSON'] = _put(fs_handle, b'{"sample_source": "test"}', pid)
        mongo_collection.insert_one({
            '_id': pid,
            'project_name': f'pytest-access-{uuid.uuid4().hex[:8]}',
            'private': visibility,
            'project_members': [_Member.username],
            'delete': False,
            'current': True,
            'runs': {SAMPLE: [dict(files, Sample_name=SAMPLE, Feature_ID=FEATURE,
                                   AA_amplicon_number=1, Classification='ecDNA',
                                   Location=['chr1:1-2'], Oncogenes=[])]},
        })
        made.append(pid)
        return str(pid), {k: str(v) for k, v in files.items()}

    yield _make
    for pid in made:
        doc = mongo_collection.find_one({'_id': pid}) or {}
        delete_gridfs_payload_for_project(delete_gridfs_file, doc)
        mongo_collection.delete_one({'_id': pid})
        for root in ('tmp', os.path.join('caper', 'tmp')):
            shutil.rmtree(os.path.join(root, str(pid)), ignore_errors=True)


def _request(request_factory, user, path='/project/x/download'):
    req = request_factory.get(path, REMOTE_ADDR=EXTERNAL_ADDR)
    req.user = user
    req.session = {}
    grant_download_pass(req)        # past the captcha: what is left is authorization
    return req


def _call(view, req, *args):
    """Status of the view's answer; a raised Http404 counts as 404.

    The response is closed: a zip response removes its archive from the
    working directory only on close.
    """
    try:
        response = view(req, *args)
    except Http404:
        return 404
    response.close()
    return response.status_code


def _file_routes():
    from caper.views import feature_download, pdf_download, png_download
    return [(feature_download, 'Feature_BED_file'),
            (pdf_download, 'AA_PDF_file'),
            (png_download, 'AA_PNG_file')]


# ---------------------------------------------------------------------------
# Per-feature file routes
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize('visibility,user,expected', [
    ('private', _Stranger(), 404),
    ('private', _Anon(), 302),           # to the login form, as the pages do
    ('private', _Member(), 200),
    ('hidden_public', _Stranger(), 200),
    ('hidden_public', _Anon(), 200),
    ('public', _Anon(), 200),
])
def test_file_routes_follow_the_page_rule(make_project, request_factory,
                                          visibility, user, expected):
    pid, files = make_project(visibility)
    for view, key in _file_routes():
        status = _call(view, _request(request_factory, user), pid, SAMPLE, FEATURE, files[key])
        assert status == expected, f'{view.__name__} on {visibility} for {type(user).__name__}'


@pytest.mark.integration
def test_file_routes_refuse_an_id_from_another_project(make_project, request_factory):
    """The hole as found: a public project's URL carrying a private file's id."""
    public_pid, _ = make_project('public')
    _, private_files = make_project('private')
    for view, key in _file_routes():
        status = _call(view, _request(request_factory, _Anon()),
                       public_pid, SAMPLE, FEATURE, private_files[key])
        assert status == 404, view.__name__


@pytest.mark.integration
def test_file_routes_refuse_an_id_under_the_wrong_kind(make_project, request_factory):
    """A PNG route serves PNG fields only, not the sample's other files."""
    from caper.views import png_download
    pid, files = make_project('public')
    status = _call(png_download, _request(request_factory, _Anon()),
                   pid, SAMPLE, FEATURE, files['CNV_BED_file'])
    assert status == 404


@pytest.mark.integration
@pytest.mark.parametrize('bad_id', ['Not Provided', 'not-an-objectid', str(ObjectId())])
def test_file_routes_404_on_missing_or_unknown_ids(make_project, request_factory, bad_id):
    pid, _ = make_project('public')
    for view, _key in _file_routes():
        assert _call(view, _request(request_factory, _Anon()), pid, SAMPLE, FEATURE, bad_id) == 404


# ---------------------------------------------------------------------------
# Sample and project routes
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize('route', ['sample_download', 'sample_metadata_download'])
@pytest.mark.parametrize('visibility,user,expected', [
    ('private', _Stranger(), 404),
    ('private', _Anon(), 302),
    ('private', _Member(), 200),
    ('hidden_public', _Stranger(), 200),
    ('public', _Anon(), 200),
])
def test_sample_routes_follow_the_page_rule(make_project, request_factory,
                                            route, visibility, user, expected):
    import caper.views as views
    pid, _ = make_project(visibility)
    status = _call(getattr(views, route), _request(request_factory, user), pid, SAMPLE)
    assert status == expected


@pytest.mark.integration
@pytest.mark.parametrize('visibility,user,refused', [
    ('private', _Stranger(), True),
    ('private', _Anon(), True),
    ('private', _Member(), False),
    ('hidden_public', _Stranger(), False),
    ('public', _Anon(), False),
])
def test_project_download_follows_the_page_rule(make_project, request_factory, monkeypatch,
                                                visibility, user, refused):
    """Refusal is checked before anything else runs, so count downloads to see it."""
    import caper.views as views
    counted = []
    monkeypatch.setattr(views, 'update_project_download_count',
                        lambda project, name: counted.append(name))
    pid, _ = make_project(visibility)
    status = _call(views.project_download, _request(request_factory, user), pid)
    if refused:
        assert status in (302, 404) and not counted
    else:
        assert counted == [pid]
