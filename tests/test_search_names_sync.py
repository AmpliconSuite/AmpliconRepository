"""A name the list cannot resolve is a sample the site cannot find.

``search_names`` is the small collection substring name search resolves
against: the feature index holds ~48,000 rows, the name list one document per
distinct name, and a name query scans the small one and hands the exact names
back as an indexed ``$in``.  Two indexed steps instead of one collection scan.

Until this module's fix, that collection had exactly one writer -- the full
``rebuild_feature_index`` command.  ``index_project``, the hook that runs on
every project write, did not touch it, so every name arriving between full
rebuilds was in the index and not in the list.

Measured on prod 2026-09-13: the last full rebuild ran 2026-09-08 18:42, one
project was created 2026-09-11 20:15, and 468 of its 476 sample names were
missing.  A search for ``143B`` returned 1 row where the index held 6 -- a
partial answer rather than an empty one, which is why it went unnoticed for
three days.

``feature_index``'s design calls for two prongs: hooks keep the derived data
current, and a drift check is the standing falsifying measurement that makes
gaps in the hooks survivable.  The name list was in neither.  These tests
cover both.
"""
import pytest
from bson.objectid import ObjectId

from caper import feature_index
from caper.feature_index import (
    feature_index_drift,
    feature_index_handle,
    index_project,
    manifest_handle,
    search_names_handle,
)
from caper.search_index import _resolve_names

pytestmark = pytest.mark.integration


def _project(name, samples):
    """A project document shaped the way the indexable ones are."""
    return {
        '_id': ObjectId(),
        'project_name': name,
        'private': 'public',
        'project_members': ['someone@example.org'],
        'runs': {
            sample: [{
                'Sample_name': sample,
                'Feature_ID': f'{sample}_amplicon1',
                'Classification': 'ecDNA',
                'All_genes': ["'MYC'"],
                'Oncogenes': ["'MYC'"],
                'Location': ["'chr8:127000000-128000000'"],
                'Reference_version': 'GRCh38',
            }] for sample in samples
        },
    }


@pytest.fixture
def indexed_projects():
    """Index throwaway projects, and remove every trace of them afterwards."""
    created = []

    def index(name, samples):
        project = _project(name, samples)
        created.append(project)
        index_project(project)
        return project

    try:
        yield index
    finally:
        for project in created:
            feature_index_handle.delete_many({'project_id': project['_id']})
            manifest_handle.delete_one({'project_id': project['_id']})
            search_names_handle.delete_many({'name': project['project_name']})
            for sample in project['runs']:
                # Only names this test minted; a shared name is left alone.
                if not feature_index_handle.count_documents({'sample_name': sample}):
                    search_names_handle.delete_many({'name': sample})


def test_index_project_records_the_names_it_writes(indexed_projects):
    """The hook, which is the prong that keeps a new project searchable."""
    project = indexed_projects('pytest names A', ['pytest_sample_alpha'])

    stored = {row['name'] for row in search_names_handle.find(
        {'kind': 'sample'}, {'name': 1, '_id': 0})}
    assert 'pytest_sample_alpha' in stored

    projects = {row['name'] for row in search_names_handle.find(
        {'kind': 'project'}, {'name': 1, '_id': 0})}
    assert project['project_name'] in projects


def test_a_newly_indexed_name_is_resolvable_by_substring(indexed_projects):
    """The load-bearing question, not the adjacent one.

    "Do the two collections agree?" is adjacent. "Can a search find the
    sample?" is what broke on prod, so that is what this asserts.
    """
    indexed_projects('pytest names B', ['pytest_sample_bravo'])
    assert _resolve_names('sample', 'bravo') == ['pytest_sample_bravo']


def test_a_name_two_projects_share_is_stored_once(indexed_projects):
    """Names are distinct across the corpus; indexing twice must not duplicate."""
    indexed_projects('pytest names C', ['pytest_sample_shared'])
    indexed_projects('pytest names D', ['pytest_sample_shared'])

    assert search_names_handle.count_documents(
        {'kind': 'sample', 'name': 'pytest_sample_shared'}) == 1


def test_reindexing_keeps_a_name_another_project_still_uses(indexed_projects):
    """Additions only, deliberately -- the two failure modes are not symmetric.

    A missing name makes a search silently under-report.  A name kept after
    its rows are gone resolves to an ``$in`` entry matching nothing, which
    cannot over-report because the access filter is a separate clause beside
    it.  So a reindex adds and never deletes, and removal is left to the full
    rebuild.
    """
    project = indexed_projects('pytest names E', ['pytest_sample_kept'])
    index_project(dict(project, runs={}))

    assert search_names_handle.count_documents(
        {'kind': 'sample', 'name': 'pytest_sample_kept'}) == 1


def test_drift_reports_a_name_the_index_has_and_the_list_does_not(indexed_projects):
    """The second prong: the check has to be able to see the failure.

    Asserted as a delta rather than an absolute -- a developer database
    carries whatever drift it already had, and a test that demanded zero would
    be testing the fixture, not the code.
    """
    indexed_projects('pytest names F', ['pytest_sample_foxtrot'])
    before = feature_index_drift()
    assert ('sample', 'pytest_sample_foxtrot') not in before['names_missing']

    search_names_handle.delete_many({'kind': 'sample', 'name': 'pytest_sample_foxtrot'})

    after = feature_index_drift()
    assert ('sample', 'pytest_sample_foxtrot') in after['names_missing']
    assert len(after['names_missing']) == len(before['names_missing']) + 1


def test_search_names_is_not_left_out_of_the_derived_collections():
    """Whatever wipes the projects must wipe this too, or a search lies."""
    assert feature_index.SEARCH_NAMES_COLLECTION in feature_index.DERIVED_COLLECTIONS
