"""The feature index must say what a search over the project documents says.

The index is derived data, so the defect to guard against is not corruption --
a rebuild fixes that -- but *divergence*: the index quietly answering a
different question than ``get_samples_from_features`` answers, so that a search
served from the index returns a different set of samples than the same search
served the old way, and nobody notices because both return something.

So the load-bearing tests here are the equivalence ones. The rest cover the
normalisations they depend on.
"""
import copy

import pytest
from bson.objectid import ObjectId

from caper.feature_index import (
    SCHEMA_VERSION,
    feature_rows_for_project,
    normalize_gene,
    normalize_reference,
    project_digest,
)
from caper.search import get_samples_from_features


def _project(**overrides):
    """A project document shaped the way the real ones are.

    ``All_genes`` is written here the way it is actually stored: as a list of
    strings that have been through a repr at some point and carry stray quote
    characters. Writing a clean list would test a document this site does not
    have.
    """
    project = {
        '_id': ObjectId(),
        'project_name': 'Test Project',
        'private': 'public',
        'project_members': ['someone@example.org'],
        'runs': {
            'sample_365': [
                {
                    'Sample_name': 'sample_365',
                    'Feature_ID': 'sample_365_amplicon1',
                    'Classification': 'ecDNA',
                    'All_genes': ["'MYC'", "'PVT1'", "'MYC'"],
                    'Oncogenes': ["'MYC'"],
                    'Location': ["'chr8:127000000-128000000'"],
                    'Reference_version': 'GRCh38',
                    'Sample_type': 'Cell Line',
                    'Cancer_type': 'Glioblastoma',
                    'Tissue_of_origin': 'Brain',
                },
                {
                    'Sample_name': 'sample_365',
                    'Feature_ID': 'sample_365_amplicon2',
                    'Classification': 'BFB',
                    'All_genes': ["'EGFR'"],
                    'Oncogenes': ["'EGFR'"],
                    'Location': ["'chr7:55000000-55300000'"],
                    'Reference_version': 'GRCh38',
                    'Sample_type': 'Cell Line',
                    'Cancer_type': 'Glioblastoma',
                    'Tissue_of_origin': 'Brain',
                },
            ],
            'sample_366': [],
        },
        'sample_data': [
            {'Sample_name': 'sample_366', 'Sample_type': 'Tissue',
             'Cancer_type': 'Sarcoma', 'Tissue_of_origin': 'Bone'},
        ],
    }
    project.update(overrides)
    return project


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ("'MYC'", 'MYC'),
    ('"MYC"', 'MYC'),
    ('  myc  ', 'MYC'),
    ('MYC', 'MYC'),
])
def test_normalize_gene_strips_quotes_and_case(raw, expected):
    assert normalize_gene(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('GRCh37', 'hg19'),
    ('hg19', 'hg19'),
    ('GRCh38', 'hg38'),
    ('GRCh38_viral', 'hg38'),
    ('hg38', 'hg38'),
    ('', ''),
    (None, ''),
])
def test_normalize_reference_matches_coamp_equivalence(raw, expected):
    assert normalize_reference(raw) == expected


def test_unknown_reference_is_kept_verbatim():
    """An unrecognised build is not silently folded into hg19 or hg38.

    Mapping it to one of the two would be a claim about comparability that
    nothing supports, and it would be invisible afterwards. Keeping the string
    means a facet on reference_build shows it and someone can decide.
    """
    assert normalize_reference('mm10') == 'mm10'


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def test_one_row_per_feature_plus_one_per_zero_feature_sample():
    rows = feature_rows_for_project(_project())
    assert len(rows) == 3
    assert {row['feature_id'] for row in rows} == {
        'sample_365_amplicon1', 'sample_365_amplicon2', ''}


def test_genes_are_normalised_and_deduplicated():
    rows = feature_rows_for_project(_project())
    amplicon1 = next(row for row in rows if row['feature_id'] == 'sample_365_amplicon1')
    assert amplicon1['genes'] == ['MYC', 'PVT1']


def test_zero_feature_row_inherits_metadata_from_sample_data():
    """A sample with no amplicon has no feature to carry its metadata.

    get_samples_from_features recovers it from the project's cached
    sample_data, and the index has to recover it from the same place or a
    cancer-type filter would drop these samples where the search page keeps
    them.
    """
    rows = feature_rows_for_project(_project())
    zero = next(row for row in rows if row['sample_name'] == 'sample_366')
    assert zero['metadata']['Cancer_type'] == 'Sarcoma'
    assert zero['metadata']['Tissue_of_origin'] == 'Bone'
    assert zero['has_amplicon'] is False
    assert zero['genes'] == []


@pytest.mark.parametrize('classification', ['NA', 'No FSCNA', 'no fscna', 'na'])
def test_no_amplicon_classifications_are_flagged_whatever_the_case(classification):
    project = _project()
    project['runs']['sample_365'][0]['Classification'] = classification
    rows = feature_rows_for_project(project)
    row = next(row for row in rows if row['feature_id'] == 'sample_365_amplicon1')
    assert row['has_amplicon'] is False


def test_has_amplicon_is_a_stored_flag_not_an_absent_field():
    """Every row carries the flag, so a query can ask for either answer.

    Relying on a field's absence to mean 'no amplicon' is the shape that has
    caught this codebase out before: absence also means 'written by an older
    version', and the two are indistinguishable at query time.
    """
    rows = feature_rows_for_project(_project())
    assert all('has_amplicon' in row for row in rows)
    assert {row['has_amplicon'] for row in rows} == {True, False}


def test_project_with_no_runs_yields_no_rows():
    assert feature_rows_for_project(_project(runs={})) == []


def test_runs_that_is_not_a_dict_yields_no_rows():
    """Upload placeholders and half-written documents exist; they are not rows."""
    assert feature_rows_for_project(_project(runs=None)) == []
    assert feature_rows_for_project(_project(runs=[])) == []


# ---------------------------------------------------------------------------
# The digest, which is what the drift check believes
# ---------------------------------------------------------------------------

def test_digest_ignores_fields_the_rows_are_not_built_from():
    """A download counter ticking is not drift.

    If it were, the drift report would be noise inside a day and nobody would
    read it -- which would cost more than the check is worth.
    """
    project = _project()
    before = project_digest(project)
    project['downloads'] = 41
    project['views'] = 9001
    project['project_downloads'] = {'2026-09-07': 3}
    assert project_digest(project) == before


def test_digest_changes_when_a_gene_changes():
    project = _project()
    before = project_digest(project)
    project['runs']['sample_365'][0]['All_genes'].append("'CDK4'")
    assert project_digest(project) != before


def test_digest_changes_when_visibility_or_membership_changes():
    """Both decide who a row is returned to, so both are part of the source."""
    project = _project()
    before = project_digest(project)
    project['private'] = 'private'
    assert project_digest(project) != before

    project = _project()
    before = project_digest(project)
    project['project_members'].append('someone-else@example.org')
    assert project_digest(project) != before


def test_digest_is_insensitive_to_member_order():
    project = _project(project_members=['a@example.org', 'b@example.org'])
    reordered = _project(project_members=['b@example.org', 'a@example.org'])
    reordered['_id'] = project['_id']
    assert project_digest(project) == project_digest(reordered)


def test_schema_version_is_part_of_the_digest():
    """Changing the builder has to invalidate every stored digest.

    Otherwise a release that changes what a row contains leaves the whole
    corpus reporting 'current' while every row is built the old way.
    """
    import caper.feature_index as fi

    project = _project()
    before = project_digest(project)
    original = fi.SCHEMA_VERSION
    try:
        fi.SCHEMA_VERSION = original + 1
        assert project_digest(project) != before
    finally:
        fi.SCHEMA_VERSION = original


def test_rows_carry_the_schema_version_they_were_built_by():
    rows = feature_rows_for_project(_project())
    assert all(row['schema_version'] == SCHEMA_VERSION for row in rows)


# ---------------------------------------------------------------------------
# Equivalence with the search path being replaced
# ---------------------------------------------------------------------------

def _search_rows(project, **kwargs):
    """Run the existing search over one project.

    Deep-copied because replace_space_to_underscore rewrites the project's
    feature dicts in place, so calling this twice on one document does not
    measure the same thing twice.
    """
    options = {
        'genequery': None, 'classquery': None, 'metadata_sample_name': None,
        'metadata_sample_type': None, 'metadata_cancer_type': None,
        'metadata_tissue_origin': None, 'extra_metadata': None,
        'include_no_amp': True, 'no_filter': True,
    }
    options.update(kwargs)
    return get_samples_from_features([copy.deepcopy(project)], **options)


def _key(sample_name, feature_id):
    return (str(sample_name), str(feature_id))


def test_index_covers_exactly_the_rows_search_returns():
    project = _project()
    indexed = {_key(row['sample_name'], row['feature_id'])
               for row in feature_rows_for_project(project)}
    searched = {_key(row.get('Sample_name'), row.get('Feature_ID', ''))
                for row in _search_rows(project)}
    assert indexed == searched


def test_gene_match_agrees_with_the_search_gene_filter():
    """The index's gene array must select the same rows the pandas filter does.

    This is the test that would fail if normalisation drifted -- a stray quote
    left on one side, or a case difference -- and it is the failure that would
    otherwise show up as a search silently returning fewer samples.
    """
    project = _project()
    indexed = {_key(row['sample_name'], row['feature_id'])
               for row in feature_rows_for_project(project)
               if 'MYC' in row['genes']}
    searched = {_key(row.get('Sample_name'), row.get('Feature_ID', ''))
                for row in _search_rows(project, genequery='MYC')}
    assert indexed == searched
    assert indexed == {('sample_365', 'sample_365_amplicon1')}


def test_and_across_genes_is_per_feature_in_todays_search():
    """Today's '&' means one amplicon carries both genes, not one sample.

    Recorded as a test because the API's gene_all default deliberately widens
    this to the sample, and that widening is a behaviour change someone will
    have to be told about. Measured on prod on 2026-09-07 the widening is
    real and uneven: MYC+EGFR goes from 11 rows to 32 samples, CDK4+MDM2 from
    102 to 114 -- 12q neighbours share an amplicon, genes on different
    chromosomes do not.
    """
    project = _project()
    searched = _search_rows(project, genequery='MYC&EGFR')
    assert searched == []

    rows = feature_rows_for_project(project)
    per_sample = {}
    for row in rows:
        per_sample.setdefault(row['sample_name'], set()).update(row['genes'])
    assert {'MYC', 'EGFR'} <= per_sample['sample_365']


def test_classification_is_stored_verbatim_for_the_class_filter():
    rows = feature_rows_for_project(_project())
    assert {row['classification'] for row in rows} == {'ecDNA', 'BFB', 'NA'}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class _User:
    def __init__(self, authenticated=True, username='someone', email='someone@example.org'):
        self.is_authenticated = authenticated
        self.username = username
        self.email = email


def test_anonymous_sees_public_rows_only():
    from caper.feature_index import index_access_filter
    from caper.visibility import PUBLIC_QUERY_VALUES

    assert index_access_filter(_User(authenticated=False)) == {
        'visibility': {'$in': PUBLIC_QUERY_VALUES}}


def test_anonymous_filter_has_no_membership_branch():
    """The failure to guard against is an unauthenticated request reaching
    restricted rows because a branch was added for the logged-in case and not
    fenced off from the anonymous one."""
    from caper.feature_index import index_access_filter

    assert '$or' not in index_access_filter(_User(authenticated=False))
    assert 'project_members' not in repr(index_access_filter(None))


def test_member_filter_matches_username_and_email():
    """project_members holds both spellings, so matching one of them silently
    hides a user's own projects from them."""
    from caper.feature_index import index_access_filter

    query = index_access_filter(_User())
    restricted = next(branch for branch in query['$or'] if 'project_members' in branch)
    assert restricted['project_members']['$in'] == ['someone', 'someone@example.org']


def test_hidden_public_is_restricted_not_public():
    """Unlisted projects are reachable by URL and must not be search results.

    site_statistics counts hidden_public in its own bucket while access control
    folds it in with private; taking the statistics sense here would publish
    every unlisted project.
    """
    from caper.feature_index import index_access_filter
    from caper.visibility import PUBLIC_QUERY_VALUES, RESTRICTED_QUERY_VALUES

    assert 'hidden_public' not in PUBLIC_QUERY_VALUES
    assert 'hidden_public' in RESTRICTED_QUERY_VALUES

    public_branch = index_access_filter(_User(authenticated=False))
    assert 'hidden_public' not in public_branch['visibility']['$in']


def test_user_without_identities_gets_the_public_filter():
    from caper.feature_index import index_access_filter
    from caper.visibility import PUBLIC_QUERY_VALUES

    assert index_access_filter(_User(username=None, email='')) == {
        'visibility': {'$in': PUBLIC_QUERY_VALUES}}


def test_visibility_values_are_imported_not_restated():
    """A copy of the visibility lists here would be the eleventh hand-written
    copy, and the reason visibility.py exists is that the tenth went stale."""
    source = open('caper/caper/feature_index.py').read()
    assert 'from .visibility import' in source
    assert "'hidden_public'" not in source.split('def index_access_filter')[0]


def test_visibility_is_normalised_into_the_row():
    """Derived data holds one encoding, not the two the projects hold.

    The legacy boolean is what makes ``{'private': False}`` match nothing and
    ``if project['private']`` true for a public project. Neither trap should be
    reachable from the index at all.
    """
    rows = feature_rows_for_project(_project(private=False))
    assert {row['visibility'] for row in rows} == {'public'}

    rows = feature_rows_for_project(_project(private=True))
    assert {row['visibility'] for row in rows} == {'private'}

    rows = feature_rows_for_project(_project(private='hidden_public'))
    assert {row['visibility'] for row in rows} == {'hidden_public'}


def test_rewriting_a_boolean_visibility_to_its_string_is_not_drift():
    """False and 'public' mean the same thing and build the same rows.

    Reporting that as drift would make a visibility backfill look like the
    whole corpus needing a rebuild.
    """
    assert project_digest(_project(private=False)) == project_digest(
        {**_project(private='public'), '_id': None})


def test_changing_visibility_for_real_is_still_drift():
    boolean_private = _project(private=True)
    made_public = {**boolean_private, 'private': 'public'}
    assert project_digest(boolean_private) != project_digest(made_public)


# ---------------------------------------------------------------------------
# Display spelling vs match spelling
# ---------------------------------------------------------------------------

def test_display_genes_keep_their_case():
    """1,083 symbols in the corpus are not upper-case.

    Measured on caper-dev 2026-09-07: 11,986 of 325,264 gene mentions differ
    from their upper-case form, nearly all open reading frame names whose
    canonical refGene spelling is mixed case. Showing C17ORF37 for C17orf37
    would put a symbol on screen that is not the gene's name.
    """
    project = _project()
    project['runs']['sample_365'][0]['All_genes'] = ["'C17orf37'", "'MYC'"]
    rows = feature_rows_for_project(project)
    row = next(r for r in rows if r['feature_id'] == 'sample_365_amplicon1')

    assert row['genes_display'] == ['C17orf37', 'MYC']
    assert row['genes'] == ['C17ORF37', 'MYC']


def test_matching_is_still_case_insensitive():
    """The upper-cased array is what a query matches, so case cannot miss."""
    project = _project()
    project['runs']['sample_365'][0]['All_genes'] = ["'C17orf37'"]
    row = feature_rows_for_project(project)[0]
    assert 'C17ORF37' in row['genes']


def test_display_genes_are_not_deduplicated():
    """Today's output does not deduplicate, so neither does the display array.

    Deduplicating would be an improvement, and an improvement is a difference:
    a result served from the index has to be indistinguishable from one served
    the old way, or the equivalence gate is measuring the wrong thing.
    """
    project = _project()
    project['runs']['sample_365'][0]['All_genes'] = ["'MYC'", "'PVT1'", "'MYC'"]
    row = feature_rows_for_project(project)[0]

    assert row['genes_display'] == ['MYC', 'PVT1', 'MYC']
    assert row['genes'] == ['MYC', 'PVT1']


def test_display_genes_match_what_the_old_search_returns():
    """Byte-for-byte against get_samples_from_features, not by eye."""
    project = _project()
    project['runs']['sample_365'][0]['All_genes'] = ["'C17orf37'", "'MYC'", "'MYC'"]

    indexed = {row['feature_id']: row['genes_display']
               for row in feature_rows_for_project(project)}
    for row in _search_rows(project):
        feature_id = str(row.get('Feature_ID', ''))
        if feature_id in indexed and row.get('All_genes') is not None:
            assert indexed[feature_id] == list(row['All_genes'])


def test_oncogenes_carry_both_spellings_too():
    project = _project()
    project['runs']['sample_365'][0]['Oncogenes'] = ["'C17orf37'"]
    row = feature_rows_for_project(project)[0]
    assert row['oncogenes_display'] == ['C17orf37']
    assert row['oncogenes'] == ['C17ORF37']
