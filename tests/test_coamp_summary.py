"""The co-amplification pages' per-project facts come from the index manifest.

Before 2026-09-19 the landing page read every visible project in full to learn
two facts per project -- its reference genome and whether any sample carries
an ecDNA -- and the visualizer's cache-hit path read every selected project in
full twice more for the same facts plus two counts.  Measured on prod that
day: 71 MiB and 3.4 s per landing view for a list that renders 0.5 MiB.

The facts are now computed when a project is indexed and stored on its
manifest.  These tests pin two things: the stored summary says what the view
code it replaces would have said (parity), and the pages no longer read
``runs`` for a project whose manifest carries it (the point).
"""

import pytest

from django.test import RequestFactory


def _feature(sample, feature_id, classification='ecDNA', ref='GRCh38', with_ref=True):
    row = {'Sample_name': sample, 'Feature_ID': feature_id,
           'Classification': classification, 'All_genes': ['MYC'], 'Oncogenes': ['MYC'],
           'Location': "['chr8:1-2']"}
    if with_ref:
        row['Reference_version'] = ref
    return row


# Each case is (runs, expected reference, expected ecDNA-sample count).
CASES = {
    'plain': ({'s1': [_feature('s1', 'f1')], 's2': [_feature('s2', 'f2', 'BFB')]},
              'GRCh38', 1),
    'two ecDNA features in one sample count once': (
        {'s1': [_feature('s1', 'f1'), _feature('s1', 'f2')]}, 'GRCh38', 1),
    'mixed references': ({'s1': [_feature('s1', 'f1')], 's2': [_feature('s2', 'f2', ref='hg19')]},
                         'Multiple', 2),
    'a feature without Reference_version': (
        {'s1': [_feature('s1', 'f1', with_ref=False)]}, 'Unknown', 1),
    'empty run beside a full one': ({'s0': [], 's1': [_feature('s1', 'f1')]}, 'GRCh38', 1),
    'no runs at all': ({}, 'Unknown', 0),
    'lower-case classification still counts': (
        {'s1': [_feature('s1', 'f1', classification='ecdna')]}, 'GRCh38', 1),
    'mm10': ({'s1': [_feature('s1', 'f1', ref='mm10')]}, 'mm10', 1),
}


@pytest.mark.parametrize('case', list(CASES))
def test_summary_matches_the_view_logic_it_replaces(case):
    from caper.feature_index import coamp_summary_for_project
    from caper.views import reference_genome_from_project

    runs, reference, ecdna = CASES[case]
    summary = coamp_summary_for_project({'_id': 'x', 'runs': runs})

    assert summary['reference_genome'] == reference
    assert summary['reference_genome'] == reference_genome_from_project(runs), \
        'the summary and views.reference_genome_from_project disagree'
    assert summary['sample_count'] == len(runs)
    assert summary['ecdna_sample_count'] == ecdna


def test_summary_survives_a_malformed_runs_field():
    from caper.feature_index import coamp_summary_for_project
    assert coamp_summary_for_project({'runs': 'not a dict'}) == {
        'reference_genome': 'Unknown', 'sample_count': 0, 'ecdna_sample_count': 0}
    assert coamp_summary_for_project({}) == coamp_summary_for_project({'runs': None})


# ---------------------------------------------------------------------------
# Against the database: the manifest carries it, and the pages read it
# ---------------------------------------------------------------------------

RUNS = {
    's1': [_feature('s1', 'f1'), _feature('s1', 'f2', 'BFB')],
    's2': [_feature('s2', 'f3', 'Linear')],
    's3': [_feature('s3', 'f4')],
}


@pytest.fixture
def indexed_project(mongo_collection, test_user):
    from caper.feature_index import index_project, unindex_project
    document = {
        'project_name': 'pytest coamp summary',
        'creator': test_user.username,
        'project_members': [test_user.username],
        'private': 'private',
        'delete': False, 'current': True, 'FINISHED?': True,
        'runs': RUNS, 'sample_count': len(RUNS),
    }
    inserted = mongo_collection.insert_one(document).inserted_id
    mongo_collection.update_one({'_id': inserted}, {'$set': {'linkid': str(inserted)}})
    document['_id'] = inserted
    index_project(document)
    try:
        yield inserted
    finally:
        unindex_project(inserted)
        mongo_collection.delete_one({'_id': inserted})


@pytest.mark.integration
def test_index_project_writes_the_summary_to_the_manifest(indexed_project):
    from caper.feature_index import manifest_handle, coamp_summaries
    manifest = manifest_handle.find_one({'project_id': indexed_project})
    assert manifest['coamp'] == {
        'reference_genome': 'GRCh38', 'sample_count': 3, 'ecdna_sample_count': 2}
    assert coamp_summaries([indexed_project]) == {indexed_project: manifest['coamp']}


class _NoRuns:
    """A ``collection_handle`` that fails any find that would read ``runs`` for
    the guarded project.  Other projects in the shared test database may have
    manifests from before the summary existed; their fallback read is allowed,
    since it is the documented behaviour, but it must not include this one."""

    def __init__(self, real, project_id):
        self._real = real
        self._project_id = project_id

    def find(self, query, projection=None, *args, **kwargs):
        if projection is None:
            reads_runs = True
        elif any(v == 0 for v in projection.values()):
            reads_runs = projection.get('runs') != 0          # exclusion list
        else:
            reads_runs = projection.get('runs') == 1          # inclusion list
        if reads_runs:
            wanted = query.get('_id')
            ids = wanted.get('$in', []) if isinstance(wanted, dict) else [wanted]
            assert self._project_id not in ids and wanted is not None, \
                f'a co-amplification page read runs for the summarised project: find({query!r}, {projection!r})'
        return self._real.find(query, projection, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def no_runs_reads(monkeypatch, indexed_project):
    import caper.views as views
    import caper.utils as utils
    guard = _NoRuns(utils.collection_handle, indexed_project)
    monkeypatch.setattr(utils, 'collection_handle', guard)
    monkeypatch.setattr(views, 'collection_handle', guard)
    return guard


@pytest.mark.integration
def test_landing_page_lists_the_project_without_reading_runs(
        indexed_project, test_user, no_runs_reads, monkeypatch):
    from caper import views

    captured = {}
    monkeypatch.setattr(views, 'render',
                        lambda request, template, context: captured.update(context) or 'rendered')
    request = RequestFactory().get('/coamplification-graph/')
    request.user = test_user
    assert views.coamplification_graph(request) == 'rendered'

    listed = {p['_id']: p for p in captured['all_projects']}
    assert indexed_project in listed, 'the indexed project fell off the list'
    project = listed[indexed_project]
    assert project['reference_genome'] == 'GRCh38'
    assert project['reference_class'] == 'hg38'
    assert 'runs' not in project and 'sample_data' not in project


@pytest.mark.integration
def test_visualizer_metadata_comes_from_the_manifest(indexed_project, no_runs_reads):
    from caper.views import get_projects_metadata, get_reference_genomes
    pid = str(indexed_project)
    assert get_projects_metadata([pid]) == {pid: [3, 2]}
    assert get_reference_genomes([pid]) == ['GRCh38']


@pytest.mark.integration
def test_a_project_without_the_summary_is_read_from_runs(
        indexed_project, mongo_collection, monkeypatch):
    """The fallback: a manifest from before the summary existed.  The project
    stays on the page, at the old cost, and only that project pays it."""
    from caper.feature_index import manifest_handle
    from caper import views
    manifest_handle.update_one({'project_id': indexed_project}, {'$unset': {'coamp': ''}})

    projections = []
    real_find = views.collection_handle.find

    class _Spy:
        def find(self, query, projection=None, *a, **k):
            projections.append(projection)
            return real_find(query, projection, *a, **k)

        def __getattr__(self, name):
            return getattr(views.collection_handle, name)

    monkeypatch.setattr(views, 'collection_handle', _Spy())
    summaries = views._coamp_summaries_for([{'_id': indexed_project}])
    assert summaries[indexed_project] == {
        'reference_genome': 'GRCh38', 'sample_count': 3, 'ecdna_sample_count': 2}
    assert projections == [{'runs': 1}], 'the fallback should read runs and nothing else'
