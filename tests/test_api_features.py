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

from urllib.parse import quote
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
from caper.classifications import is_no_amplicon  # noqa: E402


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
        has_amplicon=not is_no_amplicon(classification),
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
    tissue = ObjectId()
    names = ObjectId()
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
        # The same thing, as rows written before the indexer learned that a
        # null Classification means "found nothing": stored blank, and flagged
        # as carrying an amplicon.  1,002 rows on prod and 4,117 on dev looked
        # like this when it was measured, 2026-09-08, and they answered no
        # classification query at all.  Built by hand rather than through _row()
        # precisely because _row() now gets this right; the corpus has to hold
        # what the old writer left behind.
        dict(_row(public, 'Pub', 'public', 'S5', 'S5_a1', 'NA', []),
             classification='', has_amplicon=True),
        # A build the fold map has never heard of.  Prod carries mm10 and
        # neither dev nor the local corpus did, so a static allowlist advertised
        # it through /facets/ and then 400'd anyone who filtered on it.
        _row(public, 'Pub', 'public', 'S4', 'S4_a1', 'ecDNA', ['MYC'],
             build='mm10'),
        # Not visible to an anonymous caller.
        _row(private, 'Priv', 'private', 'S9', 'S9_a1', 'ecDNA', ['MYC'],
             members=['someone@example.org']),
        # The same tissue spelled two ways, which is what prod's metadata
        # actually looks like: 16 case-collision groups across the three
        # metadata fields on 2026-09-08, and dev had none -- so dev could not
        # show that /facets/ counted 'Lung' and 'lung' separately while
        # filtering on either returned both.  Held in their own project so the
        # counts the other tests assert stay about what those tests are for.
        _row(tissue, 'Tiss', 'public', 'T1', 'T1_a1', 'BFB', ['KRAS'],
             build='hg19', metadata={'Tissue_of_origin': 'Lung'}),
        _row(tissue, 'Tiss', 'public', 'T2', 'T2_a1', 'BFB', ['KRAS'],
             build='hg19', metadata={'Tissue_of_origin': 'lung'}),
        # Two cell lines whose names nest, which is what defeats a client-side
        # prefix match: HOS and HOS-MNNG are different lines, and 315 of the
        # corpus's normalised names are a prefix of another (prod, 2026-09-12).
        # 'S1' as well, because it is also a sample name in the 'Pub' project
        # above -- 2,324 names occur in more than one project, so a count of
        # distinct names is not a count of samples.  Their own project, so the
        # counts the other tests assert stay about what those tests are for.
        _row(names, 'Names', 'public', 'HOS', 'HOS_a1', 'ecDNA', ['MYC']),
        _row(names, 'Names', 'public', 'HOS-MNNG', 'HOS-MNNG_a1', 'ecDNA', ['MYC']),
        _row(names, 'Names', 'public', 'HOS-MNNG', 'HOS-MNNG_a2', 'BFB', ['MYC']),
        _row(names, 'Names', 'public', 'S1', 'S1_a1', 'ecDNA', ['MYC']),
    ]
    feature_index_handle.insert_many(rows)
    monkeypatch.setattr(api_features, 'index_is_usable', lambda: True)
    yield {'public': public, 'private': private, 'tissue': tissue,
           'names': names}
    feature_index_handle.delete_many(
        {'project_id': {'$in': [public, private, tissue, names]}})


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
    # Two cleared samples, stored under two spellings -- 'NA' and the blank a
    # pre-fix indexer wrote for a null Classification.  Both are the same answer
    # to "which samples came back clean", so both have to come back.
    assert _scoped(corpus, 'classification=None&count_only=true').data['count'] == 2
    assert _scoped(corpus, 'classification=NA&count_only=true').data['count'] == 2
    assert _scoped(corpus, 'classification=ecDNA,None&count_only=true').data['count'] == 5


def test_reference_build_breakdown_sums_to_the_count(corpus):
    resp = _scoped(corpus, 'count_only=true')
    assert resp.data['reference_builds'] == {'hg38': 4, 'hg19': 1, 'mm10': 1}
    assert sum(resp.data['reference_builds'].values()) == resp.data['count']


def test_reference_build_filter_folds_equivalent_names(corpus):
    assert _scoped(corpus, 'reference_build=GRCh38&count_only=true').data['count'] == 4
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
    """Just enough user for the code under test.

    index_access_filter reads username/email/is_authenticated; the throttle
    identifies an authenticated caller by pk.
    """
    is_authenticated = True
    pk = 1

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
    """Every facet must be filterable *under the name the facets response uses*.

    The point of facets is that a client stops guessing -- so the name it reads
    there has to be the name the filter accepts, and the count it reads there
    has to be the count filtering on that value returns.  Both halves matter,
    and only the first was checked before.

    The earlier version of this test carried a translation map with
    ``'tissue_of_origin': 'tissue'`` in it, encoding the very mismatch it should
    have failed on.  Because it only asserted "not a 400", it passed while
    ``?tissue_of_origin=lung`` silently dropped the parameter and returned the
    whole corpus -- 37,795 rows on prod against the 33,722 the facet advertised.
    An ignored filter is worse than a rejected one: the caller gets a plausible
    number back and no way to tell it answers a different question.

    So: no translation map, and compare counts, not statuses.  Facets and search
    run the same access filter over the same index, so the two counts are
    comparable exactly, whatever else is in the collection.
    """
    from caper.views_apis import FeatureFacetsView
    request = RequestFactory(SERVER_NAME='localhost').get('/api/v1/features/facets/')
    request.user = AnonymousUser()
    facets = FeatureFacetsView.as_view()(request)
    assert facets.status_code == 200

    wrong = []
    total = facets.data['total_rows']
    assert total > 0
    for facet_name, entries in facets.data['facets'].items():
        for entry in entries:
            resp = _get('?%s=%s&count_only=true'
                        % (facet_name, quote(str(entry['value']))))
            if resp.status_code != 200:
                wrong.append('%s=%r advertised by /facets/, refused by the '
                             'filter: %s %s' % (facet_name, entry['value'],
                                                resp.status_code,
                                                resp.data.get('code')))
            elif resp.data['count'] != entry['count']:
                wrong.append('%s=%r: /facets/ says %d, filtering returns %d'
                             % (facet_name, entry['value'], entry['count'],
                                resp.data['count']))
    assert not wrong, 'facets and the filter disagree:\n  ' + '\n  '.join(wrong)


def test_an_unknown_parameter_is_refused(corpus):
    """A parameter this endpoint does not implement is a 400, never silence.

    Silently ignoring it returns the unfiltered corpus, which the caller reads
    as the answer to the narrower question they asked.  That is how the
    ``tissue_of_origin`` mismatch above stayed invisible, and it is the failure
    mode an agent composing a query from the OpenAPI document is most likely to
    hit -- a plausible name that does not exist.
    """
    resp = _get('?nonsense_parameter=xyz')
    assert resp.status_code == 400
    assert resp.data['code'] == 'invalid_parameter'
    assert 'nonsense_parameter' in resp.data['error']


def test_the_legacy_tissue_alias_still_answers(corpus):
    """``tissue`` was the original name and is still accepted.

    Renaming a published parameter without keeping the old one working breaks
    whoever already wrote the old spelling.
    """
    canonical = _get('?tissue_of_origin=Lung&count_only=true')
    legacy = _get('?tissue=Lung&count_only=true')
    assert canonical.status_code == legacy.status_code == 200
    assert canonical.data['count'] == legacy.data['count']


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


# ---------------------------------------------------------------------------
# Authenticated access, through the view
# ---------------------------------------------------------------------------

def test_a_token_gets_the_caller_their_private_rows(corpus, monkeypatch):
    """The link the other membership test cannot reach.

    test_a_member_sees_their_private_rows calls search_features directly,
    because setting request.user does nothing -- the view resolves its caller
    from an API token. That leaves the view's own wiring untested: token ->
    user -> index_access_filter -> rows. This patches the authenticator rather
    than the endpoint, so _authenticate_api_request still does its real work and
    the whole chain is exercised.

    This is the answer to "how does someone pull their private projects": a
    token, and nothing else changes about the request.
    """
    from rest_framework.authentication import TokenAuthentication
    from caper.views_apis import FeatureSearchView

    member = _Member('someone', 'someone@example.org')
    monkeypatch.setattr(TokenAuthentication, 'authenticate',
                        lambda self, request: (member, 'a-token'))

    request = RequestFactory(SERVER_NAME='localhost').get(
        '/api/v1/features/?gene_any=MYC&limit=500',
        HTTP_AUTHORIZATION='Token a-token')
    request.user = AnonymousUser()
    resp = FeatureSearchView.as_view()(request)

    assert resp.status_code == 200, resp.data
    ids = {row['project_id'] for row in resp.data['results']}
    assert str(corpus['private']) in ids, 'a token did not unlock the caller\'s own rows'
    assert str(corpus['public']) in ids


def test_a_bad_token_is_401_and_not_a_quiet_downgrade(corpus, monkeypatch):
    """A rejected token must not silently serve the anonymous view of the corpus.

    Falling back to public results would hand the caller a smaller answer that
    looks like a complete one -- the same failure the stale-index 503 exists to
    prevent, arriving through the front door.
    """
    from rest_framework.exceptions import AuthenticationFailed
    from rest_framework.authentication import TokenAuthentication
    from caper.views_apis import FeatureSearchView

    def reject(self, request):
        raise AuthenticationFailed('Invalid token.')

    monkeypatch.setattr(TokenAuthentication, 'authenticate', reject)
    request = RequestFactory(SERVER_NAME='localhost').get(
        '/api/v1/features/?gene_any=MYC', HTTP_AUTHORIZATION='Token nope')
    request.user = AnonymousUser()
    resp = FeatureSearchView.as_view()(request)

    assert resp.status_code == 401
    assert resp.data['code'] == 'invalid_token'


def test_a_null_classification_is_indexed_as_no_amplicon():
    """A cleared sample reaches the indexer with Classification null, not 'NA'.

    This is the shape that produced the defect: AmpliconClassifier found nothing,
    the aggregator wrote a JSON null, the index reader turned it into '' and the
    old predicate -- which knew only the strings 'NA' and 'NO FSCNA' -- concluded
    the row had an amplicon.  Measured 2026-09-08, that was 1,002 rows on prod
    and 4,117 on dev: rows that answered no classification query at all, since
    ?classification=None matched the flag and no real class matched a blank.

    Built from a project document rather than by calling the predicate, because
    the bug was in the two steps between the stored null and the flag.
    """
    project = {
        '_id': ObjectId(),
        'project_name': 'Cleared',
        'private': 'public',
        'project_members': [],
        'runs': {
            'S_clean': [{'Feature_ID': 'S_clean_NA', 'Classification': None,
                         'All genes': [''], 'Location': ['']}],
            'S_amp': [{'Feature_ID': 'S_amp_1', 'Classification': 'ecDNA',
                       'All genes': ['MYC'], 'Location': ["'chr8:1-2'"]}],
        },
    }

    rows = {row['feature_id']: row
            for row in feature_index.feature_rows_for_project(project)}

    cleared = rows['S_clean_NA']
    assert cleared['has_amplicon'] is False
    # And it is stored under one spelling, so /facets/ cannot advertise 'NA'
    # while a blank row hides behind it.
    assert cleared['classification'] == 'NA'
    assert rows['S_amp_1']['has_amplicon'] is True


def test_no_amplicon_rows_are_reachable_however_they_were_spelled(corpus):
    """?classification=None finds both spellings, and /facets/ counts both.

    The two halves are asserted together on purpose: the defect showed up as
    /facets/ dropping the blank rows while the filter's flag also missed them,
    so the rows were invisible from both sides at once and no count disagreed
    with any other count.  Only a row known to exist can catch that.
    """
    from caper.views_apis import FeatureFacetsView
    request = RequestFactory(SERVER_NAME='localhost').get('/api/v1/features/facets/')
    request.user = AnonymousUser()
    facets = FeatureFacetsView.as_view()(request)
    assert facets.status_code == 200

    entries = {e['value']: e['count']
               for e in facets.data['facets']['classification']}
    assert 'None' in entries, 'cleared samples must be advertised as a value'
    assert '' not in entries, 'a blank is not its own facet value'

    # The invariant, over whatever the collection holds: what /facets/ advertises
    # for 'None' is what filtering on it returns.
    resp = _get('?classification=None&count_only=true')
    assert resp.status_code == 200
    assert resp.data['count'] == entries['None']

    # And within the known corpus, that is both spellings and not just one.
    assert _scoped(corpus, 'classification=None&count_only=true').data['count'] == 2


def test_facet_values_never_differ_only_by_case(corpus):
    """Two spellings of one value must be one entry, because the filter is one query.

    The filter folds case, so ?tissue_of_origin=Lung and ?tissue_of_origin=lung
    return the same rows. A facet list that carries both spellings with separate
    counts therefore advertises two numbers neither of which filtering will
    reproduce -- on prod that was 'Lung' at 88 and 'lung' at 379 against 467
    either way, and 27 disagreements in total.

    test_facet_values_are_all_filterable catches this too, but only against a
    corpus that contains a collision, which dev's did not. This states the
    property directly so it cannot pass by absence.
    """
    from caper.views_apis import FeatureFacetsView
    request = RequestFactory(SERVER_NAME='localhost').get('/api/v1/features/facets/')
    request.user = AnonymousUser()
    facets = FeatureFacetsView.as_view()(request)

    for facet_name, entries in facets.data['facets'].items():
        seen = {}
        for entry in entries:
            key = str(entry['value']).strip().lower()
            assert key not in seen, (
                '%s advertises %r and %r as separate values; the filter treats '
                'them as one' % (facet_name, seen[key], entry['value']))
            seen[key] = entry['value']


def test_a_case_variant_filters_to_the_advertised_count(corpus):
    """Whichever spelling the caller has, they get the count /facets/ showed."""
    def count(spelling):
        return _get('?project_id=%s&tissue_of_origin=%s&count_only=true'
                    % (corpus['tissue'], spelling)).data['count']

    assert count('Lung') == count('lung') == count('LUNG') == 2, 'both spellings, not one'


# ---------------------------------------------------------------------------
# Samples, as distinct from rows
# ---------------------------------------------------------------------------

def _samples(qs, user=None):
    from caper.views_apis import FeatureSamplesView
    request = RequestFactory(SERVER_NAME='localhost').get(
        '/api/v1/features/samples/' + qs)
    request.user = user or AnonymousUser()
    return FeatureSamplesView.as_view()(request)


def test_sample_count_counts_samples_and_count_counts_rows(corpus):
    """The distinction an agent got wrong against the live API.

    S2 carries two amplicons, so the 'Pub' project's six rows are five samples.
    Reported per response because a paged caller cannot compute it: the rows
    for one sample can straddle a page boundary.
    """
    resp = _scoped(corpus, 'count_only=true')
    assert resp.data['count'] == 6
    assert resp.data['sample_count'] == 5


def test_sample_identity_is_project_and_name_not_name_alone(corpus):
    """'S1' exists in two of the corpus's projects and is two samples.

    Counting distinct sample names instead undercounts: on prod 2,324 names
    occur in more than one project and the shortfall is 11.7% (2026-09-12).
    """
    listed = _samples('?sample_name=S1&limit=500')
    names = {r['project_name'] for r in listed.data['results']}
    assert {'Pub', 'Names'} <= names
    # One name, at least two samples -- and the row endpoint agrees.
    assert listed.data['count'] >= 2
    assert _get('?sample_name=S1&count_only=true').data['sample_count'] \
        == listed.data['count']


def test_count_only_and_the_full_response_agree_on_both_counts(corpus):
    full = _scoped(corpus, 'limit=500')
    counted = _scoped(corpus, 'count_only=true')
    assert counted.data['count'] == full.data['count']
    assert counted.data['sample_count'] == full.data['sample_count']


def test_an_empty_result_still_reports_a_sample_count(corpus):
    """Shape stability: a client reading the field must always find it."""
    resp = _scoped(corpus, 'gene_all=MYC,NOSUCHGENE&count_only=true')
    assert resp.data['count'] == 0
    assert resp.data['sample_count'] == 0


def test_sample_name_is_exact_and_case_insensitive(corpus):
    """Exact on the name, folded on case.

    The case folding is the half that was undocumented, so a caller did it
    client-side and fetched whole projects to do it.
    """
    assert _scoped(corpus, 'sample_name=s1&count_only=true').data['count'] == 1
    assert _scoped(corpus, 'sample_name=S1&count_only=true').data['count'] == 1
    # And exact: S1 does not match S1_something.
    assert _get('?sample_name=HOS&project_id=%s&count_only=true'
                % corpus['names']).data['count'] == 1


def test_sample_name_contains_finds_the_longer_spelling(corpus):
    """The entity-resolution case: a name spelled differently here."""
    resp = _get('?sample_name_contains=MNNG&count_only=true')
    assert resp.data['count'] == 2
    assert resp.data['sample_count'] == 1


def test_sample_name_contains_does_not_silently_merge_nested_names(corpus):
    """'HOS' matches HOS and HOS-MNNG, and the caller is able to see that.

    This is the failure the endpoint exists to prevent: prefix-matching HOS
    client-side merged it with HOS-MNNG, a different line, and reported one
    number for both.
    """
    rows = _get('?sample_name_contains=HOS&count_only=true')
    assert rows.data['count'] == 3
    assert rows.data['sample_count'] == 2

    listed = _samples('?sample_name_contains=HOS&limit=500')
    assert [r['sample_name'] for r in listed.data['results']] == ['HOS', 'HOS-MNNG']
    by_name = {r['sample_name']: r for r in listed.data['results']}
    assert by_name['HOS']['row_count'] == 1
    assert by_name['HOS-MNNG']['row_count'] == 2
    assert by_name['HOS-MNNG']['classifications'] == ['BFB', 'ecDNA']


def test_sample_name_contains_is_a_literal_not_a_pattern(corpus):
    """No wildcards and no operators.

    The site's own query language treats ``*``, ``&`` and ``|`` as operators
    with no way to escape them; that is not a contract to repeat in an API,
    where the value arrives from a URL.
    """
    assert _get('?sample_name_contains=HOS*&count_only=true').data['count'] == 0
    assert _get('?sample_name_contains=.&count_only=true').data['count'] == 0


def test_sample_name_contains_matching_nothing_matches_nothing(corpus):
    """Not 'every row': a dropped clause answers a different question."""
    resp = _get('?sample_name_contains=NOSUCHSAMPLE&count_only=true')
    assert resp.data['count'] == 0


def test_sample_name_contains_refuses_to_expand_too_far(corpus, monkeypatch):
    """A fragment matching most of the corpus is refused, not served slowly."""
    monkeypatch.setattr(api_features, 'MAX_SAMPLE_NAME_MATCHES', 1)
    resp = _get('?sample_name_contains=S&count_only=true')
    assert resp.status_code == 400
    assert resp.data['code'] == 'query_too_broad'


def test_a_row_points_at_its_own_sample(corpus):
    """sample_url named a sample and returned the whole project until 2026-09-12."""
    resp = _get('?project_id=%s&sample_name=HOS-MNNG&limit=1' % corpus['names'])
    row = resp.data['results'][0]
    assert row['sample_url'].endswith(
        '/api/v1/projects/%s/samples/HOS-MNNG/' % corpus['names'])
    assert row['sample_page_url'].endswith(
        '/project/%s/sample/HOS-MNNG' % corpus['names'])


def test_the_two_endpoints_answer_the_same_question(corpus):
    """/features/ sample_count and /features/samples/ count must agree.

    They filter through one shared parser for exactly this reason. The
    facets-versus-filter breaks this file is full of were all two code paths
    that were supposed to mean the same thing and drifted; this is the guard
    against the next one.
    """
    for qs in ('', 'gene_any=MYC', 'classification=ecDNA', 'classification=None',
               'sample_name_contains=HOS', 'reference_build=hg19',
               'gene_all=MYC,EGFR', 'gene_all=MYC,CDK4&same_amp=true',
               'oncogenes_only=true', 'tissue_of_origin=Lung'):
        rows = _get('?count_only=true&' + qs)
        grouped = _samples('?limit=500&' + qs)
        assert rows.status_code == grouped.status_code == 200, qs
        assert rows.data['sample_count'] == grouped.data['count'], qs


def test_grouped_samples_report_their_amplicon_count(corpus):
    """A sample with no amplicon is a result, not a gap."""
    listed = _samples('?project_id=%s&limit=500' % corpus['public'])
    by_name = {r['sample_name']: r for r in listed.data['results']}
    assert by_name['S3']['row_count'] == 1
    assert by_name['S3']['amplicon_count'] == 0
    assert by_name['S3']['classifications'] == ['None']
    assert by_name['S2']['row_count'] == 2
    assert by_name['S2']['amplicon_count'] == 2


def test_grouped_samples_page_exactly_once(corpus):
    """No sample repeated, none skipped, and the walk ends."""
    total = _samples('?limit=500').data['count']
    seen, cursor, pages = [], None, 0
    while True:
        qs = 'limit=2'
        if cursor:
            qs += '&cursor=%s' % cursor
        resp = _samples('?' + qs)
        assert resp.status_code == 200, resp.data
        seen.extend((r['project_id'], r['sample_name']) for r in resp.data['results'])
        cursor = resp.data['next_cursor']
        pages += 1
        if not cursor:
            break
        assert pages <= total + 1, 'cursor walk did not terminate'
    assert len(seen) == total
    assert len(set(seen)) == len(seen), 'a sample appeared on two pages'


def test_grouped_samples_respect_the_access_boundary(corpus):
    listed = _samples('?gene_any=MYC&limit=500')
    assert str(corpus['private']) not in {r['project_id'] for r in listed.data['results']}

    body = api_features.search_feature_samples(
        QueryDict('gene_any=MYC&limit=500'),
        _Member('someone', 'someone@example.org'))
    assert str(corpus['private']) in {r['project_id'] for r in body['results']}


def test_grouped_samples_refuse_a_row_shaped_parameter(corpus):
    """'fields' and 'count_only' describe a row and mean nothing on a sample."""
    for bad in ('fields=sample_name', 'count_only=true'):
        resp = _samples('?' + bad)
        assert resp.status_code == 400, bad
        assert resp.data['code'] == 'invalid_parameter'


def test_grouped_samples_are_503_on_a_stale_index(monkeypatch):
    monkeypatch.setattr(api_features, 'index_is_usable', lambda: False)
    resp = _samples('?gene_any=MYC')
    assert resp.status_code == 503
    assert resp.data['code'] == 'index_unavailable'


# ---------------------------------------------------------------------------
# Per-project metadata coverage
# ---------------------------------------------------------------------------

def test_metadata_coverage_is_the_fraction_of_rows_with_a_value(corpus):
    """Rows, not samples: rows are what a /features/ filter returns.

    The number answers "can a filter on this field reach this project", which
    is the question an agent got wrong against PCAWG unfiltered -- 5,002 rows,
    no cancer type on any of them, and an empty result that read as an answer.
    """
    from django.core.cache import cache
    from caper import feature_index
    cache.delete(feature_index._COVERAGE_CACHE_KEY)
    try:
        coverage = feature_index.metadata_coverage_by_project()
    finally:
        cache.delete(feature_index._COVERAGE_CACHE_KEY)

    # The 'Tiss' project records a tissue for both its rows and nothing else.
    tissue = coverage[str(corpus['tissue'])]
    assert tissue == {'cancer_type': 0.0, 'sample_type': 0.0,
                      'tissue_of_origin': 1.0}
    # The 'Pub' project records none of the three.
    assert coverage[str(corpus['public'])]['tissue_of_origin'] == 0.0


def test_metadata_coverage_counts_na_as_recorded_and_not_provided_as_absent(
        corpus, monkeypatch):
    """'NA' is the submitter saying "not applicable" -- a value, and filterable.

    'Not Provided' is the aggregator's placeholder for a field nobody filled
    in, and /features/facets/ already skips it; counting it as coverage would
    advertise a filter that reaches nothing.
    """
    from django.core.cache import cache
    from caper import feature_index
    marked = ObjectId()
    feature_index_handle.insert_many([
        _row(marked, 'Marked', 'public', 'M1', 'M1_a1', 'ecDNA', ['MYC'],
             metadata={'Cancer_type': 'NA', 'Sample_type': 'Not Provided'}),
        _row(marked, 'Marked', 'public', 'M2', 'M2_a1', 'ecDNA', ['MYC'],
             metadata={'Cancer_type': '', 'Sample_type': 'Cell Line'}),
    ])
    cache.delete(feature_index._COVERAGE_CACHE_KEY)
    try:
        coverage = feature_index.metadata_coverage_by_project()[str(marked)]
    finally:
        cache.delete(feature_index._COVERAGE_CACHE_KEY)
        feature_index_handle.delete_many({'project_id': marked})

    assert coverage['cancer_type'] == 0.5      # 'NA' counts, '' does not
    assert coverage['sample_type'] == 0.5      # 'Cell Line' counts, the
                                               # placeholder does not
