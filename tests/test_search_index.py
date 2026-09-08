"""The indexed search must be indistinguishable from the one it replaces.

Not "close enough", and not "better": indistinguishable. A result that is
tidier -- a deduplicated gene list, a normalised spelling, a sorted order --
is a difference, and a difference means the two paths cannot be swapped without
someone noticing. Improvements come after the swap, on purpose.

The exhaustive comparison lives in `manage.py compare_search_paths`, which runs
a corpus of queries down both paths against real data. These tests cover the
translation rules that comparison would only catch if the corpus happened to
include the right query.
"""
import pytest

from caper.search_index import (
    CLASSIFICATION_ALIASES,
    _classification_query,
    _gene_query,
    _metadata_query,
    build_index_query,
    can_serve,
)


# ---------------------------------------------------------------------------
# Gene terms
# ---------------------------------------------------------------------------

def test_single_gene_is_an_equality_match_not_a_regex():
    """Equality uses the index; a regex is a collection scan on DocumentDB.

    An anchored prefix regex was measured as a COLLSCAN on dev 2026-09-07,
    where MongoDB would have used the index -- so a translation that reached
    for a regex here would be correct and useless.
    """
    assert _gene_query('MYC') == {'genes': 'MYC'}


def test_gene_terms_are_upper_cased_to_match_the_indexed_array():
    assert _gene_query('myc') == {'genes': 'MYC'}


def test_and_wins_over_or_exactly_as_it_does_today():
    """`&` takes precedence wherever it appears and `|` stays literal text.

    This reproduces a defect: `MYC|EGFR&CDK4` searches for a gene literally
    named `MYC|EGFR` and returns nothing, with no error. Fixing it here would
    make the two paths disagree, so it is fixed in the API surface instead,
    where the operator lives in the parameter name.
    """
    query = _gene_query('MYC|EGFR&CDK4')
    assert query == {'$and': [{'genes': 'MYC|EGFR'}, {'genes': 'CDK4'}]}


def test_or_across_genes():
    assert _gene_query('MYC|EGFR') == {'$or': [{'genes': 'MYC'}, {'genes': 'EGFR'}]}


def test_and_across_genes_is_per_row_which_means_one_amplicon():
    """Today's AND means both genes on the same focal amplification.

    Each index row is one feature, so an AND of two clauses against the same
    row is the same claim the pandas filter makes.
    """
    assert _gene_query('MYC&EGFR') == {'$and': [{'genes': 'MYC'}, {'genes': 'EGFR'}]}


@pytest.mark.parametrize('pattern, regex', [
    ('MY*', '^MY.*$'),
    ('*GFR', '^.*GFR$'),
    ('*ORF*', '^.*ORF.*$'),
])
def test_wildcards_become_the_same_anchored_regex_the_old_path_builds(pattern, regex):
    assert _gene_query(pattern) == {'genes': {'$regex': regex}}


def test_empty_gene_query_contributes_nothing():
    assert _gene_query('') is None
    assert _gene_query(None) is None


# ---------------------------------------------------------------------------
# Classification, including the three zero-feature branches
# ---------------------------------------------------------------------------

def test_linear_amplification_still_matches_the_short_spelling():
    query = _classification_query('LINEAR AMPLIFICATION', include_no_amp=False,
                                  no_filter=False)
    assert query['classification']['$regex'] == 'LINEAR AMPLIFICATION|LINEAR'


def test_complex_non_cyclic_matches_however_it_is_punctuated():
    query = _classification_query('COMPLEX NON-CYCLIC', include_no_amp=False,
                                  no_filter=False)
    assert query['classification']['$regex'] == r'COMPLEX.?NON.?CYCLIC'


def test_classification_aliases_match_the_ones_the_old_path_hard_codes():
    """Guard: the two spellings are written inline in get_samples_from_features.

    If one gains a third alias and this does not, a classification silently
    stops matching on one path only.
    """
    import inspect

    from caper import search

    source = inspect.getsource(search.get_samples_from_features)
    for expected in CLASSIFICATION_ALIASES.values():
        assert expected in source, (
            f'{expected!r} is no longer what search.py builds; the indexed path '
            f'would match a different set of classifications')


def test_class_filter_with_no_amp_keeps_the_zero_feature_rows_beside_it():
    query = _classification_query('ECDNA', include_no_amp=True, no_filter=False)
    assert query == {'$or': [{'classification': {'$regex': 'ECDNA', '$options': 'i'}},
                             {'has_amplicon': False}]}


def test_only_the_no_amp_box_keeps_only_zero_feature_rows():
    assert _classification_query(None, include_no_amp=True, no_filter=False) == {
        'has_amplicon': False}


def test_every_amp_type_but_not_no_amp_excludes_the_zero_feature_rows():
    assert _classification_query(None, include_no_amp=False, no_filter=False) == {
        'has_amplicon': True}


def test_no_filter_constrains_nothing():
    assert _classification_query(None, include_no_amp=True, no_filter=True) is None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_cancer_type_searches_tissue_of_origin_too_as_one_or():
    """The box on the page is 'Cancer Type or Tissue', and or is not and.

    Splitting it into two filters would AND them, and a sample whose cancer
    type matched but whose tissue did not would vanish.
    """
    query = _metadata_query(['metadata.Cancer_type', 'metadata.Tissue_of_origin'],
                            'BRAIN')
    assert '$or' in query
    assert {clause_key for clause in query['$or'] for clause_key in clause} == {
        'metadata.Cancer_type', 'metadata.Tissue_of_origin'}


def test_metadata_matching_is_substring_and_case_insensitive():
    query = _metadata_query(['metadata.Sample_type'], 'cell line')
    assert query['metadata.Sample_type']['$options'] == 'i'
    assert query['metadata.Sample_type']['$regex'] == 'cell\\ line'


def test_quoted_metadata_term_is_anchored():
    query = _metadata_query(['metadata.Sample_type'], '"Cell Line"')
    assert query['metadata.Sample_type']['$regex'] == '^Cell\\ Line$'


# ---------------------------------------------------------------------------
# What it declines
# ---------------------------------------------------------------------------

def test_free_text_metadata_is_declined_rather_than_approximated():
    """The old implementation narrows column by column and keeps the last
    narrowing that matched anything, which depends on column order. Reproducing
    that means reproducing an accident; approximating it returns different
    samples. So it says no and the old path serves it.
    """
    assert can_serve(extra_metadata=None) is True
    assert can_serve(extra_metadata='') is True
    assert can_serve(extra_metadata='anything') is False


def test_unknown_parameters_cannot_make_it_claim_more_than_it_serves():
    assert can_serve(some_future_filter='x', extra_metadata='y') is False


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_a_name_query_that_matches_nothing_returns_none_not_an_empty_filter():
    """None and {} are different answers and the caller has to tell them apart.

    A filter that matches no names means no rows can match. An absent filter
    means every row is still a candidate. Collapsing the two would turn a
    search for a project that does not exist into a search for everything.
    """
    import caper.search_index as search_index

    original = search_index._resolve_names
    try:
        search_index._resolve_names = lambda kind, pattern, strip=True: []
        assert search_index.build_index_query(project_name='NOPE') is None
    finally:
        search_index._resolve_names = original


def test_access_filter_is_always_part_of_the_query():
    """The visibility clause is not optional and not the caller's to remember."""
    access = {'visibility': {'$in': [False, 'public']}}
    query = build_index_query(genequery='MYC', access=access)
    assert access in query['$and']


# ---------------------------------------------------------------------------
# The staleness guard
# ---------------------------------------------------------------------------

def test_a_stale_index_is_not_used():
    """A short answer is worse than a slow one.

    An index missing a project does not fail a search -- it returns fewer rows
    than the site holds, and nothing in the result says so. This is the case
    that would ship a silently incomplete search, so the read checks first.
    """
    import caper.feature_index as feature_index

    original = feature_index.index_coverage
    try:
        feature_index.index_coverage = lambda: {'indexable': 33, 'indexed': 31, 'outdated': 0}
        assert feature_index.index_is_usable() is False

        feature_index.index_coverage = lambda: {'indexable': 33, 'indexed': 33, 'outdated': 0}
        assert feature_index.index_is_usable() is True
    finally:
        feature_index.index_coverage = original


def test_an_empty_index_is_not_mistaken_for_an_empty_site():
    """Zero indexed against zero indexable is not 'current', it is 'not built'.

    A deployment that turned the flag on before building would otherwise report
    a site with no projects at all, which looks like data loss rather than like
    a missing step.
    """
    import caper.feature_index as feature_index

    original = feature_index.index_coverage
    try:
        feature_index.index_coverage = lambda: {'indexable': 0, 'indexed': 0, 'outdated': 0}
        assert feature_index.index_is_usable() is False
    finally:
        feature_index.index_coverage = original

def test_rows_from_an_older_builder_are_not_used():
    """Full coverage is not the same as usable coverage.

    After a SCHEMA_VERSION bump every project is still present and still
    indexed, so the two counts agree while the rows are the wrong shape -- any
    query touching a field the old builder did not write answers zero. Caught on
    the bump to 4, which added sample_key: deploying without this would have
    served the sample-level gene AND as an empty result until someone rebuilt,
    and an empty result is indistinguishable from a real one.
    """
    import caper.feature_index as feature_index

    original = feature_index.index_coverage
    try:
        feature_index.index_coverage = lambda: {
            'indexable': 33, 'indexed': 33, 'outdated': 1}
        assert feature_index.index_is_usable() is False

        feature_index.index_coverage = lambda: {
            'indexable': 33, 'indexed': 33, 'outdated': 0}
        assert feature_index.index_is_usable() is True
    finally:
        feature_index.index_coverage = original


def test_the_outdated_count_is_computed_against_the_current_schema():
    """The guard must read SCHEMA_VERSION, not a number written beside it."""
    import inspect
    import caper.feature_index as feature_index

    source = inspect.getsource(feature_index.index_coverage)
    assert 'SCHEMA_VERSION' in source



def test_the_guard_is_consulted_on_every_search_not_once_at_boot():
    """An index that falls behind mid-run must stop being used immediately.

    Caching the answer at import would keep serving short results until a
    restart, and the restart is exactly what nobody does when search looks
    like it is working.
    """
    import inspect

    from caper import search

    source = inspect.getsource(search.perform_search)
    assert 'index_is_usable()' in source


def test_the_flag_is_read_at_call_time():
    """So it can be flipped by restarting a worker, not by a release."""
    import inspect

    from caper import search

    source = inspect.getsource(search.feature_index_search_enabled)
    assert 'settings' in source


def test_the_results_table_lists_projects_by_id_not_by_name():
    """A name is not an identity, and two LIVE projects can share one.

    Measured on caper-dev 2026-09-07: four names are held by more than one LIVE
    project, two of them both called 'test' with 118 samples and 7. Filtering
    the results table by name listed both whenever a sample in either matched,
    showing the description, date and sample count of a project that
    contributed nothing to the search.
    """
    import inspect

    from caper import search

    source = inspect.getsource(search.perform_search)
    assert 'public_project_ids' in source
    assert 'proj["project_name"] in public_project_names' not in source
