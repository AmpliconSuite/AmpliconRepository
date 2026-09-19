"""The two download routes no test called, measured 2026-09-19.

Every other download view was exercised by at least one test; ``feature_download``
(the per-feature BED) and ``download_coamp_edges`` (the co-amplification graph's
edge CSV) were not.  Neither needs the sample tarballs: the BED route reads one
GridFS file, and the edge route reads project documents, so both are seeded
directly and cleaned up afterwards.

``download_coamp_edges`` has three exits before it produces anything, and each
is a redirect carrying a message rather than an error page, so the session
shape it reads (``selected_projects``, ``graph_available``) is set up here the
way the visualizer leaves it.
"""

import csv
import io

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404

from conftest import _download_bytes


# ---------------------------------------------------------------------------
# feature_download: one GridFS file, streamed back as a BED attachment
# ---------------------------------------------------------------------------

BED = b"chr8\t127700000\t128800000\tecDNA_1\n"


@pytest.fixture
def bed_in_gridfs():
    from caper.views import fs_handle
    file_id = fs_handle.put(BED, filename='pytest_feature.bed')
    try:
        yield str(file_id)
    finally:
        fs_handle.delete(file_id)


@pytest.mark.integration
def test_feature_download_returns_the_bed_as_an_attachment(
        request_factory, test_user, bed_in_gridfs):
    from caper.views import feature_download

    req = request_factory.get(
        f'/project/p/sample/s/feature/ecDNA_1/download/{bed_in_gridfs}')
    req.user = test_user
    resp = feature_download(req, project_name='p', sample_name='s',
                            feature_name='ecDNA_1', feature_id=bed_in_gridfs)

    assert resp.status_code == 200
    assert _download_bytes(resp) == BED, 'the bytes served are not the file stored'
    assert resp['Content-Disposition'] == 'attachment; filename="ecDNA_1.bed"'
    assert resp['Content-Type'] == 'application/caper.bed+csv'


@pytest.mark.parametrize('sentinel', ['Not Provided', 'not provided', ''])
def test_feature_download_without_a_file_is_a_404_not_a_crash(
        request_factory, test_user, sentinel):
    """The aggregator writes a sentinel where a feature has no BED (#515 had
    the PDF route crash on one).  Same vocabulary, same route shape, same
    expectation: a 404 naming the feature, not ObjectId() raising."""
    from caper.views import feature_download, _MISSING_FILE_SENTINELS

    assert sentinel in _MISSING_FILE_SENTINELS, 'the sentinel vocabulary moved; update this test'
    req = request_factory.get('/project/p/sample/s/feature/f/download/x')
    req.user = test_user
    with pytest.raises(Http404):
        feature_download(req, project_name='p', sample_name='s',
                         feature_name='f', feature_id=sentinel)


# ---------------------------------------------------------------------------
# download_coamp_edges: the visualizer's session, then a CSV of edges
# ---------------------------------------------------------------------------

def _feature(sample_name, feature_id, genes):
    return {
        'Sample_name': sample_name,
        'Feature_ID': feature_id,
        'Classification': 'ecDNA',
        'Reference_version': 'GRCh38',
        'Location': "['chr8:127700000-128800000']",
        'Oncogenes': genes,
        'All_genes': genes,
        'AA_amplicon_number': 1,
    }


@pytest.fixture
def coamp_project(mongo_collection, test_user):
    """Two samples whose ecDNA features share MYC and PVT1: one edge at least."""
    document = {
        'project_name': 'pytest coamp edges',
        'creator': test_user.username,
        'project_members': [test_user.username],
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': {
            'S1': [_feature('S1', 'S1_amplicon1', ['MYC', 'PVT1'])],
            'S2': [_feature('S2', 'S2_amplicon1', ['MYC', 'PVT1', 'CASC8'])],
        },
        'sample_count': 2,
    }
    inserted = mongo_collection.insert_one(document).inserted_id
    mongo_collection.update_one({'_id': inserted}, {'$set': {'linkid': str(inserted)}})
    try:
        yield str(inserted)
    finally:
        mongo_collection.delete_one({'_id': inserted})


@pytest.fixture(autouse=True)
def edges_dir(tmp_path, monkeypatch):
    """Every test writes its edge CSVs under its own directory."""
    monkeypatch.setenv('COAMP_EDGES_DIR', str(tmp_path / 'coamp_edges'))
    return tmp_path / 'coamp_edges'


def _visualizer_request(request_factory, test_user, session, query=''):
    req = request_factory.get('/coamplification-graph/download-edges/' + query)
    req.user = test_user
    req.session = session
    # messages.error() needs a storage; the middleware is not in the loop here.
    req._messages = FallbackStorage(req)
    return req


def _messages(req):
    return [str(m) for m in req._messages]


def test_edge_download_without_a_selection_redirects_with_a_message(
        request_factory, test_user):
    from caper.views import download_coamp_edges

    req = _visualizer_request(request_factory, test_user, session={})
    resp = download_coamp_edges(req)
    assert resp.status_code == 302
    assert resp['Location'] == '/coamplification-graph/'
    assert any('No projects selected' in m for m in _messages(req))


def test_edge_download_before_the_graph_is_built_redirects_with_a_message(
        request_factory, test_user):
    from caper.views import download_coamp_edges

    req = _visualizer_request(request_factory, test_user,
                              session={'selected_projects': ['whatever'],
                                       'graph_available': False})
    resp = download_coamp_edges(req)
    assert resp.status_code == 302
    assert any('generate the graph' in m for m in _messages(req))


@pytest.mark.integration
def test_edge_download_streams_a_csv_of_the_graphs_edges(
        request_factory, test_user, coamp_project):
    from caper.views import download_coamp_edges

    req = _visualizer_request(request_factory, test_user,
                              session={'selected_projects': [coamp_project],
                                       'graph_available': True})
    resp = download_coamp_edges(req)

    assert resp.status_code == 200, _messages(req)
    assert resp['Content-Type'].startswith('text/csv')
    assert 'attachment' in resp['Content-Disposition']

    rows = list(csv.DictReader(io.StringIO(_download_bytes(resp).decode())))
    assert rows, 'the CSV carries no edges for a project whose samples share genes'
    genes_on_edges = {(r['gene1'], r['gene2']) for r in rows} | {(r['gene2'], r['gene1']) for r in rows}
    assert ('MYC', 'PVT1') in genes_on_edges, sorted(genes_on_edges)
    # Sample ids are left out unless asked for, to keep the file small.
    assert 'gene1_sample_ids' not in rows[0], list(rows[0].keys())


@pytest.mark.integration
def test_edge_download_can_include_sample_ids(request_factory, test_user, coamp_project):
    from caper.views import download_coamp_edges

    req = _visualizer_request(request_factory, test_user,
                              session={'selected_projects': [coamp_project],
                                       'graph_available': True},
                              query='?include_samples=true')
    resp = download_coamp_edges(req)
    assert resp.status_code == 200, _messages(req)
    rows = list(csv.DictReader(io.StringIO(_download_bytes(resp).decode())))
    assert rows
    assert {'gene1_sample_ids', 'gene2_sample_ids'} <= set(rows[0]), list(rows[0].keys())


# ---------------------------------------------------------------------------
# The CSV is written when the graph is built, and the download streams it
# ---------------------------------------------------------------------------

def _download_rows(resp):
    return list(csv.DictReader(io.StringIO(_download_bytes(resp).decode())))


@pytest.mark.integration
def test_edge_download_serves_the_saved_csv_without_rebuilding(
        request_factory, test_user, coamp_project, edges_dir, monkeypatch):
    """Build once (the fallback does it), then the next download must not
    touch a project document at all."""
    from caper import views
    from caper.coamp_edges import edges_path
    from caper.neo4j_utils import generate_cache_key

    session = {'selected_projects': [coamp_project], 'graph_available': True}
    first = views.download_coamp_edges(_visualizer_request(request_factory, test_user, session))
    assert first.status_code == 200
    first_rows = _download_rows(first)
    key = generate_cache_key([coamp_project])
    assert edges_path(key, False).startswith(str(edges_dir))
    assert {p.name for p in edges_dir.iterdir()} == {f'{key}.edges.csv.gz', f'{key}.with_samples.csv.gz'}

    def no_rebuild(*a, **k):
        raise AssertionError('the download rebuilt the graph with a CSV on disk')
    monkeypatch.setattr(views, 'concat_projects', no_rebuild)

    again = views.download_coamp_edges(_visualizer_request(request_factory, test_user, session))
    assert again.status_code == 200
    assert _download_rows(again) == first_rows
    with_ids = views.download_coamp_edges(
        _visualizer_request(request_factory, test_user, session, query='?include_samples=true'))
    rows = _download_rows(with_ids)
    assert {'gene1_sample_ids', 'gene2_sample_ids', 'shared_sample_ids'} <= set(rows[0])
    assert [{k: v for k, v in r.items() if not k.endswith('_sample_ids')} for r in rows] == first_rows, \
        'the two variants disagree on the edges'


@pytest.mark.integration
def test_clearing_the_graph_cache_removes_its_csv(coamp_project, edges_dir, monkeypatch):
    """The files are keyed like the neo4j graph and go when it goes, so a
    rebuilt project can never serve a CSV from its previous data."""
    from caper.coamp_edges import save_edges, open_edges
    from caper.coamp_graph import Graph
    from caper.neo4j_utils import _clear_cache_keys
    from caper.views import concat_projects

    key = 'pytest-clear-key'
    projects_df, _ = concat_projects([coamp_project])
    assert save_edges(key, Graph(projects_df))
    assert open_edges(key, False) is not None

    class _Session:
        def run(self, *a, **k):
            return None
    _clear_cache_keys(_Session(), [key])
    assert open_edges(key, False) is None and open_edges(key, True) is None
    _clear_cache_keys(_Session(), [key])  # absent is fine


def test_a_graph_with_no_edges_saves_nothing(edges_dir):
    from caper.coamp_edges import save_edges, open_edges

    class _Empty:
        def get_edges_dataframe(self, include_sample_ids=False):
            import pandas as pd
            return pd.DataFrame()
    assert save_edges('empty', _Empty()) is None
    assert open_edges('empty', False) is None
    assert not edges_dir.exists() or not any(edges_dir.iterdir())
