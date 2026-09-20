"""``<link rel="canonical">`` on every page (caper/canonical.py).

Search Console, 2026-09-20: 241 URLs under "Duplicate without user-selected
canonical", and Google crawling both ``www.ampliconrepository.org`` and the
apex for the same pages.  The site had never said which URL a page is.

Three properties are pinned here.  The host comes from ``SITE_URL`` and never
from the request, since the request host is the thing being corrected.  A
project or sample page reached through a superseded ``linkid`` is filed under
the head of its chain.  And a deployment that is ``noindex`` (the dev gate)
emits no canonical at all, because a canonical beside a noindex is a
contradiction a crawler resolves by guessing.
"""

import re
from types import SimpleNamespace

import pytest
from bson.objectid import ObjectId
from django.test import override_settings

from caper import canonical


SITE = 'https://example.org/'
prod_like = override_settings(SITE_URL=SITE, DEV_GATE_ENABLED=False)

CANONICAL_TAG = re.compile(r'<link rel="canonical" href="([^"]*)">')


def _tags(html):
    return CANONICAL_TAG.findall(html)


def _render_terms(request_factory, path, context=None, host='example.org'):
    """The terms page through the real template stack: base.html, the
    context processors, Mezzanine's menu tags (which want request.user)."""
    from django.contrib.auth.models import AnonymousUser
    from django.template.loader import render_to_string
    request = request_factory.get(path, HTTP_HOST=host)
    request.user = AnonymousUser()
    return render_to_string('pages/terms.html', context, request=request)


# ---------------------------------------------------------------------------
# The default: request path, no query string, host from the setting
# ---------------------------------------------------------------------------

@prod_like
def test_default_canonical_is_the_path_on_site_url_without_the_query(request_factory):
    request = request_factory.get('/gene-search/?gene=MYC&page=2', HTTP_HOST='www.example.org')
    assert canonical.canonical_url_context(request) == {
        'CANONICAL_URL': 'https://example.org/gene-search/'}


@prod_like
def test_the_request_host_is_never_used(request_factory):
    """www. and the apex must produce the same canonical -- that is the point."""
    urls = {canonical.canonical_url_context(
                request_factory.get('/project/abc', HTTP_HOST=host))['CANONICAL_URL']
            for host in ('www.example.org', 'example.org', 'localhost')}
    assert urls == {'https://example.org/project/abc'}


@prod_like
def test_a_post_carries_no_canonical(request_factory):
    assert canonical.canonical_url_context(request_factory.post('/project/abc')) == {}


@override_settings(SITE_URL=SITE, DEV_GATE_ENABLED=True)
def test_a_noindex_deployment_emits_no_canonical(request_factory):
    assert canonical.canonical_url_context(request_factory.get('/')) == {}
    assert canonical.canonical_url('/') is None


@override_settings(SITE_URL='', DEV_GATE_ENABLED=False)
def test_no_site_url_means_no_canonical(request_factory):
    assert canonical.canonical_url_context(request_factory.get('/')) == {}


# ---------------------------------------------------------------------------
# The template: base.html emits the tag, and a view's own value wins
# ---------------------------------------------------------------------------

@prod_like
def test_base_template_emits_one_canonical_link(request_factory):
    html = _render_terms(request_factory, '/terms/?utm_source=x', host='www.example.org')
    assert _tags(html) == ['https://example.org/terms/']


@prod_like
def test_a_view_context_value_overrides_the_default(request_factory):
    html = _render_terms(request_factory, '/terms/', {'CANONICAL_URL': 'https://example.org/elsewhere'})
    assert _tags(html) == ['https://example.org/elsewhere']


@override_settings(SITE_URL=SITE, DEV_GATE_ENABLED=True)
def test_base_template_emits_nothing_when_gated(request_factory):
    assert _tags(_render_terms(request_factory, '/terms/')) == []


# ---------------------------------------------------------------------------
# Project and sample pages: the head of the chain, whichever id was asked for
# ---------------------------------------------------------------------------

class _Chain:
    """A pointered three-version chain and a collection that serves it."""

    def __init__(self):
        chain_id = ObjectId()
        self.ids = [ObjectId() for _ in range(3)]
        self.members = [
            {'_id': oid, 'linkid': oid, 'version_chain_id': chain_id,
             'version_ordinal': i + 1, 'is_latest': i == 2}
            for i, oid in enumerate(self.ids)]

    def find(self, query, projection=None):
        return [m for m in self.members if m['version_chain_id'] == query['version_chain_id']]

    def update_one(self, *a, **k):
        return None

    @property
    def head(self):
        return str(self.ids[-1])


def test_a_superseded_version_is_filed_under_the_head():
    chain = _Chain()
    for member in chain.members:
        assert canonical.canonical_project_id(chain, member) == chain.head


def test_an_unpointered_document_is_its_own_head():
    doc = {'_id': ObjectId()}
    assert canonical.canonical_project_id(SimpleNamespace(find=None), doc) == str(doc['_id'])


@prod_like
def test_project_and_sample_urls_point_at_the_head():
    chain = _Chain()
    old = chain.members[0]
    assert canonical.project_canonical_url(chain, old) == f'https://example.org/project/{chain.head}'
    assert canonical.sample_canonical_url(chain, old, 'S 1') == \
        f'https://example.org/project/{chain.head}/sample/S%201'


@prod_like
@pytest.mark.integration
def test_project_page_context_carries_the_heads_canonical(monkeypatch, request_factory):
    """Through the view, with the same stand-ins test_project_feature_count
    uses: the page rendered for an old linkid names the head, not itself."""
    import pandas as pd
    from caper import views

    chain = _Chain()
    old = chain.members[0]
    project = {
        **old, '_id': str(old['_id']), 'linkid': str(old['_id']),
        'project_name': 'canonical-regression', 'private': 'public',
        'delete': True, 'current': False, 'FINISHED?': True, 'project_members': [],
        'runs': {'sample_1': [{'Sample_name': 'sample-a', 'Oncogenes': [],
                               'Classification': 'ecDNA', 'Feature_ID': 'f1'}]},
    }
    monkeypatch.setattr(views, 'get_one_project', lambda _id: project)
    monkeypatch.setattr(views, 'validate_project', lambda value, _name: value)
    monkeypatch.setattr(views, 'previous_versions', lambda _p: ([], 'Viewing an older version'))
    monkeypatch.setattr(views, 'set_project_edit_OK_flag', lambda *_a: None)
    monkeypatch.setattr(views, 'initialize_ecDNA_context', lambda *_a: None)
    monkeypatch.setattr(views, 'reference_genome_from_project', lambda *_a: 'hg38')
    monkeypatch.setattr(views, 'create_aggregate_df', lambda *_a: (pd.DataFrame(), '/tmp/unused.csv'))
    monkeypatch.setattr(views, 'get_cached_chart', lambda *_a: '')
    monkeypatch.setattr(views, 'session_visit', lambda *_a: (0, 0))
    monkeypatch.setattr(views, 'collection_handle', chain)
    monkeypatch.setattr(views, 'render', lambda _r, _t, context: context)
    request = request_factory.get(f'/project/{old["_id"]}')
    request._messages = SimpleNamespace(add=lambda *a, **k: None)

    context = views.project_page(request, str(old['_id']))

    assert context['CANONICAL_URL'] == f'https://example.org/project/{chain.head}'
    assert context['proj_id'] == str(old['_id']), 'the page itself still renders the old version'


# ---------------------------------------------------------------------------
# End to end: a real project, reaggregated once, both pages fully rendered
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.integration
@prod_like
def test_rendered_pages_of_a_real_chain_carry_the_heads_canonical(
        request_factory, test_user, mongo_collection):
    """Create a project, reaggregate it into a second version, then render the
    project page and the sample page through the real templates under both
    ids.  Every one of the four pages must carry exactly one canonical, and it
    must name the head."""
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from conftest import (_build_create_request, _build_edit_request, _cleanup_project,
                          _poll_until_finished, _project_id_from_redirect,
                          DATASET_SMALL_TAR, DATASET_SMALL_XLSX)
    from caper import views

    def _get(path):
        request = request_factory.get(path, HTTP_HOST='localhost')  # in ALLOWED_HOSTS
        request.user = test_user
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    created = []
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'CanonicalChain',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            old_id = _project_id_from_redirect(views.create_project(request))
        finally:
            for h in handles:
                h.close()
        created.append(old_id)
        doc = _poll_until_finished(mongo_collection, old_id)
        assert doc and not doc.get('aggregation_failed'), doc and doc.get('error_message')
        sample = next(f[0]['Sample_name'] for f in doc['runs'].values() if f)

        request, handles = _build_edit_request(request_factory, test_user, old_id,
                                               project_name='CanonicalChain')
        try:
            head_id = _project_id_from_redirect(views.edit_project_page(request, project_name=old_id))
        finally:
            for h in handles:
                h.close()
        assert head_id and head_id != old_id, 'the reaggregation did not mint a new version'
        created.append(head_id)
        doc = _poll_until_finished(mongo_collection, head_id)
        assert doc and not doc.get('aggregation_failed'), doc and doc.get('error_message')

        expected = {
            'project': f'https://example.org/project/{head_id}',
            'sample': f'https://example.org/project/{head_id}/sample/{sample}',
        }
        for pid in (old_id, head_id):
            page = views.project_page(_get(f'/project/{pid}'), project_name=pid)
            if page.status_code in (301, 302):
                # the resolver may bounce a superseded id to the head itself
                assert page['Location'].endswith(f'/project/{head_id}')
                continue
            assert page.status_code == 200, (pid, page.status_code)
            assert _tags(page.content.decode()) == [expected['project']], pid

            page = views.sample_page(_get(f'/project/{pid}/sample/{sample}'),
                                     project_name=pid, sample_name=sample)
            assert page.status_code == 200, (pid, page.status_code)
            assert _tags(page.content.decode()) == [expected['sample']], pid
    finally:
        for pid in created:
            _cleanup_project(mongo_collection, pid)
