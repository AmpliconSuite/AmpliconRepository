"""A derived collection nothing measures is the defect this module keeps making.

``feature_index`` derives four collections from the project documents, and
keeps them current two ways: hooks on every project write, and a drift check
that is the standing falsifying measurement for "the derived data is current".
The design holds only if both prongs cover every derived collection.

``search_names`` was in neither until 2026-09-14.  It was not an exotic
failure: it was added, it was written by the full rebuild, nobody wrote down
that the check did not look at it, and prod carried 468 names a search could
not resolve for three days while ``--check`` reported "index is current".  The
hook was an ordinary bug.  The check not covering the collection is what made
the bug silent, and silence is the part worth engineering against.

So coverage is a list in the code (``DERIVED_DRIFT_CHECKS``) rather than a
habit, and these tests assert the list is complete *and* that the guard would
notice if it stopped being.  A guard nobody has seen fail is a guard nobody has
tested.
"""
import pytest
from bson.objectid import ObjectId

from caper import feature_index
from caper.feature_index import (
    DERIVED_COLLECTIONS,
    feature_index_drift,
    feature_index_handle,
    gene_catalog_drift,
    gene_catalog_handle,
    index_project,
    manifest_handle,
    search_names_handle,
    unchecked_derived_collections,
)

# One symbol no corpus contains, so these tests read their own effect and not
# whatever drift a developer database already carries.
PYTEST_GENE = 'PYTESTONLYGENE1'


def test_every_derived_collection_has_a_drift_check():
    """The list, which is the wall itself."""
    assert unchecked_derived_collections() == [], (
        'a collection is derived here and measured by nothing; add it to '
        'DERIVED_DRIFT_CHECKS and to feature_index_drift')


def test_the_guard_notices_a_collection_nobody_measured(monkeypatch):
    """And the guard has to actually fire, or the test above is decoration.

    Asserting only that today's list is empty would pass just as happily
    against a function that returned ``[]`` unconditionally -- which is the
    shape of the failure it exists to catch.
    """
    monkeypatch.setattr(
        feature_index, 'DERIVED_COLLECTIONS',
        DERIVED_COLLECTIONS + ('pytest_derived_collection',))
    assert unchecked_derived_collections() == ['pytest_derived_collection']


def test_the_registry_names_only_collections_that_exist():
    """A check registered against a collection nothing derives is a lie."""
    assert set(feature_index.DERIVED_DRIFT_CHECKS) <= set(DERIVED_COLLECTIONS)


@pytest.mark.integration
class TestGeneCatalogueDrift:
    """The collection the previous fix left outside the check.

    It has no reader today -- repo-wide the only one is
    ``manage.py compare_search_paths`` -- so drift in it is a hazard, not a
    fault, and the command grades it that way.  That is an argument about how
    loudly to report it, and it was allowed to decide whether to *measure* it,
    which is a different question and the wrong answer to it.
    """

    @pytest.fixture
    def project_with_a_private_gene(self):
        """Index one throwaway row carrying a symbol no corpus has."""
        project = {
            '_id': ObjectId(),
            'project_name': 'pytest gene catalogue drift',
            'private': 'public',
            'project_members': ['someone@example.org'],
            'runs': {
                'pytest_sample_catalogue': [{
                    'Sample_name': 'pytest_sample_catalogue',
                    'Feature_ID': 'pytest_sample_catalogue_amplicon1',
                    'Classification': 'ecDNA',
                    'All_genes': [PYTEST_GENE],
                    'Oncogenes': [],
                    'Location': ["'chr8:127000000-128000000'"],
                    'Reference_version': 'GRCh38',
                }],
            },
        }
        index_project(project)
        try:
            yield project
        finally:
            feature_index_handle.delete_many({'project_id': project['_id']})
            manifest_handle.delete_one({'project_id': project['_id']})
            search_names_handle.delete_many({'name': project['project_name']})
            search_names_handle.delete_many({'name': 'pytest_sample_catalogue'})
            gene_catalog_handle.delete_many({'symbol': PYTEST_GENE})

    def test_a_symbol_the_index_has_and_the_catalogue_does_not(
            self, project_with_a_private_gene):
        """The exact shape the names drifted in: rebuild-only, so a new row
        introduces a symbol the catalogue will not hold until the next one."""
        assert PYTEST_GENE in gene_catalog_drift()['genes_missing']

    def test_a_symbol_the_catalogue_describes_differently(
            self, project_with_a_private_gene):
        """Stale content, not just a stale symbol list.

        The row says the symbol is not an oncogene; the catalogue here says it
        is.  Comparing symbol sets alone would call that agreement.
        """
        gene_catalog_handle.insert_one({
            'symbol': PYTEST_GENE,
            'is_oncogene': True,
            'reference_builds': ['hg38'],
            'source': 'refGene, via AmpliconClassifier',
            'schema_version': feature_index.SCHEMA_VERSION,
        })
        drift = gene_catalog_drift()
        assert PYTEST_GENE not in drift['genes_missing']
        assert PYTEST_GENE in drift['genes_changed']

    def test_a_stored_schema_version_behind_the_builder_is_drift(
            self, project_with_a_private_gene):
        """A builder change makes every stored entry stale, the way it makes
        every stored row stale through ``project_digest``."""
        gene_catalog_handle.insert_one({
            'symbol': PYTEST_GENE,
            'is_oncogene': False,
            'reference_builds': ['hg38'],
            'source': 'refGene, via AmpliconClassifier',
            'schema_version': feature_index.SCHEMA_VERSION - 1,
        })
        assert PYTEST_GENE in gene_catalog_drift()['genes_changed']

    def test_the_standing_check_reports_it(self, project_with_a_private_gene):
        """Reported by the function the command calls, not only by its own.

        ``gene_catalog_drift`` being correct on its own is what
        ``search_name_drift`` would have been if it had existed: correct, and
        never called.
        """
        assert PYTEST_GENE in feature_index_drift()['genes_missing']
