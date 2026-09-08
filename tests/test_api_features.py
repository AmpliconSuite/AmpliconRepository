"""Tests for GET /api/v1/features/ and /api/v1/features/facets/.

Two kinds of test here, deliberately.

The parameter tests are pure and assert exact values: parsing is a contract with
a client and a change to it is a breaking change, so it should be pinned down
precisely.

The query tests build their own rows and assert exact counts.  They first read
whatever the local index happened to hold, which was wrong twice over: the
numbers moved with the data, and -- worse -- running them inside the full suite
returned 503 for all eight, because other tests create projects the index does
not have, coverage mismatches, and the endpoint's staleness guard fires exactly
as designed.  A test that only passes when run alone is measuring the rest of
the suite, not the code.  So these insert a known corpus, force the guard true,
and scope every query to their own project.
"""

import pytest

from caper import api_features
from caper.api_features import FeatureQueryError
from caper.classifications import (
    ACCEPTED_CLASSIFICATION_INPUTS, CANONICAL_CLASSIFICATIONS,
)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected', [
    ('true', True), ('True', True), ('1', True), ('yes', True), ('on', True),
    ('false', False), ('0', False), ('no', False), ('off', False), ('', False),
])
def test_parse_bool_accepts_the_usual_spellings(value, expected):
    assert api_features.parse_bool(value, 'flag') is expected


def test_parse_bool_rejects_anything_else():
    """A typo must not read as false; that is a silently different query."""
    with pytest.raises(FeatureQueryError) as excinfo:
        api_features.parse_bool('maybe', 'same_amp')
    assert excinfo.value.code == 'invalid_parameter'
    assert 'same_amp' in excinfo.value.message


def test_parse_csv_trims_and_drops_empties():
    assert api_features.parse_csv(' MYC , EGFR ,, ') == ['MYC', 'EGFR']
    assert api_features.parse_csv('') == []
    assert api_features.parse_csv(None) == []


def test_limit_defaults_and_bounds():
    assert api_features.parse_limit(None) == api_features.DEFAULT_LIMIT
    assert api_features.parse_limit('10') == 10
    assert api_features.parse_limit(str(api_features.MAX_LIMIT)) == api_features.MAX_LIMIT
    for bad in ('0', '-1', str(api_features.MAX_LIMIT + 1), 'ten'):
        with pytest.raises(FeatureQueryError):
            api_features.parse_limit(bad)


def test_cursor_round_trips_and_rejects_forgeries():
    from bson.objectid import ObjectId
    oid = ObjectId()
    assert api_features.decode_cursor(api_features.encode_cursor(oid)) == oid
    assert api_features.decode_cursor(None) is None
    for bad in ('notacursor', 'YWJj', '!!!!'):
        with pytest.raises(FeatureQueryError) as excinfo:
            api_features.decode_cursor(bad)
        assert excinfo.value.code == 'invalid_cursor'


def test_unknown_field_is_an_error_not_a_missing_column():
    """A dropped column reads as 'no data', which is the wrong answer."""
    with pytest.raises(FeatureQueryError) as excinfo:
        api_features.parse_fields('sample_name,not_a_field')
    assert 'not_a_field' in excinfo.value.message


def test_field_selection_keeps_the_requested_order():
    assert api_features.parse_fields('genes,sample_name') == ['genes', 'sample_name']


def test_unknown_classification_is_400_not_empty():
    """The failure this endpoint exists to avoid: a typo reading as an answer."""
    with pytest.raises(FeatureQueryError) as excinfo:
        api_features.parse_classifications(['ecDNAA'])
    assert excinfo.value.code == 'invalid_classification'


def test_classification_aliases_fold_onto_one_spelling():
    assert api_features.parse_classifications(['ECDNA']) == ['ecDNA']
    assert api_features.parse_classifications(['ecDNA']) == ['ecDNA']
    assert (api_features.parse_classifications(['LINEAR AMPLIFICATION'])
            == api_features.parse_classifications(['Linear']))


def test_no_amplicon_rows_are_filterable_under_one_name():
    """'no focal amplification' is a real question, so it is a value, not a gap."""
    assert api_features.parse_classifications(['NA']) == ['None']
    assert api_features.parse_classifications(['NO FSCNA']) == ['None']
    assert api_features.parse_classifications(['None']) == ['None']


def test_reference_build_folds_equivalents():
    assert api_features.parse_reference_build('GRCh38') == 'hg38'
    assert api_features.parse_reference_build('hg19') == 'hg19'
    with pytest.raises(FeatureQueryError):
        api_features.parse_reference_build('hg17')


def test_every_advertised_classification_is_accepted_by_the_filter():
    """The facets/filter invariant, at the vocabulary level.

    A facets endpoint exists so a client can stop guessing.  Advertising a value
    the filter then rejects with a 400 is worse than advertising nothing: it
    reads as a supported query.  Caught exactly this on the local corpus, where
    the classification facet offered 'NA' and filtering on it was a 400.
    """
    for value in CANONICAL_CLASSIFICATIONS:
        assert value.upper() in ACCEPTED_CLASSIFICATION_INPUTS, value
        assert api_features.parse_classifications([value]) == [value]


# ---------------------------------------------------------------------------
# Queries, against a corpus the test owns
# ---------------------------------------------------------------------------

from bson.objectid import ObjectId  # noqa: E402
from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.http import QueryDict  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from caper import feature_index  # noqa: E402
from caper.feature_index import feature_index_handle  # noqa: E402


def _row(project_id, project_name, visibility, sample, feature_id,
         classification, genes, build='hg38', members=(), metadata=None):
    """One index row, built through the indexer's own row builder.

    Not a hand-written dict: the builder owns the shape, and a test carrying its
    own copy of that shape is the second copy this repository keeps getting
    caught by.  Going through _row() means a change to the row shape breaks
    these tests instead of silently diverging from them.
    """
    return feature_index._row(
        project_id=project_id, project_name=project_name,
        project_sample_count=3, visibility=visibility, members=list(members),
        run_key=sample, sample_name=sample, feature_id=feature_id,
        classification=classification,
        genes=[g.upper() for g in genes], genes_display=list(genes),
        oncogenes=[], oncogenes_display=[], locations=['chr8:1-2'],
        reference_build=build, metadata=(metadata or {}), extra_metadata={},
        has_amplicon=classification.upper() not in feature_index.NO_AMPLICON_CLASSIFICATIONS,
    )


@pytest.fixture
def corpus(monkeypatch):
    """A small, known set of rows, and a guard forced true.

    The guard is forced rather than satisfied because making index_coverage()
    agree would mean creating real projects through the aggregator -- about five
    seconds each -- to test query translation, which is not what these assert.
    test_a_stale_index_is_a_503 covers the guard itself.
    """
    public = ObjectId()
    private = ObjectId()
    rows = [
        # One sample carrying MYC and CDK4 on the SAME amplicon...
        _row(public, 'Pub', 'public', 'S1', 'S1_a1', 'ecDNA', ['MYC', 'CDK4']),
        # ...and one carrying MYC and EGFR on two DIFFERENT amplicons, which is
        # the case that separates gene_all from gene_all+same_amp.
        _row(public, 'Pub', 'public', 'S2', 'S2_a1', 'ecDNA', ['MYC']),
        _row(public, 'Pub', 'public', 'S2', 'S2_a2', 'BFB', ['EGFR'],
             build='hg19'),
        # A sample the classifier found nothing in.
        _row(public, 'Pub', 'public', 'S3', 'S3_a1', 'NA', []),
        # A build the fold map has never heard of.  Prod carries mm10 and
        # neither dev nor the local corpus did, so a static allowlist advertised
        # it through /facets/ and then 400'd anyone who filtered on it.
        _row(public, 'Pub', 'public', 'S4', 'S4_a1', 'ecDNA', ['MYC'],
             build='mm10'),
        # Not visible to an anonymous caller.
        _row(private, 'Priv', 'private', 'S9', 'S9_a1', 'ecDNA', ['MYC'],
             members=['someone@example.org']),
    ]
    feature_index_handle.insert_many(rows)
    monkeypatch.setattr(api_features, 'index_is_usable', lambda: True)
    yield {'public': public, 'private': private}
    feature_index_handle.delete_many({'project_id': {'$in': [public, private]}})


def _get(qs, user=None):
    from caper.views_apis import FeatureSearchView
    request = RequestFactory(SERVER_NAME='localhost').get('/api/v1/features/' + qs)
    request.user = user or AnonymousUser()
    return FeatureSearchView.as_view()(request)


def _scoped(corpus, qs):
    return _get('?project_id=%s&%s' % (corpus['public'], qs))


def test_gene_any_is_an_or(corpus):
    assert _scoped(corpus, 'gene_any=MYC&count_only=true').data['count'] == 3
    assert _scoped(corpus, 'gene_any=MYC,EGFR&count_only=true').data['count'] == 4
    assert _scoped(corpus, 'gene_any=NOSUCHGENE&count_only=true').data['count'] == 0


def test_gene_matching_is_case_insensitive(corpus):
    assert _scoped(corpus, 'gene_any=myc&count_only=true').data['count'] == 3


def test_gene_all_means_the_same_sample_not_the_same_amplicon(corpus):
    """The distinction the contract turns on.

    S2 carries MYC on one amplicon and EGFR on another, so it satisfies
    gene_all and not same_amp.  S1 carries MYC and CDK4 on one amplicon and
    satisfies both.
    """
    # S2's two rows both mention one of the pair, so both come back.
    assert _scoped(corpus, 'gene_all=MYC,EGFR&count_only=true').data['count'] == 2
    assert _scoped(corpus, 'gene_all=MYC,EGFR&same_amp=true&count_only=true').data['count'] == 0
    # S1 has both on one row.
    assert _scoped(corpus, 'gene_all=MYC,CDK4&count_only=true').data['count'] == 1
    assert _scoped(corpus, 'gene_all=MYC,CDK4&same_amp=true&count_only=true').data['count'] == 1


def test_gene_any_and_gene_all_combine_as_an_and(corpus):
    """Supplying both is not an error; the two clauses are ANDed."""
    both = _scoped(corpus, 'gene_all=MYC,EGFR&gene_any=EGFR&count_only=true')
    assert both.status_code == 200, both.data
    assert both.data['count'] == 1


def test_classification_filter_and_the_no_amplicon_value(corpus):
    assert _scoped(corpus, 'classification=ecDNA&count_only=true').data['count'] == 3
    assert _scoped(corpus, 'classification=ECDNA&count_only=true').data['count'] == 3
    assert _scoped(corpus, 'classification=None&count_only=true').data['count'] == 1
    assert _scoped(corpus, 'classification=NA&count_only=true').data['count'] == 1
    assert _scoped(corpus, 'classification=ecDNA,None&count_only=true').data['count'] == 4


def test_reference_build_breakdown_sums_to_the_count(corpus):
    resp = _scoped(corpus, 'count_only=true')
    assert resp.data['reference_builds'] == {'hg38': 3, 'hg19': 1, 'mm10': 1}
    assert sum(resp.data['reference_builds'].values()) == resp.data['count']


def test_reference_build_filter_folds_equivalent_names(corpus):
    assert _scoped(corpus, 'reference_build=GRCh38&count_only=true').data['count'] == 3
    assert _scoped(corpus, 'reference_build=hg19&count_only=true').data['count'] == 1


def test_count_only_agrees_with_the_full_response(corpus):
    full = _scoped(corpus, 'limit=500')
    counted = _scoped(corpus, 'count_only=true')
    assert counted.data['count'] == full.data['count'] == len(full.data['results'])
    assert 'results' not in counted.data
    assert counted.data['reference_builds'] == full.data['reference_builds']


def test_field_selection_returns_exactly_what_was_asked_for(corpus):
    resp = _scoped(corpus, 'fields=sample_name,classification&limit=1')
    assert set(resp.data['results'][0]) == {'sample_name', 'classification'}


def test_rows_report_genes_in_their_source_spelling(corpus):
    """Not the upper-cased form the index matches on.

    1,083 symbols in the corpus are not upper-case, and answering C17ORF37 for
    C17orf37 is answering with a name that does not exist.
    """
    resp = _scoped(corpus, 'gene_any=CDK4&limit=1')
    assert resp.data['results'][0]['genes'] == ['MYC', 'CDK4']


def test_cursor_pages_partition_the_result_set_exactly_once(corpus):
    """No row repeated, none skipped, and the walk ends."""
    total = _scoped(corpus, 'count_only=true').data['count']
    seen, cursor, pages = [], None, 0
    while True:
        qs = 'limit=2&fields=feature_id'
        if cursor:
            qs += '&cursor=%s' % cursor
        resp = _scoped(corpus, qs)
        assert resp.status_code == 200, resp.data
        seen.extend(r['feature_id'] for r in resp.data['results'])
        cursor = resp.data['next_cursor']
        pages += 1
        if not cursor:
            break
        assert pages <= total + 1, 'cursor walk did not terminate'
    assert len(seen) == total
    assert len(set(seen)) == len(seen), 'a row appeared on two pages'


def test_the_last_page_has_no_cursor(corpus):
    resp = _scoped(corpus, 'limit=500')
    assert resp.data['next_cursor'] is None


def test_anonymous_callers_never_see_a_private_row(corpus):
    """The access boundary, asserted on the rows rather than on the query."""
    resp = _get('?gene_any=MYC&limit=500')
    ids = {r['project_id'] for r in resp.data['results']}
    assert str(corpus['private']) not in ids
    assert str(corpus['public']) in ids


class _Member:
    """Just enough user for index_access_filter, which reads these three."""
    is_authenticated = True

    def __init__(self, username, email):
        self.username = username
        self.email = email


def test_a_member_sees_their_private_rows(corpus):
    """Called below the view on purpose.

    The view resolves its user from an API token, so setting request.user does
    nothing -- an earlier version of this test set it, was silently served as
    anonymous, and would have passed while proving nothing about membership.
    Token authentication has its own tests; what needs asserting here is that a
    member's rows come back once a user is resolved.
    """
    body = api_features.search_features(
        QueryDict('gene_any=MYC&limit=500'),
        _Member('someone', 'someone@example.org'))
    ids = {row['project_id'] for row in body['results']}
    assert str(corpus['private']) in ids
    assert str(corpus['public']) in ids


def test_a_non_member_does_not_see_those_rows(corpus):
    body = api_features.search_features(
        QueryDict('gene_any=MYC&limit=500'),
        _Member('someone_else', 'else@example.org'))
    ids = {row['project_id'] for row in body['results']}
    assert str(corpus['private']) not in ids


def test_a_stale_index_is_a_503_and_not_an_empty_answer(monkeypatch):
    """A caller cannot tell a real zero from a broken index, so never answer zero.

    The search page may fall back to the slow path; an API contract cannot,
    because "no ecDNA anywhere" is a plausible-looking wrong answer.
    """
    monkeypatch.setattr(api_features, 'index_is_usable', lambda: False)
    resp = _get('?gene_any=MYC')
    assert resp.status_code == 503
    assert resp.data['code'] == 'index_unavailable'


def test_facet_values_are_all_filterable(corpus):
    """Every value the facets endpoint advertises must be one the filter accepts.

    The point of facets is that a client stops guessing at the vocabulary.  A
    value it offers that the filter then rejects with a 400 is worse than
    silence: it reads as a supported query.  Measured on the local corpus when
    this was written, the classification facet offered 'NA' and
    ?classification=NA was a 400.
    """
    from caper.views_apis import FeatureFacetsView
    request = RequestFactory(SERVER_NAME='localhost').get('/api/v1/features/facets/')
    request.user = AnonymousUser()
    facets = FeatureFacetsView.as_view()(request)
    assert facets.status_code == 200

    param_for = {
        'classification': 'classification',
        'sample_type': 'sample_type',
        'cancer_type': 'cancer_type',
        'tissue_of_origin': 'tissue',
        'reference_build': 'reference_build',
    }
    rejected = []
    for facet_name, param in param_for.items():
        for entry in facets.data.get(facet_name, []):
            resp = _get('?%s=%s&count_only=true' % (param, entry['value']))
            if resp.status_code != 200:
                rejected.append((facet_name, entry['value'], resp.status_code,
                                 resp.data.get('code')))
    assert not rejected, (
        'these values are advertised by /facets/ and refused by the filter:\n'
        + '\n'.join('  %s=%r -> %s %s' % r for r in rejected))


def test_a_build_outside_the_fold_map_is_still_filterable(corpus):
    """Prod carries mm10; the fold map only knows the human assemblies.

    REFERENCE_EQUIVALENCE says which names mean the same assembly. It is not a
    list of the assemblies that exist, and using it as one made every mm10 row
    unreachable while /facets/ advertised them.
    """
    resp = _scoped(corpus, 'reference_build=mm10&count_only=true')
    assert resp.status_code == 200, resp.data
    assert resp.data['count'] == 1


def test_a_build_that_is_in_neither_is_still_a_400(corpus):
    """Checking the data must not turn every typo into an empty result."""
    resp = _scoped(corpus, 'reference_build=hg17&count_only=true')
    assert resp.status_code == 400
    assert resp.data['code'] == 'invalid_parameter'


def test_row_urls_use_the_scheme_the_client_used(corpus):
    """Behind the TLS-terminating ELB the container's own scheme is http.

    Prod handed out http://ampliconrepository.org/... on the first request after
    this endpoint shipped, which is the same defect #600 logged against the
    batch download's download_url.
    """
    from caper.views_apis import FeatureSearchView
    request = RequestFactory(SERVER_NAME='ampliconrepository.org').get(
        '/api/v1/features/?gene_any=MYC&limit=1&fields=project_url,sample_url',
        HTTP_X_FORWARDED_PROTO='https')
    request.user = AnonymousUser()
    resp = FeatureSearchView.as_view()(request)
    row = resp.data['results'][0]
    assert row['project_url'].startswith('https://'), row['project_url']
    assert row['sample_url'].startswith('https://'), row['sample_url']
