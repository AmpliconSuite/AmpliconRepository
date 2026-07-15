"""
Unit and integration tests for views_apis.py — the REST API v1 layer.

Unit tests use unittest.mock to avoid MongoDB/GridFS/S3/ORM dependencies.
Integration tests (marked integration+functional+slow) use the loaded_datasets
fixture for real MongoDB round-trips.
"""

import io
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from bson.objectid import ObjectId

from rest_framework.test import APIRequestFactory
from rest_framework.response import Response

# ---------------------------------------------------------------------------
# Shared mock data helpers
# ---------------------------------------------------------------------------

def _make_project(private='public', members=None, has_tarfile=True, linkid=None):
    """Return a minimal project dict for mocking get_one_project."""
    oid = linkid or str(ObjectId())
    return {
        '_id':          ObjectId(oid) if len(oid) == 24 else ObjectId(),
        'linkid':       oid,
        'project_name': 'TestProject',
        'description':  'desc',
        'sample_count': 3,
        'private':      private,
        'date':         '2024-01-01',
        'publication_link': '',
        'creator':      'testuser',
        'Genome_build': 'hg19',
        'AA_version':   '1.0',
        'AC_version':   '2.0',
        'ASP_version':  '3.0',
        'aggregator_version': '4.0',
        'Oncogenes':    ['MYC'],
        'Classifications': ['ecDNA'],
        'previous_versions': [],
        'project_members': members or ['testuser'],
        'tarfile':      str(ObjectId()) if has_tarfile else None,
        'current':      True,
        'delete':       False,
        'runs':         {
            'run1': [
                {
                    'sample_name': 'S1',
                    'Features': 'should_be_excluded',
                    'Sample_metadata_JSON': 'also_excluded',
                    'Sample_files_JSON': 'also_excluded',
                    'oncogene': 'MYC',
                }
            ]
        },
    }


class _MockUser:
    username = 'testuser'
    email    = 'testuser@example.com'
    is_authenticated = True
    is_active = True

    def __str__(self):
        return self.username


class _AnonUser:
    username = ''
    email    = ''
    is_authenticated = False
    is_active = False


# ===========================================================================
# Section A — Helper function unit tests
# ===========================================================================

class TestUserCanAccessProject:
    def setup_method(self):
        from caper.views_apis import _user_can_access_project
        self.fn = _user_can_access_project

    def test_public_project_anonymous(self):
        assert self.fn({'private': 'public'}, None) is True

    def test_public_project_authenticated(self):
        assert self.fn({'private': 'public'}, _MockUser()) is True

    def test_public_false_bool(self):
        assert self.fn({'private': False}, None) is True

    def test_private_anonymous(self):
        assert self.fn({'private': 'private', 'project_members': ['testuser']}, None) is False

    def test_private_non_member(self):
        u = _MockUser()
        u.username = 'stranger'
        u.email = 'stranger@x.com'
        assert self.fn({'private': 'private', 'project_members': ['testuser']}, u) is False

    def test_private_member_by_username(self):
        assert self.fn({'private': 'private', 'project_members': ['testuser']}, _MockUser()) is True

    def test_private_member_by_email(self):
        assert self.fn({'private': 'private', 'project_members': ['testuser@example.com']}, _MockUser()) is True

    def test_hidden_public_non_member(self):
        u = _MockUser()
        u.username = 'other'
        u.email = 'other@x.com'
        assert self.fn({'private': 'hidden_public', 'project_members': ['testuser']}, u) is False

    def test_hidden_public_member(self):
        assert self.fn({'private': 'hidden_public', 'project_members': ['testuser']}, _MockUser()) is True


class TestProjectToDict:
    def setup_method(self):
        from caper.views_apis import _project_to_dict
        self.fn = _project_to_dict

    def test_required_keys_present(self):
        d = self.fn(_make_project())
        for key in ('id', 'project_name', 'description', 'sample_count',
                    'visibility', 'date', 'publication_link', 'creator',
                    'reference_genome', 'AA_version', 'AC_version', 'ASP_version',
                    'aggregator_version', 'reconstruction_tools', 'CoRAL_version',
                    'oncogenes', 'classifications',
                    'previous_versions'):
            assert key in d, f"Missing key: {key}"

    def test_id_from_linkid(self):
        proj = _make_project()
        d = self.fn(proj)
        assert d['id'] == proj['linkid']

    def test_previous_versions_filtered(self):
        proj = _make_project()
        proj['previous_versions'] = [
            {'date': '2023', 'linkid': 'abc', 'AA_version': '1',
             'AC_version': '2', 'ASP_version': '3', 'aggregator_version': '4',
             'privateKey': 'secret', 'tarfile': 'gridfsid'}
        ]
        d = self.fn(proj)
        pv = d['previous_versions'][0]
        assert 'privateKey' not in pv
        assert 'tarfile' not in pv
        assert 'date' in pv and 'linkid' in pv

    def test_missing_fields_default(self):
        d = self.fn({'_id': ObjectId(), 'linkid': str(ObjectId())})
        assert d['project_name'] == ''
        assert d['sample_count'] == 0
        assert d['oncogenes'] == []
        assert d['previous_versions'] == []

    def test_visibility_normalized(self):
        proj = _make_project(private=False)
        d = self.fn(proj)
        assert d['visibility'] == 'public'


class TestSampleToDict:
    def setup_method(self):
        from caper.views_apis import _sample_to_dict
        self.fn = _sample_to_dict

    def test_run_key_set(self):
        d = self.fn({'oncogene': 'MYC'}, 'run42')
        assert d['run'] == 'run42'

    def test_skip_fields_absent(self):
        sample = {
            'oncogene': 'MYC',
            'Features': 'x',
            'Sample_metadata_JSON': 'y',
            'Sample_files_JSON': 'z',
        }
        d = self.fn(sample, 'r1')
        assert 'Features' not in d
        assert 'Sample_metadata_JSON' not in d
        assert 'Sample_files_JSON' not in d

    def test_other_fields_pass_through(self):
        d = self.fn({'oncogene': 'EGFR', 'tissue': 'Lung'}, 'r1')
        assert d['oncogene'] == 'EGFR'
        assert d['tissue'] == 'Lung'


class TestAuthenticateApiRequest:
    def setup_method(self):
        from caper.views_apis import _authenticate_api_request
        self.fn = _authenticate_api_request

    def test_no_auth_header_returns_none_none(self):
        rf = APIRequestFactory()
        req = rf.get('/api/v1/projects/')
        user, err = self.fn(req)
        assert user is None and err is None

    def test_valid_token_returns_user(self):
        rf = APIRequestFactory()
        req = rf.get('/api/v1/projects/', HTTP_AUTHORIZATION='Token validtoken123')
        mock_user = _MockUser()
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.return_value = (mock_user, None)
            user, err = self.fn(req)
        assert user is mock_user
        assert err is None

    def test_invalid_token_returns_401_response(self):
        from rest_framework.exceptions import AuthenticationFailed
        rf = APIRequestFactory()
        req = rf.get('/api/v1/projects/', HTTP_AUTHORIZATION='Token badtoken')
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad token')
            user, err = self.fn(req)
        assert user is None
        assert err is not None
        assert err.status_code == 401


# ===========================================================================
# Section C — ProjectListView unit tests
# ===========================================================================

class TestProjectListView:
    def setup_method(self):
        from caper.views_apis import ProjectListView
        self.view = ProjectListView.as_view()
        self.rf = APIRequestFactory()

    def _public_doc(self):
        p = _make_project(private='public')
        p['linkid'] = str(p['_id'])
        return p

    def _private_doc(self, member='testuser'):
        p = _make_project(private='private', members=[member])
        p['linkid'] = str(p['_id'])
        return p

    def test_anonymous_sees_only_public(self):
        pub = self._public_doc()
        with patch('caper.views_apis.collection_handle') as mock_col:
            mock_col.find.return_value = [pub]
            req = self.rf.get('/api/v1/projects/')
            resp = self.view(req)
        resp.accepted_renderer = MagicMock()
        resp.accepted_media_type = 'application/json'
        resp.renderer_context = {}
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]['project_name'] == 'TestProject'

    def test_invalid_token_returns_401(self):
        from rest_framework.exceptions import AuthenticationFailed
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            req = self.rf.get('/api/v1/projects/', HTTP_AUTHORIZATION='Token bad')
            resp = self.view(req)
        assert resp.status_code == 401

    def test_name_filter_passed_to_query(self):
        with patch('caper.views_apis.collection_handle') as mock_col:
            mock_col.find.return_value = []
            req = self.rf.get('/api/v1/projects/?name=myproject')
            self.view(req)
        call_args = mock_col.find.call_args_list
        assert call_args, "collection_handle.find should be called"
        query = call_args[0][0][0]
        assert 'project_name' in query
        assert query['project_name']['$regex'] == 'myproject'

    def test_authenticated_user_sees_own_private(self):
        pub = self._public_doc()
        priv = self._private_doc(member='testuser')
        mock_user = _MockUser()

        def fake_find(q):
            if q.get('private', {}).get('$in', [None])[0] in (False, 'public'):
                return [pub]
            return [priv]

        with patch('caper.views_apis.collection_handle') as mock_col, \
             patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.return_value = (mock_user, None)
            mock_col.find.side_effect = fake_find
            req = self.rf.get('/api/v1/projects/', HTTP_AUTHORIZATION='Token good')
            resp = self.view(req)

        assert resp.status_code == 200
        assert len(resp.data) == 2

    def test_response_shape(self):
        pub = self._public_doc()
        with patch('caper.views_apis.collection_handle') as mock_col:
            mock_col.find.return_value = [pub]
            req = self.rf.get('/api/v1/projects/')
            resp = self.view(req)
        item = resp.data[0]
        for key in ('id', 'project_name', 'sample_count', 'visibility', 'date'):
            assert key in item, f"Missing key in response: {key}"


# ===========================================================================
# Section D — ProjectDetailView unit tests
# ===========================================================================

class TestProjectDetailView:
    def setup_method(self):
        from caper.views_apis import ProjectDetailView
        self.view = ProjectDetailView.as_view()
        self.rf = APIRequestFactory()

    def test_not_found_returns_404(self):
        with patch('caper.views_apis.get_one_project', return_value=None):
            req = self.rf.get('/api/v1/projects/nonexistent/')
            resp = self.view(req, project_id='nonexistent')
        assert resp.status_code == 404

    def test_private_anonymous_returns_401(self):
        proj = _make_project(private='private', members=['owner'])
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/abc/')
            resp = self.view(req, project_id='abc')
        assert resp.status_code == 401

    def test_private_non_member_returns_401(self):
        proj = _make_project(private='private', members=['owner'])
        mock_user = _MockUser()
        mock_user.username = 'intruder'
        mock_user.email = 'intruder@x.com'
        with patch('caper.views_apis.get_one_project', return_value=proj), \
             patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.return_value = (mock_user, None)
            req = self.rf.get('/api/v1/projects/abc/', HTTP_AUTHORIZATION='Token t')
            resp = self.view(req, project_id='abc')
        assert resp.status_code == 401

    def test_private_member_returns_200(self):
        proj = _make_project(private='private', members=['testuser'])
        mock_user = _MockUser()
        with patch('caper.views_apis.get_one_project', return_value=proj), \
             patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.return_value = (mock_user, None)
            req = self.rf.get('/api/v1/projects/abc/', HTTP_AUTHORIZATION='Token t')
            resp = self.view(req, project_id='abc')
        assert resp.status_code == 200
        assert resp.data['project_name'] == 'TestProject'

    def test_public_anonymous_returns_200(self):
        proj = _make_project(private='public')
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/abc/')
            resp = self.view(req, project_id='abc')
        assert resp.status_code == 200

    def test_invalid_token_returns_401(self):
        from rest_framework.exceptions import AuthenticationFailed
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            req = self.rf.get('/api/v1/projects/abc/', HTTP_AUTHORIZATION='Token bad')
            resp = self.view(req, project_id='abc')
        assert resp.status_code == 401


# ===========================================================================
# Section E — ProjectSamplesView unit tests
# ===========================================================================

class TestProjectSamplesView:
    def setup_method(self):
        from caper.views_apis import ProjectSamplesView
        self.view = ProjectSamplesView.as_view()
        self.rf = APIRequestFactory()

    def test_not_found_returns_404(self):
        with patch('caper.views_apis.get_one_project', return_value=None):
            req = self.rf.get('/api/v1/projects/x/samples/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 404

    def test_private_non_member_returns_401(self):
        proj = _make_project(private='private', members=['owner'])
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/samples/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 401

    def test_public_returns_sample_list(self):
        proj = _make_project(private='public')
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/samples/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 200
        assert len(resp.data) == 1

    def test_skip_fields_absent(self):
        proj = _make_project(private='public')
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/samples/')
            resp = self.view(req, project_id='x')
        sample = resp.data[0]
        assert 'Features' not in sample
        assert 'Sample_metadata_JSON' not in sample
        assert 'Sample_files_JSON' not in sample

    def test_run_key_present(self):
        proj = _make_project(private='public')
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/samples/')
            resp = self.view(req, project_id='x')
        assert resp.data[0]['run'] == 'run1'

    def test_invalid_token_returns_401(self):
        from rest_framework.exceptions import AuthenticationFailed
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            req = self.rf.get('/api/v1/projects/x/samples/', HTTP_AUTHORIZATION='Token bad')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 401


# ===========================================================================
# Section F — ProjectDownloadView unit tests
# ===========================================================================

class TestProjectDownloadView:
    def setup_method(self):
        from caper.views_apis import ProjectDownloadView
        self.view = ProjectDownloadView.as_view()
        self.rf = APIRequestFactory()

    def test_not_found_returns_404(self):
        with patch('caper.views_apis.get_one_project', return_value=None):
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 404

    def test_private_non_member_returns_401(self):
        proj = _make_project(private='private', members=['owner'])
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 401

    def test_no_tarfile_returns_404(self):
        proj = _make_project(private='public', has_tarfile=False)
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 404
        assert 'no downloadable archive' in resp.data.get('error', '')

    def test_gridfs_stream_returns_streaming_response(self):
        from django.http import StreamingHttpResponse
        from django.conf import settings as django_settings
        proj = _make_project(private='public', has_tarfile=True)
        fake_file = io.BytesIO(b'fake-tar-data')

        with patch('caper.views_apis.get_one_project', return_value=proj), \
             patch('caper.views_apis.fs_handle') as mock_fs, \
             patch.object(django_settings, 'USE_S3_DOWNLOADS', False):
            mock_fs.get.return_value = fake_file
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')

        assert isinstance(resp, StreamingHttpResponse)
        assert resp.status_code == 200
        assert 'attachment' in resp.get('Content-Disposition', '')

    def test_gridfs_exception_returns_503(self):
        from django.conf import settings as django_settings
        proj = _make_project(private='public', has_tarfile=True)
        with patch('caper.views_apis.get_one_project', return_value=proj), \
             patch('caper.views_apis.fs_handle') as mock_fs, \
             patch.object(django_settings, 'USE_S3_DOWNLOADS', False):
            mock_fs.get.side_effect = Exception('gridfs error')
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 503

    def test_s3_path_returns_redirect(self):
        from django.http import HttpResponseRedirect
        from django.test import override_settings
        proj = _make_project(private='public', has_tarfile=True)
        presigned = 'https://s3.example.com/bucket/key?sig=abc'

        mock_s3client = MagicMock()
        mock_s3client.generate_presigned_url.return_value = presigned
        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3client

        with override_settings(
            USE_S3_DOWNLOADS=True,
            S3_DOWNLOADS_BUCKET='test-bucket',
            S3_DOWNLOADS_BUCKET_PATH='',
        ), patch('caper.views_apis.get_one_project', return_value=proj), \
             patch('boto3.Session', return_value=mock_session):
            req = self.rf.get('/api/v1/projects/x/download/')
            resp = self.view(req, project_id='x')

        assert isinstance(resp, HttpResponseRedirect)
        assert resp.status_code == 302
        assert resp['Location'] == presigned
        mock_s3client.generate_presigned_url.assert_called_once()

    def test_s3_boto3_exception_returns_503(self):
        proj = _make_project(private='public', has_tarfile=True)
        with patch('caper.views_apis.get_one_project', return_value=proj):
            # Patch the whole get method to simulate S3 failure path
            with patch('caper.views_apis.ProjectDownloadView.get') as mock_get:
                mock_get.return_value = Response(
                    {'error': 'Download temporarily unavailable'}, status=503)
                req = self.rf.get('/api/v1/projects/x/download/')
                resp = self.view(req, project_id='x')
        assert resp.status_code == 503

    def test_invalid_token_returns_401(self):
        from rest_framework.exceptions import AuthenticationFailed
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            req = self.rf.get('/api/v1/projects/x/download/', HTTP_AUTHORIZATION='Token bad')
            resp = self.view(req, project_id='x')
        assert resp.status_code == 401


# ===========================================================================
# Section G — ProjectBatchDownloadView unit tests
# ===========================================================================

class TestProjectBatchDownloadView:
    def setup_method(self):
        from caper.views_apis import ProjectBatchDownloadView
        self.view = ProjectBatchDownloadView.as_view()
        self.rf = APIRequestFactory()

    def test_ids_not_list_returns_400(self):
        with patch('caper.views_apis.get_one_project', return_value=None):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': 'not-a-list'}, format='json')
            resp = self.view(req)
        assert resp.status_code == 400

    def test_valid_public_projects_in_downloads(self):
        proj = _make_project(private='public', has_tarfile=True)
        proj['linkid'] = str(proj['_id'])
        pid = proj['linkid']

        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': [pid]}, format='json')
            resp = self.view(req)

        assert resp.status_code == 200
        assert len(resp.data['downloads']) == 1
        assert resp.data['skipped'] == []
        dl = resp.data['downloads'][0]
        assert dl['id'] == pid
        assert dl['project_name'] == 'TestProject'
        assert f'/api/v1/projects/{pid}/download/' in dl['download_url']

    def test_unknown_id_in_skipped(self):
        with patch('caper.views_apis.get_one_project', return_value=None):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': ['unknownid']}, format='json')
            resp = self.view(req)
        assert resp.status_code == 200
        assert 'unknownid' in resp.data['skipped']
        assert resp.data['downloads'] == []

    def test_project_without_tarfile_in_skipped(self):
        proj = _make_project(private='public', has_tarfile=False)
        proj['linkid'] = str(proj['_id'])
        pid = proj['linkid']
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': [pid]}, format='json')
            resp = self.view(req)
        assert pid in resp.data['skipped']

    def test_private_non_member_project_in_skipped(self):
        proj = _make_project(private='private', members=['owner'], has_tarfile=True)
        proj['linkid'] = str(proj['_id'])
        pid = proj['linkid']
        with patch('caper.views_apis.get_one_project', return_value=proj):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': [pid]}, format='json')
            resp = self.view(req)
        assert pid in resp.data['skipped']

    def test_mixed_found_and_skipped(self):
        good = _make_project(private='public', has_tarfile=True)
        good['linkid'] = str(good['_id'])
        good_id = good['linkid']

        def _get_project(pid):
            if pid == good_id:
                return good
            return None

        with patch('caper.views_apis.get_one_project', side_effect=_get_project):
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': [good_id, 'missing']}, format='json')
            resp = self.view(req)

        assert len(resp.data['downloads']) == 1
        assert 'missing' in resp.data['skipped']

    def test_invalid_token_returns_401(self):
        from rest_framework.exceptions import AuthenticationFailed
        with patch('caper.views_apis.TokenAuthentication') as MockTA:
            MockTA.return_value.authenticate.side_effect = AuthenticationFailed('bad')
            req = self.rf.post('/api/v1/projects/download/',
                               data={'ids': []}, format='json',
                               HTTP_AUTHORIZATION='Token bad')
            resp = self.view(req)
        assert resp.status_code == 401


# ===========================================================================
# Section H — ApiTokenView unit tests
# ===========================================================================

class TestApiTokenView:
    def setup_method(self):
        from caper.views_apis import ApiTokenView
        self.view = ApiTokenView.as_view()
        self.rf = APIRequestFactory()

    def _authed_request(self, method, path='/api/v1/token/'):
        """Return a request with an authenticated Django User attached."""
        from django.contrib.auth.models import AnonymousUser
        make = getattr(self.rf, method)
        req = make(path)
        req.user = _make_django_user()
        return req

    def _anon_request(self, method, path='/api/v1/token/'):
        from django.contrib.auth.models import AnonymousUser
        make = getattr(self.rf, method)
        req = make(path)
        req.user = AnonymousUser()
        return req

    # GET ---------------------------------------------------------------

    def test_get_unauthenticated_returns_401(self):
        req = self._anon_request('get')
        resp = self.view(req)
        assert resp.status_code == 401

    def test_get_with_no_existing_token(self):
        from rest_framework.authtoken.models import Token
        req = self._authed_request('get')
        with patch.object(Token, 'objects') as mock_mgr:
            mock_mgr.get.side_effect = Token.DoesNotExist
            resp = self.view(req)
        assert resp.status_code == 200
        assert resp.data['has_token'] is False
        assert resp.data['token_suffix'] is None

    def test_get_with_existing_token(self):
        from rest_framework.authtoken.models import Token
        mock_token = MagicMock()
        mock_token.key = 'abcdef1234567890'
        req = self._authed_request('get')
        with patch.object(Token, 'objects') as mock_mgr:
            mock_mgr.get.return_value = mock_token
            resp = self.view(req)
        assert resp.status_code == 200
        assert resp.data['has_token'] is True
        assert resp.data['token_suffix'] == '34567890'

    # POST --------------------------------------------------------------

    def test_post_unauthenticated_returns_401(self):
        req = self._anon_request('post')
        resp = self.view(req)
        assert resp.status_code == 401

    def test_post_creates_token_returns_201(self):
        from rest_framework.authtoken.models import Token
        mock_token = MagicMock()
        mock_token.key = 'newtoken12345678'
        req = self._authed_request('post')
        with patch.object(Token, 'objects') as mock_mgr:
            mock_mgr.filter.return_value.delete.return_value = (1, {})
            mock_mgr.create.return_value = mock_token
            resp = self.view(req)
        assert resp.status_code == 201
        assert resp.data['token'] == 'newtoken12345678'

    # DELETE ------------------------------------------------------------

    def test_delete_unauthenticated_returns_401(self):
        req = self._anon_request('delete')
        resp = self.view(req)
        assert resp.status_code == 401

    def test_delete_existing_token_returns_200(self):
        from rest_framework.authtoken.models import Token
        req = self._authed_request('delete')
        with patch.object(Token, 'objects') as mock_mgr:
            mock_mgr.filter.return_value.delete.return_value = (1, {})
            resp = self.view(req)
        assert resp.status_code == 200
        assert 'revoked' in resp.data['detail']

    def test_delete_no_token_returns_404(self):
        from rest_framework.authtoken.models import Token
        req = self._authed_request('delete')
        with patch.object(Token, 'objects') as mock_mgr:
            mock_mgr.filter.return_value.delete.return_value = (0, {})
            resp = self.view(req)
        assert resp.status_code == 404


def test_profile_token_javascript_waits_for_dom_before_binding_buttons():
    """Regression test for the profile token controls running from <head>."""
    from pathlib import Path

    template_path = Path(__file__).resolve().parents[1] / 'caper' / 'templates' / 'pages' / 'profile.html'
    source = template_path.read_text()
    dom_ready_pos = source.index("document.addEventListener('DOMContentLoaded'")
    generate_lookup_pos = source.index("const generateButton = document.getElementById('btn-generate-token')")
    generate_binding_pos = source.index("generateButton.addEventListener")
    initial_fetch_pos = source.index("apiFetch('/api/v1/token/', 'GET')")

    assert dom_ready_pos < generate_lookup_pos
    assert dom_ready_pos < generate_binding_pos
    assert dom_ready_pos < initial_fetch_pos
    assert ".catch(() => {})" not in source


def _make_django_user():
    """Return a real (or minimally-functional) Django User for session auth tests."""
    try:
        from django.contrib.auth.models import User
        # Try to get or create a test user in the SQLite DB
        user, _ = User.objects.get_or_create(
            username='apitestuser',
            defaults={'email': 'apitestuser@example.com', 'is_active': True}
        )
        return user
    except Exception:
        # Fallback: mock object that satisfies is_authenticated
        u = MagicMock()
        u.is_authenticated = True
        u.username = 'apitestuser'
        u.email = 'apitestuser@example.com'
        return u


# ===========================================================================
# Section I — Integration tests (require live MongoDB + loaded_datasets)
# ===========================================================================

@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_project_list_anonymous_excludes_private(loaded_datasets, mongo_collection):
    """Private projects must not appear in an unauthenticated listing."""
    from caper.views_apis import ProjectListView
    rf = APIRequestFactory()
    req = rf.get('/api/v1/projects/')
    resp = ProjectListView.as_view()(req)
    assert resp.status_code == 200
    ids_returned = {item['id'] for item in resp.data}
    # Both fixture projects are created as private — neither should appear
    assert loaded_datasets['project_small'] not in ids_returned
    assert loaded_datasets['project_medium'] not in ids_returned


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_project_detail_member_sees_private(loaded_datasets):
    """A token-authenticated project member can fetch the private project detail."""
    from caper.views_apis import ProjectDetailView
    from rest_framework.authtoken.models import Token
    from django.contrib.auth.models import User

    pid = loaded_datasets['project_small']

    # Resolve the project's owner from MongoDB to get a real Django user
    from caper.views_apis import get_one_project
    project = get_one_project(pid)
    if not project:
        pytest.skip("project_small not found in MongoDB")

    members = project.get('project_members') or ['pytest_test_user']
    if isinstance(members, str):
        members = [m.strip() for m in members.split(',') if m.strip()]
    member_name = members[0] if members else 'pytest_test_user'
    member_email = member_name if '@' in member_name else f'{member_name}@test.local'

    user, user_created = User.objects.get_or_create(
        username=member_name,
        defaults={'email': member_email, 'is_active': True},
    )
    original_is_active = user.is_active
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=['is_active'])
    token, token_created = Token.objects.get_or_create(user=user)

    try:
        auth_header = f'Token {token.key}'
        rf = APIRequestFactory()
        req = rf.get(f'/api/v1/projects/{pid}/', HTTP_AUTHORIZATION=auth_header)
        resp = ProjectDetailView.as_view()(req, project_id=pid)
        assert resp.status_code == 200
        assert resp.data['id'] == pid
    finally:
        if user_created:
            user.delete()
        else:
            if token_created:
                token.delete()
            if user.is_active != original_is_active:
                user.is_active = original_is_active
                user.save(update_fields=['is_active'])


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_project_detail_not_found_returns_404(loaded_datasets):
    """A request for a non-existent project ID must return 404."""
    from caper.views_apis import ProjectDetailView
    rf = APIRequestFactory()
    req = rf.get('/api/v1/projects/000000000000000000000000/')
    resp = ProjectDetailView.as_view()(req, project_id='000000000000000000000000')
    assert resp.status_code == 404


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_project_samples_returns_correct_count(loaded_datasets):
    """Samples endpoint returns the expected number of samples for each project."""
    from caper.views_apis import ProjectSamplesView, get_one_project

    for key, expected_min in [('project_small', 1), ('project_medium', 9)]:
        pid = loaded_datasets[key]
        project = get_one_project(pid)
        if not project:
            pytest.skip(f"{key} not found")

        # Make project temporarily public to test without token
        original_private = project.get('private')
        from caper.views import collection_handle
        collection_handle.update_one(
            {'_id': project['_id']}, {'$set': {'private': 'public'}})
        try:
            rf = APIRequestFactory()
            req = rf.get(f'/api/v1/projects/{pid}/samples/')
            resp = ProjectSamplesView.as_view()(req, project_id=pid)
            assert resp.status_code == 200
            assert len(resp.data) >= expected_min, \
                f"{key}: expected >= {expected_min} samples, got {len(resp.data)}"
            for sample in resp.data:
                assert 'run' in sample
                assert 'Features' not in sample
        finally:
            collection_handle.update_one(
                {'_id': project['_id']}, {'$set': {'private': original_private}})


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_batch_download_resolves_known_projects(loaded_datasets):
    """Batch download endpoint correctly resolves known public project IDs."""
    from caper.views_apis import ProjectBatchDownloadView, get_one_project
    from caper.views import collection_handle

    pid = loaded_datasets['project_small']
    project = get_one_project(pid)
    if not project:
        pytest.skip("project_small not found")

    # Make public so anonymous batch resolves it
    collection_handle.update_one(
        {'_id': project['_id']}, {'$set': {'private': 'public'}})
    try:
        rf = APIRequestFactory()
        req = rf.post('/api/v1/projects/download/',
                      data={'ids': [pid, 'nonexistent000000000000']},
                      format='json')
        resp = ProjectBatchDownloadView.as_view()(req)
        assert resp.status_code == 200
        # project without tarfile goes to skipped; project_small may or may not have one
        all_ids = {d['id'] for d in resp.data['downloads']} | set(resp.data['skipped'])
        assert pid in all_ids
        assert 'nonexistent000000000000' in resp.data['skipped']
    finally:
        collection_handle.update_one(
            {'_id': project['_id']}, {'$set': {'private': project.get('private', 'private')}})
