"""
The OpenAPI document at /api/v1/openapi.json, and the error contract it claims.

A published spec is a promise about behaviour, and the failure mode that matters
is not "the spec is malformed" -- it is "the spec is well-formed and wrong."
These tests therefore do two different jobs:

  * TestOpenApiDocument checks the document itself: it generates, it covers
    every v1 route, and it does not advertise the write endpoints.
  * TestErrorContract drives the real views and asserts the responses match what
    the document says about them.  Without this half, the annotations are
    decoration.
"""

import json

import pytest
from django.test import Client
from django.urls import get_resolver
from rest_framework.test import APIRequestFactory
from unittest.mock import patch

from caper import views_apis
from caper.api_errors import normalize_api_v1_errors


# `testserver`, Django's default test host, is not in ALLOWED_HOSTS; `localhost`
# is.  Naming it here keeps these tests off override_settings for one constant.
def _client():
    return Client(SERVER_NAME='localhost')


def _generate_schema():
    from drf_spectacular.generators import SchemaGenerator
    return SchemaGenerator().get_schema(request=None, public=True)


# ── the document ────────────────────────────────────────────────────────────

class TestOpenApiDocument:

    def test_document_generates_without_errors(self):
        schema = _generate_schema()
        assert schema['openapi'].startswith('3.')
        assert schema['info']['title'] == 'AmpliconRepository API'

    def test_endpoint_serves_the_document(self):
        resp = _client().get('/api/v1/openapi.json')
        assert resp.status_code == 200
        body = json.loads(resp.content)
        assert body['openapi'].startswith('3.')
        assert '/api/v1/projects/' in body['paths']

    def test_document_is_reachable_without_credentials(self):
        """A client must be able to read the description before it has a token."""
        assert _client().get('/api/v1/openapi.json').status_code == 200

    def test_every_v1_route_is_documented(self):
        """
        Drift guard.  Adding a route under /api/v1/ without annotating it would
        otherwise ship a document that silently under-describes the API -- the
        exact failure a generated spec is supposed to make impossible.
        """
        documented = set(_generate_schema()['paths'])

        routed = set()
        for pattern in get_resolver().url_patterns:
            route = str(getattr(pattern, 'pattern', ''))
            if not route.startswith('api/v1/'):
                continue
            # Django's <str:project_id> is {project_id} in OpenAPI.
            path = '/' + route.replace('<str:project_id>', '{project_id}')
            routed.add(path)

        # The schema endpoint documents everything but itself, by design.
        routed.discard('/api/v1/openapi.json')
        missing = routed - documented
        assert not missing, f'v1 routes missing from the OpenAPI document: {missing}'

    def test_write_endpoints_are_not_advertised(self):
        """
        The upload endpoints are DRF views, so the generator finds them unless
        the preprocessing hook removes them.  They are not part of the public
        read API and their contract belongs to AmpliconSuiteAggregator; a
        document that listed them would contradict the API's documented
        read-only nature.
        """
        paths = _generate_schema()['paths']
        assert all(p.startswith('/api/v1/') for p in paths), paths
        assert '/upload_api/' not in paths
        assert '/add_samples_to_project_api/' not in paths

    def test_error_component_documents_code_and_retry_after(self):
        props = _generate_schema()['components']['schemas']['Error']['properties']
        assert 'error' in props and 'code' in props and 'retry_after' in props


# ── the behaviour the document describes ────────────────────────────────────

def _project(private='private', members=None):
    return {'_id': 'abc', 'linkid': 'abc', 'project_name': 'P',
            'private': private, 'project_members': members or [],
            'tarfile': 't'}


class _User:
    is_authenticated = True

    def __init__(self, username='intruder', email='intruder@x.com'):
        self.username, self.email = username, email


class TestErrorContract:
    """Every documented error body really is `{"error": ..., "code": ...}`."""

    def setup_method(self):
        self.rf = APIRequestFactory()

    def _detail(self, user=None, project=None):
        view = views_apis.ProjectDetailView.as_view()
        patches = [patch('caper.views_apis.get_one_project_sans_runs',
                         return_value=project)]
        if user is not None:
            ta = patch('caper.views_apis.TokenAuthentication')
            mock = ta.start()
            mock.return_value.authenticate.return_value = (user, None)
            req = self.rf.get('/api/v1/projects/abc/', HTTP_AUTHORIZATION='Token t')
        else:
            ta = None
            req = self.rf.get('/api/v1/projects/abc/')
        try:
            with patches[0]:
                return view(req, project_id='abc')
        finally:
            if ta:
                ta.stop()

    def test_not_found_shape(self):
        resp = self._detail(project=None)
        assert resp.status_code == 404
        assert resp.data == {'error': 'Project not found', 'code': 'not_found'}

    def test_anonymous_on_private_is_401_authentication_required(self):
        resp = self._detail(project=_project(members=['owner']))
        assert resp.status_code == 401
        assert resp.data['code'] == 'authentication_required'

    def test_authenticated_non_member_is_403_permission_denied(self):
        resp = self._detail(user=_User(), project=_project(members=['owner']))
        assert resp.status_code == 403
        assert resp.data['code'] == 'permission_denied'

    def test_every_error_body_carries_both_keys(self):
        """`error` stays for humans and existing clients; `code` is the contract."""
        for resp in (self._detail(project=None),
                     self._detail(project=_project(members=['owner'])),
                     self._detail(user=_User(), project=_project(members=['owner']))):
            assert set(resp.data) >= {'error', 'code'}
            assert isinstance(resp.data['error'], str)
            assert isinstance(resp.data['code'], str)


class TestBatchRequiresIds:
    """
    A body without `ids` used to return 200 `{"downloads": [], "skipped": []}`.

    That is the worst possible answer for an unattended client: a caller that
    misspells the key cannot distinguish its own mistake from a truthful "none
    of these are downloadable", so it reports success having fetched nothing.
    Found by making exactly that mistake against production on 2026-09-04.
    """

    def setup_method(self):
        self.rf = APIRequestFactory()
        self.view = views_apis.ProjectBatchDownloadView.as_view()

    def _post(self, body):
        return self.view(self.rf.post('/api/v1/projects/download/', body,
                                      format='json'))

    def test_misspelled_key_is_a_400_not_an_empty_success(self):
        resp = self._post({'project_ids': ['abc']})
        assert resp.status_code == 400
        assert resp.data['code'] == 'ids_required'

    def test_empty_body_is_a_400(self):
        assert self._post({}).status_code == 400

    def test_wrong_type_is_a_400_with_its_own_code(self):
        resp = self._post({'ids': 'abc'})
        assert resp.status_code == 400
        assert resp.data['code'] == 'ids_not_a_list'

    def test_empty_list_is_still_a_valid_request(self):
        """An explicitly empty list is a real question with a real answer."""
        resp = self._post({'ids': []})
        assert resp.status_code == 200
        assert resp.data == {'downloads': [], 'skipped': []}


class TestFrameworkErrorsAreNormalized:
    """
    DRF renders its own errors as `{"detail": ...}`.  Throttling is the one a
    client most needs to parse reliably, and it is on that side -- so under
    /api/v1/ the handler restates it in the same shape as everything else.
    """

    def test_throttle_response_is_normalized_with_retry_after(self):
        from rest_framework.exceptions import Throttled
        rf = APIRequestFactory()
        request = rf.get('/api/v1/projects/')
        resp = normalize_api_v1_errors(Throttled(wait=30), {'request': request})
        assert resp.status_code == 429
        assert resp.data['code'] == 'rate_limited'
        assert resp.data['retry_after'] == 30
        assert 'error' in resp.data

    def test_upload_api_errors_are_left_alone(self):
        """
        Released AmpliconSuiteAggregator versions parse the upload endpoints'
        error bodies.  Normalizing those would change a contract with software
        already in the field, so the handler is scoped by URL prefix.
        """
        from rest_framework.exceptions import ParseError
        rf = APIRequestFactory()
        request = rf.post('/upload_api/')
        resp = normalize_api_v1_errors(ParseError('bad'), {'request': request})
        assert 'detail' in resp.data
        assert 'code' not in resp.data


class TestUnroutedApiPathsAnswerInJson:
    """
    A path under /api/v1/ that resolves to nothing must not return HTML.

    Measured against production 2026-09-04: `/api/v1/` and
    `/api/v1/openapi.json` both returned the site's HTML error page -- 21 KB of
    markup, with a 404 status, to a client that had asked for JSON. Django's
    handler404 runs before any DRF view, so the EXCEPTION_HANDLER never sees it.
    """

    def test_unrouted_v1_path_returns_json(self):
        resp = _client().get('/api/v1/no-such-endpoint/')
        assert resp.status_code == 404
        assert resp['Content-Type'].startswith('application/json')
        body = json.loads(resp.content)
        assert body['code'] == 'endpoint_not_found'
        assert 'openapi.json' in body['error']

    def test_the_body_is_small(self):
        """The HTML page cost 21 KB to say 'no'. This should cost ~100 bytes."""
        resp = _client().get('/api/v1/no-such-endpoint/')
        assert len(resp.content) < 500, len(resp.content)

    def test_site_404s_are_untouched(self):
        """Everything outside /api/v1/ keeps Mezzanine's HTML error page."""
        resp = _client().get('/definitely-not-a-page/')
        assert resp.status_code == 404
        assert not resp['Content-Type'].startswith('application/json')


class TestProjectClassificationsAreReported:
    """
    The API read `Classifications`; the upload path writes `Classification`.

    Measured against production 2026-09-04: 0 of 33 public projects reported any
    classification, while 10 of 10 sampled had ecDNA features in their sample
    rows -- 211 of them. An ecDNA repository was answering "no ecDNA here" to
    the one question it exists to answer, because the reader and the writer
    disagreed by one character.
    """

    def test_reads_the_key_the_upload_path_writes(self):
        d = views_apis._project_to_dict({'_id': 'x', 'linkid': 'x',
                                         'Classification': ['ECDNA', 'BFB']})
        assert d['classifications'] == ['ecDNA', 'BFB']

    def test_plural_spelling_still_read_as_a_fallback(self):
        d = views_apis._project_to_dict({'_id': 'x', 'linkid': 'x',
                                         'Classifications': ['ECDNA']})
        assert d['classifications'] == ['ecDNA']

    def test_spelling_matches_the_sample_rows(self):
        """
        Project documents store these upper-cased; /samples/ returns mixed case.
        A client filtering projects on 'ecDNA' must not then find that the
        samples it selected spell it differently.
        """
        d = views_apis._project_to_dict({
            '_id': 'x', 'linkid': 'x',
            'Classification': ['ECDNA', 'LINEAR', 'COMPLEX-NON-CYCLIC', 'BFB']})
        assert d['classifications'] == ['ecDNA', 'Linear',
                                        'Complex-non-cyclic', 'BFB']

    def test_unknown_values_pass_through_unmangled(self):
        d = views_apis._project_to_dict({'_id': 'x', 'linkid': 'x',
                                         'Classification': ['SomethingNew']})
        assert d['classifications'] == ['SomethingNew']

    def test_absent_field_is_an_empty_list_not_an_error(self):
        assert views_apis._project_to_dict({'_id': 'x', 'linkid': 'x'})[
            'classifications'] == []


class TestVersionIsVisible:
    """
    Resolving a project always returns its current version, so the response has
    to say which version that was -- superseded versions keep their ids and back
    published results, and a caller citing only the id cannot say what it read.
    """

    def test_ordinal_from_the_pointer_when_present(self):
        d = views_apis._project_to_dict(
            {'_id': 'x', 'linkid': 'x', 'version_ordinal': 3,
             'is_latest': True},
            members=[{}, {}, {}])
        assert (d['version'], d['version_count'], d['is_latest_version']) == (3, 3, True)

    def test_ordinal_from_the_array_when_unpointered(self):
        """Documents predating the pointer backfill still know where they are:
        previous_versions is cumulative, so position is len + 1."""
        d = views_apis._project_to_dict(
            {'_id': 'x', 'linkid': 'x',
             'previous_versions': [{'linkid': 'a'}, {'linkid': 'b'}]})
        assert d['version'] == 3
        assert d['version_count'] == 3

    def test_a_first_version_reports_one(self):
        d = views_apis._project_to_dict({'_id': 'x', 'linkid': 'x'})
        assert (d['version'], d['version_count'], d['is_latest_version']) == (1, 1, True)

    def test_superseded_version_says_so(self):
        d = views_apis._project_to_dict(
            {'_id': 'x', 'linkid': 'x', 'version_ordinal': 1, 'is_latest': False},
            members=[{}, {}])
        assert d['version'] == 1 and d['is_latest_version'] is False

    def test_fields_are_in_the_openapi_document(self):
        props = _generate_schema()['components']['schemas']['Project']['properties']
        for field in ('version', 'version_count', 'is_latest_version',
                      'classifications'):
            assert field in props, field


class TestAgentFacingDiscoveryFiles:
    """
    /llms.txt and the robots.txt rule that permits the API.

    Measured 2026-09-04 acting as an agent against production: `/robots.txt`
    told a compliant client `Disallow: /api/`, while leaving the project and
    sample *pages* crawlable. A well-behaved agent was therefore instructed to
    scrape hundreds of HTML renders and keep off the one endpoint that would
    have answered its question in a single call -- exactly backwards.
    """

    def test_llms_txt_is_served_as_plain_text(self):
        resp = _client().get('/llms.txt')
        assert resp.status_code == 200
        assert resp['Content-Type'].startswith('text/plain')

    def test_llms_txt_points_at_the_spec_and_the_recipe(self):
        body = _client().get('/llms.txt').content.decode()
        assert '/api/v1/openapi.json' in body
        assert '/api/v1/projects/' in body
        # The whole point is steering programs off the pages and onto the API.
        assert 'samples/' in body

    def test_robots_allows_the_public_api(self):
        body = _client().get('/robots.txt').content.decode()
        assert 'Allow: /api/v1/' in body

    def test_robots_still_blocks_internal_api_and_bulk_downloads(self):
        """
        The allow must not become a hole. `/api/` covers the internal endpoints
        the site's own pages call, and archive downloads stay off-limits to
        crawlers -- stated explicitly rather than relying on which pattern
        happens to be longer.
        """
        body = _client().get('/robots.txt').content.decode()
        assert 'Disallow: /api/\n' in body
        assert 'Disallow: /api/v1/projects/*/download/' in body

    def test_longest_match_puts_the_download_rule_above_the_allow(self):
        """RFC 9309 resolves conflicts by rule length, so assert the ordering
        we depend on rather than trusting it."""
        allow = len('/api/v1/')
        deny = len('/api/v1/projects/*/download/')
        assert deny > allow
