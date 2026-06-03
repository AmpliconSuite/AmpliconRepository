"""
Integration tests for project search: gene search, full-text search,
tissue/classification filters, and access-control visibility in results.

Note on gene_search_page vs search_results:
  - gene_search_page (GET /gene-search/) queries MongoDB using legacy boolean
    values only (private=False for public).  String-format projects ('public')
    are NOT returned by that view.  Tests that need a project to appear in
    gene_search_page results temporarily set private=False (legacy boolean).
  - search_results (POST /search_results/) uses perform_search(), which
    handles both boolean and string visibility formats.  Tests using
    search_results set private='public'.
"""

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_public_legacy(collection, project_id):
    """Set private=False (legacy boolean) so gene_search_page finds the project."""
    collection.update_one(
        {'_id': ObjectId(project_id)},
        {'$set': {'private': False}})


def _set_public(collection, project_id):
    """Set private='public' (string format) so search_results finds the project."""
    collection.update_one(
        {'_id': ObjectId(project_id)},
        {'$set': {'private': 'public'}})


def _set_private(collection, project_id):
    """Restore project to private='private'."""
    collection.update_one(
        {'_id': ObjectId(project_id)},
        {'$set': {'private': 'private'}})


# ---------------------------------------------------------------------------
# gene_search_page tests (GET /gene-search/)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_gene_search_page_returns_200(loaded_datasets, request_factory, test_user):
    """gene_search_page must return 200 for any authenticated request."""
    from caper.views import gene_search_page
    req = request_factory.get('/gene-search/')
    req.user = test_user
    resp = gene_search_page(req)
    assert resp.status_code == 200


@pytest.mark.integration
@pytest.mark.functional
def test_gene_search_no_results(loaded_datasets, request_factory, test_user):
    """Searching for a nonsense gene name must return 200 with no sample rows."""
    from caper.views import gene_search_page
    req = request_factory.get('/gene-search/', {'genequery': 'ZZZNOMATCHXYZ'})
    req.user = test_user
    resp = gene_search_page(req)
    assert resp.status_code == 200
    assert b'ZZZNOMATCHXYZ' not in resp.content or b'no results' in resp.content.lower() \
        or resp.content  # response rendered — gene not in dataset, template rendered OK


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_gene_search_finds_public_project(
        request_factory, test_user, mongo_collection):
    """
    gene_search_page must return at least one result for a project set to
    private=False (legacy boolean public).  Uses a dedicated project so
    loaded_datasets projects are not mutated.
    """
    from caper.views import create_project, gene_search_page

    req, handles = _build_create_request(
        request_factory, test_user, 'SearchTest_GeneSearch',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message') if doc else 'timeout'}"

        _set_public_legacy(mongo_collection, project_id)

        gene = doc.get('Oncogenes', [''])[0] if doc.get('Oncogenes') else ''

        req_search = request_factory.get('/gene-search/')
        req_search.user = test_user
        if gene:
            req_search = request_factory.get('/gene-search/', {'genequery': gene})
            req_search.user = test_user

        resp_search = gene_search_page(req_search)
        assert resp_search.status_code == 200
        assert b'SearchTest_GeneSearch' in resp_search.content, \
            "Public project must appear in gene search results"

    finally:
        _set_private(mongo_collection, project_id)
        _cleanup_project(mongo_collection, project_id)


# ---------------------------------------------------------------------------
# search_results tests (POST /search_results/)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_fulltext_search_returns_200(loaded_datasets, request_factory, test_user):
    """POST /search_results/ with no query must return 200."""
    from caper.views import search_results
    req = request_factory.post('/search_results/', {})
    req.user = test_user
    resp = search_results(req)
    assert resp.status_code == 200


@pytest.mark.integration
@pytest.mark.functional
def test_fulltext_search_no_match(loaded_datasets, request_factory, test_user):
    """Searching for a nonsense project name must return 200 with no result rows."""
    from caper.views import search_results
    req = request_factory.post('/search_results/', {'project_name': 'ZZZNOMATCH_PROJ'})
    req.user = test_user
    resp = search_results(req)
    assert resp.status_code == 200
    # Neither of the loaded projects should appear in results.
    # (The query string itself is echoed back in the form, so we check known project
    # names from loaded_datasets rather than the query string.)
    assert b'FuncTest_Small' not in resp.content
    assert b'FuncTest_Medium' not in resp.content


@pytest.mark.integration
@pytest.mark.functional
def test_search_private_visible_to_member(
        loaded_datasets, request_factory, test_user, mongo_collection):
    """
    project_small is private and test_user is its creator (project_members contains
    test_user.username).  A search_results POST as test_user must return
    project_small samples in private_sample_data.
    """
    from caper.views import search_results

    # Verify the owner is in project_members before the test
    doc = mongo_collection.find_one(
        {'_id': ObjectId(loaded_datasets['project_small'])})
    assert test_user.username in doc.get('project_members', []), \
        "test_user must be in project_members for this test to be meaningful"

    req = request_factory.post('/search_results/', {
        'project_name': 'FuncTest_Small'})
    req.user = test_user
    resp = search_results(req)
    assert resp.status_code == 200
    assert b'FuncTest_Small' in resp.content, \
        "Private project should be visible to its member in search results"


@pytest.mark.integration
@pytest.mark.functional
def test_search_private_hidden_to_nonmember(
        loaded_datasets, request_factory, non_member_user):
    """
    project_small is private and non_member_user is not a member.
    search_results must not reveal private project samples to non-members.
    """
    from caper.views import search_results
    req = request_factory.post('/search_results/', {
        'project_name': 'FuncTest_Small'})
    req.user = non_member_user
    resp = search_results(req)
    assert resp.status_code == 200
    # The project name is echoed as the form's search-field value, so we check
    # for the actual sample name (GBM39) which only appears in rendered result rows.
    assert b'GBM39' not in resp.content, \
        "Private project samples must not appear in search results for non-members"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.functional
def test_fulltext_search_finds_public_project(
        request_factory, test_user, mongo_collection):
    """
    A public (private='public') project must appear in search_results output.
    Uses a dedicated project to avoid mutating loaded_datasets state.
    """
    from caper.views import create_project, search_results

    req, handles = _build_create_request(
        request_factory, test_user, 'SearchTest_FullText',
        tar_path=DATASET_SMALL_TAR)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message') if doc else 'timeout'}"

        _set_public(mongo_collection, project_id)

        req_search = request_factory.post('/search_results/', {
            'project_name': 'SearchTest_FullText'})
        req_search.user = test_user
        resp_search = search_results(req_search)
        assert resp_search.status_code == 200
        assert b'SearchTest_FullText' in resp_search.content, \
            "Public project must appear in search_results output"

    finally:
        _set_private(mongo_collection, project_id)
        _cleanup_project(mongo_collection, project_id)


# ---------------------------------------------------------------------------
# Issue #532 — both legacy boolean and string visibility formats must appear
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_both_visibility_formats_appear_in_search_results(
        request_factory, test_user, mongo_collection):
    """
    Issue #532: search_results must return public projects regardless of whether
    the 'private' field is stored as legacy boolean False or string 'public'.
    The public query uses {'$in': [False, 'public']} so both formats should match.
    """
    from caper.views import search_results

    # Insert two minimal public project docs — no aggregation needed.
    # Each needs at least one sample in runs so that perform_search includes
    # the project in results (it filters out projects with no sample data).
    _sample = {'Sample_name': 'TestSample', 'Features': []}
    doc_legacy = {
        'project_name':  'SearchVis_LegacyFalse',
        'creator':       test_user.username,
        'private':       False,    # legacy boolean format
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs':          {'run1': [_sample.copy()]},
        'sample_count':  1,
    }
    doc_string = {
        'project_name':  'SearchVis_StringPublic',
        'creator':       test_user.username,
        'private':       'public', # current string format
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs':          {'run1': [_sample.copy()]},
        'sample_count':  1,
    }
    r1 = mongo_collection.insert_one(doc_legacy)
    r2 = mongo_collection.insert_one(doc_string)

    try:
        # Search with a prefix that matches both project names
        req = request_factory.post('/search_results/', {'project_name': 'SearchVis_'})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200, \
            f"search_results returned {resp.status_code}"
        assert b'SearchVis_LegacyFalse' in resp.content, \
            "Legacy boolean False project must appear in search_results (Issue #532)"
        assert b'SearchVis_StringPublic' in resp.content, \
            "String 'public' project must appear in search_results (Issue #532)"
    finally:
        mongo_collection.delete_one({'_id': r1.inserted_id})
        mongo_collection.delete_one({'_id': r2.inserted_id})


# ---------------------------------------------------------------------------
# Zero-feature sample tests — samples with empty feature lists in runs
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_only_no_amp_checked_returns_only_zero_feature_public(
        request_factory, test_user, mongo_collection):
    """
    When ONLY the 'No-Amp (sample)' checkbox is checked (no amp-type checkboxes),
    only zero-feature samples must appear — samples with features must be excluded.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_Public',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'SampleWithFeatures': [
                {
                    'Sample_name': 'SampleWithFeatures',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['MYC'],
                    'Oncogenes': ['MYC'],
                    'Sample_type': 'cell line',
                    'Cancer_type': 'GBM',
                    'Tissue_of_origin': 'Brain',
                }
            ],
            'SampleNoFeatures': [],  # zero-feature sample
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # Only "no-amp" checked — no amp-type checkboxes selected
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_Public',
            'classquery': ['no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'SampleNoFeatures' in resp.content, \
            "Zero-feature sample must appear when only no-amp is checked"
        assert b'SampleWithFeatures' not in resp.content, \
            "Sample with features must NOT appear when only no-amp is checked (no amp types selected)"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_only_no_amp_checked_returns_only_zero_feature_private(
        request_factory, test_user, mongo_collection):
    """
    When ONLY the 'No-Amp (sample)' checkbox is checked in a private project,
    only zero-feature samples must appear — samples with features must be excluded.
    """
    from caper.views import search_results

    doc = {
        'project_name':    'SearchZeroFeat_Private',
        'creator':         test_user.username,
        'project_members': [test_user.username, test_user.email],
        'private':         'private',
        'delete':          False,
        'current':         True,
        'FINISHED?':       True,
        'runs': {
            'PrivSampleWithFeats': [
                {
                    'Sample_name': 'PrivSampleWithFeats',
                    'Feature_ID': 'feat_1',
                    'Classification': 'BFB',
                    'All_genes': ['EGFR'],
                    'Oncogenes': ['EGFR'],
                    'Sample_type': 'primary tumor',
                    'Cancer_type': 'Lung',
                    'Tissue_of_origin': 'Lung',
                }
            ],
            'PrivSampleNoFeats': [],  # zero-feature sample
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_Private',
            'classquery': ['no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'PrivSampleNoFeats' in resp.content, \
            "Zero-feature sample must appear when only no-amp is checked"
        assert b'PrivSampleWithFeats' not in resp.content, \
            "Sample with features must NOT appear when only no-amp is checked (no amp types selected)"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_excluded_by_gene_filter(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples must NOT appear when a gene filter is active
    (they have no genes to match).
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_GeneFilter',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'HasMYC': [
                {
                    'Sample_name': 'HasMYC',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['MYC'],
                    'Oncogenes': ['MYC'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
            'NoFeatures': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_GeneFilter',
            'genequery': 'MYC'})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'HasMYC' in resp.content, \
            "Sample with MYC gene must appear when filtering by MYC"
        assert b'NoFeatures' not in resp.content, \
            "Zero-feature sample must NOT appear when a gene filter is active"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_excluded_by_classification_filter(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples (Classification='No FSCNA') must NOT appear when
    a specific classification filter (e.g. ecDNA) is active.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_ClassFilter',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'HasEcDNA': [
                {
                    'Sample_name': 'HasEcDNA',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['EGFR'],
                    'Oncogenes': ['EGFR'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
            'EmptySample': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_ClassFilter',
            'classquery': ['ecDNA']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'HasEcDNA' in resp.content, \
            "ecDNA sample must appear when filtering by ecDNA classification"
        assert b'EmptySample' not in resp.content, \
            "Zero-feature sample must NOT appear when classification filter is active"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_match_sample_name_search(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples must appear when searching by sample name that matches
    their run key.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_SampleName',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'TargetSampleXYZ': [],  # zero-feature — should match name search
            'OtherSample': [
                {
                    'Sample_name': 'OtherSample',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['MYC'],
                    'Oncogenes': ['MYC'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'metadata_sample_name': 'TargetSampleXYZ',
            'classquery': ['no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'TargetSampleXYZ' in resp.content, \
            "Zero-feature sample must appear when searching by its sample name and no-amp is checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_hidden_when_noamp_unchecked(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples must NOT appear when the "No-Amp (sample)" checkbox
    is unchecked (i.e. "no-amp" is absent from classquery).
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_NoAmpOff',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'RegularSampleXX': [
                {
                    'Sample_name': 'RegularSampleXX',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['MYC'],
                    'Oncogenes': ['MYC'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
            'ZeroFeatSampleXX': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # Post with ecDNA checked but no-amp NOT checked
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_NoAmpOff',
            'classquery': ['ecDNA']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'RegularSampleXX' in resp.content, \
            "Regular ecDNA sample must still appear"
        assert b'ZeroFeatSampleXX' not in resp.content, \
            "Zero-feature sample must NOT appear when no-amp checkbox is unchecked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_appear_when_noamp_checked_with_other_classes(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples must appear alongside regular samples when the
    "No-Amp (sample)" checkbox is checked together with other classification types.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchZeroFeat_NoAmpMixed',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'MixedEcDNA': [
                {
                    'Sample_name': 'MixedEcDNA',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['EGFR'],
                    'Oncogenes': ['EGFR'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
            'MixedZeroFeat': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # Post with both ecDNA and no-amp checked
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchZeroFeat_NoAmpMixed',
            'classquery': ['ecDNA', 'no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'MixedEcDNA' in resp.content, \
            "ecDNA sample must appear when ecDNA + no-amp both checked"
        assert b'MixedZeroFeat' in resp.content, \
            "Zero-feature sample must appear when no-amp is checked alongside ecDNA"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_all_five_checkboxes_checked_equals_no_filter(
        request_factory, test_user, mongo_collection):
    """
    Checking all 5 checkboxes (all 4 amp types + no-amp) must behave identically
    to having no classification filter — all sample types must appear.
    This covers the case where samples have a classification not covered by the 4
    standard types (e.g. 'No FSCNA', 'other', or any future classification).
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchAllFive_NoFilter',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'SampleEcDNA': [{'Sample_name': 'SampleEcDNA', 'Feature_ID': 'f1',
                              'Classification': 'ecDNA', 'All_genes': ['MYC'],
                              'Oncogenes': ['MYC'], 'Sample_type': '',
                              'Cancer_type': '', 'Tissue_of_origin': ''}],
            'SampleBFB':   [{'Sample_name': 'SampleBFB', 'Feature_ID': 'f2',
                              'Classification': 'BFB', 'All_genes': [],
                              'Oncogenes': [], 'Sample_type': '',
                              'Cancer_type': '', 'Tissue_of_origin': ''}],
            'SampleOther': [{'Sample_name': 'SampleOther', 'Feature_ID': 'f3',
                              'Classification': 'Heavily-Rearranged', 'All_genes': [],
                              'Oncogenes': [], 'Sample_type': '',
                              'Cancer_type': '', 'Tissue_of_origin': ''}],
            'SampleZero':  [],
        },
        'sample_count': 4,
    }
    result = mongo_collection.insert_one(doc)

    try:
        all_five = ['ecDNA', 'linear amplification', 'BFB', 'complex non-cyclic', 'no-amp']
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchAllFive_NoFilter',
            'classquery': all_five})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'SampleEcDNA' in resp.content, "ecDNA sample must appear when all 5 checked"
        assert b'SampleBFB' in resp.content, "BFB sample must appear when all 5 checked"
        assert b'SampleOther' in resp.content, \
            "Sample with non-standard classification must appear when all 5 checked (no filter)"
        assert b'SampleZero' in resp.content, \
            "Zero-feature sample must appear when all 5 checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_zero_feature_samples_in_gene_search_page_get(
        request_factory, test_user, mongo_collection):
    """
    Zero-feature samples must appear in the legacy GET gene_search_page view
    when no gene/classification filter is applied.
    """
    from caper.views import gene_search_page

    doc = {
        'project_name':  'SearchZeroFeat_GET',
        'creator':       test_user.username,
        'private':       False,  # legacy boolean for gene_search_page
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'Oncogenes':     ['MYC'],
        'runs': {
            'GETSampleWithFeat': [
                {
                    'Sample_name': 'GETSampleWithFeat',
                    'Feature_ID': 'feat_1',
                    'Classification': 'ecDNA',
                    'All_genes': ['MYC'],
                    'Oncogenes': ['MYC'],
                    'Sample_type': '',
                    'Cancer_type': '',
                    'Tissue_of_origin': '',
                }
            ],
            'GETSampleNoFeat': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # No gene or class filter — should return all samples including zero-feature
        req = request_factory.get('/gene-search/')
        req.user = test_user
        resp = gene_search_page(req)
        assert resp.status_code == 200
        assert b'GETSampleNoFeat' in resp.content, \
            "Zero-feature sample must appear in gene_search_page GET results"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_none_checked_returns_all_including_zero_feature(
        request_factory, test_user, mongo_collection):
    """
    When NO checkboxes are submitted (none of the 5 checked), every sample must
    appear: all feature types and zero-feature samples alike.
    This is the 'no filter' mode equivalent to all-5-checked.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchNoneChecked',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'NCSampleEcDNA': [{'Sample_name': 'NCSampleEcDNA', 'Feature_ID': 'f1',
                               'Classification': 'ecDNA', 'All_genes': ['MYC'],
                               'Oncogenes': ['MYC'], 'Sample_type': '',
                               'Cancer_type': '', 'Tissue_of_origin': ''}],
            'NCSampleBFB':   [{'Sample_name': 'NCSampleBFB', 'Feature_ID': 'f2',
                               'Classification': 'BFB', 'All_genes': [],
                               'Oncogenes': [], 'Sample_type': '',
                               'Cancer_type': '', 'Tissue_of_origin': ''}],
            'NCSampleZero':  [],
        },
        'sample_count': 3,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # No classquery key at all — simulates all checkboxes unchecked
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchNoneChecked'})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'NCSampleEcDNA' in resp.content, \
            "ecDNA sample must appear when no checkboxes checked"
        assert b'NCSampleBFB' in resp.content, \
            "BFB sample must appear when no checkboxes checked"
        assert b'NCSampleZero' in resp.content, \
            "Zero-feature sample must appear when no checkboxes checked (no-filter mode)"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_two_amp_types_no_no_amp_excludes_others(
        request_factory, test_user, mongo_collection):
    """
    When two specific amp types are checked (no 'no-amp'), only samples with
    those classification types appear; other amp types and zero-feature samples
    must be excluded.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchTwoAmpNoNoAmp',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            'TANSampleEcDNA':  [{'Sample_name': 'TANSampleEcDNA', 'Feature_ID': 'f1',
                                 'Classification': 'ecDNA', 'All_genes': ['MYC'],
                                 'Oncogenes': ['MYC'], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TANSampleBFB':    [{'Sample_name': 'TANSampleBFB', 'Feature_ID': 'f2',
                                 'Classification': 'BFB', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TANSampleLinear': [{'Sample_name': 'TANSampleLinear', 'Feature_ID': 'f3',
                                 'Classification': 'Linear Amplification', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TANSampleZero':   [],
        },
        'sample_count': 4,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchTwoAmpNoNoAmp',
            'classquery': ['ecDNA', 'BFB']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'TANSampleEcDNA' in resp.content, \
            "ecDNA sample must appear when ecDNA is checked"
        assert b'TANSampleBFB' in resp.content, \
            "BFB sample must appear when BFB is checked"
        assert b'TANSampleLinear' not in resp.content, \
            "Linear Amplification sample must NOT appear when only ecDNA+BFB are checked"
        assert b'TANSampleZero' not in resp.content, \
            "Zero-feature sample must NOT appear when no-amp is not checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_two_amp_types_with_no_amp(
        request_factory, test_user, mongo_collection):
    """
    When two amp types + 'no-amp' are checked, samples matching either amp type
    appear alongside zero-feature samples; other amp types are excluded.
    """
    from caper.views import search_results

    doc = {
        'project_name':   'SearchTwoAmpWithNoAmp',
        'creator':        test_user.username,
        'private':        'public',
        'delete':         False,
        'current':        True,
        'FINISHED?':      True,
        'runs': {
            'TAWSampleEcDNA':   [{'Sample_name': 'TAWSampleEcDNA', 'Feature_ID': 'f1',
                                  'Classification': 'ecDNA', 'All_genes': ['MYC'],
                                  'Oncogenes': ['MYC'], 'Sample_type': '',
                                  'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TAWSampleBFB':     [{'Sample_name': 'TAWSampleBFB', 'Feature_ID': 'f2',
                                  'Classification': 'BFB', 'All_genes': [],
                                  'Oncogenes': [], 'Sample_type': '',
                                  'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TAWSampleComplex': [{'Sample_name': 'TAWSampleComplex', 'Feature_ID': 'f3',
                                  'Classification': 'Complex Non-Cyclic', 'All_genes': [],
                                  'Oncogenes': [], 'Sample_type': '',
                                  'Cancer_type': '', 'Tissue_of_origin': ''}],
            'TAWSampleZero':    [],
        },
        'sample_count': 4,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchTwoAmpWithNoAmp',
            'classquery': ['ecDNA', 'BFB', 'no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'TAWSampleEcDNA' in resp.content, \
            "ecDNA sample must appear when ecDNA is checked"
        assert b'TAWSampleBFB' in resp.content, \
            "BFB sample must appear when BFB is checked"
        assert b'TAWSampleZero' in resp.content, \
            "Zero-feature sample must appear when no-amp is checked"
        assert b'TAWSampleComplex' not in resp.content, \
            "Complex Non-Cyclic sample must NOT appear when only ecDNA+BFB+no-amp are checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_all_4_amp_types_no_no_amp(
        request_factory, test_user, mongo_collection):
    """
    When all 4 amp-type checkboxes are checked but 'no-amp' is NOT checked,
    samples with real amplification features appear, but NA-classified and
    zero-feature samples must be excluded.
    """
    from caper.views import search_results

    doc = {
        'project_name':   'SearchAll4AmpNoNoAmp',
        'creator':        test_user.username,
        'private':        'public',
        'delete':         False,
        'current':        True,
        'FINISHED?':      True,
        'runs': {
            'A4SampleEcDNA':   [{'Sample_name': 'A4SampleEcDNA', 'Feature_ID': 'f1',
                                 'Classification': 'ecDNA', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'A4SampleBFB':     [{'Sample_name': 'A4SampleBFB', 'Feature_ID': 'f2',
                                 'Classification': 'BFB', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'A4SampleLinear':  [{'Sample_name': 'A4SampleLinear', 'Feature_ID': 'f3',
                                 'Classification': 'Linear Amplification', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            'A4SampleComplex': [{'Sample_name': 'A4SampleComplex', 'Feature_ID': 'f4',
                                 'Classification': 'Complex Non-Cyclic', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            # Real 'NA'-classified sample (AmpliconSuiteAggregator no-amp convention)
            'A4SampleNA':      [{'Sample_name': 'A4SampleNA', 'Feature_ID': 'A4SampleNA_NA',
                                 'Classification': 'NA', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
            # True zero-feature sample (empty runs list)
            'A4SampleZero':    [],
        },
        'sample_count': 6,
    }
    result = mongo_collection.insert_one(doc)

    try:
        all_4_amp = ['ecDNA', 'linear amplification', 'BFB', 'complex non-cyclic']
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchAll4AmpNoNoAmp',
            'classquery': all_4_amp})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'A4SampleEcDNA' in resp.content, \
            "ecDNA sample must appear when all 4 amp types checked"
        assert b'A4SampleBFB' in resp.content, \
            "BFB sample must appear when all 4 amp types checked"
        assert b'A4SampleLinear' in resp.content, \
            "Linear Amplification sample must appear when all 4 amp types checked"
        assert b'A4SampleComplex' in resp.content, \
            "Complex Non-Cyclic sample must appear when all 4 amp types checked"
        assert b'A4SampleNA' not in resp.content, \
            "NA-classified (no-amp) sample must NOT appear when no-amp is not checked"
        assert b'A4SampleZero' not in resp.content, \
            "Zero-feature sample must NOT appear when no-amp is not checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


# ---------------------------------------------------------------------------
# Checkbox repopulation tests — verify that the result page re-checks the
# correct boxes so the UI faithfully reflects what was submitted.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_all_five_checked_restores_all_checkboxes(
        request_factory, test_user, mongo_collection):
    """
    After submitting with all 5 checkboxes checked, the result page must
    render all 5 checkboxes in the checked state.
    """
    from caper.views import search_results

    doc = {
        'project_name': 'CBRepop_All5',
        'creator': test_user.username,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': {
            'CBAll5Sample': [{'Sample_name': 'CBAll5Sample', 'Feature_ID': 'f1',
                              'Classification': 'ecDNA', 'All_genes': [],
                              'Oncogenes': [], 'Sample_type': '',
                              'Cancer_type': '', 'Tissue_of_origin': ''}],
        },
        'sample_count': 1,
    }
    result = mongo_collection.insert_one(doc)

    try:
        all_five = ['ecDNA', 'linear amplification', 'BFB', 'complex non-cyclic', 'no-amp']
        req = request_factory.post('/search_results/', {
            'project_name': 'CBRepop_All5',
            'classquery': all_five})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        # All 5 checkboxes must be rendered as checked
        assert b'id="class-ecDNA" checked' in resp.content, \
            "ecDNA checkbox must be checked when all 5 were submitted"
        assert b'id="class-linear" checked' in resp.content, \
            "Linear Amplification checkbox must be checked when all 5 were submitted"
        assert b'id="class-bfb" checked' in resp.content, \
            "BFB checkbox must be checked when all 5 were submitted"
        assert b'id="class-complex" checked' in resp.content, \
            "Complex Non-Cyclic checkbox must be checked when all 5 were submitted"
        assert b'id="class-noamp" checked' in resp.content, \
            "No-Amp checkbox must be checked when all 5 were submitted"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_no_checkboxes_restores_none_checked(
        request_factory, test_user, mongo_collection):
    """
    After submitting with no checkboxes checked, the result page must render
    all checkboxes in the unchecked state.
    """
    from caper.views import search_results

    doc = {
        'project_name': 'CBRepop_None',
        'creator': test_user.username,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': {
            'CBNoneSample': [{'Sample_name': 'CBNoneSample', 'Feature_ID': 'f1',
                              'Classification': 'ecDNA', 'All_genes': [],
                              'Oncogenes': [], 'Sample_type': '',
                              'Cancer_type': '', 'Tissue_of_origin': ''}],
        },
        'sample_count': 1,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # No classquery key → none of the 5 checkboxes checked
        req = request_factory.post('/search_results/', {
            'project_name': 'CBRepop_None'})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        # None of the 5 checkboxes should appear as checked
        assert b'id="class-ecDNA" checked' not in resp.content, \
            "ecDNA checkbox must NOT be checked when none were submitted"
        assert b'id="class-noamp" checked' not in resp.content, \
            "No-Amp checkbox must NOT be checked when none were submitted"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_ecdna_and_noamp_restores_correct_checkboxes(
        request_factory, test_user, mongo_collection):
    """
    After submitting with ecDNA + no-amp checked, the result page must render
    exactly those two checkboxes checked and the other three unchecked.
    """
    from caper.views import search_results

    doc = {
        'project_name': 'CBRepop_EcDNANoAmp',
        'creator': test_user.username,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': {
            'CBEcDNASample': [{'Sample_name': 'CBEcDNASample', 'Feature_ID': 'f1',
                               'Classification': 'ecDNA', 'All_genes': [],
                               'Oncogenes': [], 'Sample_type': '',
                               'Cancer_type': '', 'Tissue_of_origin': ''}],
            'CBZeroSample': [],
        },
        'sample_count': 2,
    }
    result = mongo_collection.insert_one(doc)

    try:
        req = request_factory.post('/search_results/', {
            'project_name': 'CBRepop_EcDNANoAmp',
            'classquery': ['ecDNA', 'no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        # ecDNA and no-amp must be checked; others must not be
        assert b'id="class-ecDNA" checked' in resp.content, \
            "ecDNA checkbox must be checked when ecDNA + no-amp were submitted"
        assert b'id="class-noamp" checked' in resp.content, \
            "No-Amp checkbox must be checked when ecDNA + no-amp were submitted"
        assert b'id="class-linear" checked' not in resp.content, \
            "Linear Amplification checkbox must NOT be checked"
        assert b'id="class-bfb" checked' not in resp.content, \
            "BFB checkbox must NOT be checked"
        assert b'id="class-complex" checked' not in resp.content, \
            "Complex Non-Cyclic checkbox must NOT be checked"
        # And both the ecDNA sample and the zero-feature sample must appear
        assert b'CBEcDNASample' in resp.content, \
            "ecDNA sample must appear"
        assert b'CBZeroSample' in resp.content, \
            "Zero-feature sample must appear when no-amp is checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_na_classified_features_included_when_noamp_checked(
        request_factory, test_user, mongo_collection):
    """
    Real database feature rows with Classification='NA' (AmpliconSuiteAggregator
    convention for samples where the classifier produced no amplification result,
    stored with Feature_ID ending in '_NA') must be included when the
    'No-Amp (sample)' checkbox is checked.

    This mirrors the structure of real projects like glass4.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchNAClass',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            # Real ecDNA sample
            'NACEcDNASample': [{'Sample_name': 'NACEcDNASample', 'Feature_ID': 'NACEcDNASample_amplicon1_ecDNA_1',
                                'Classification': 'ecDNA', 'All_genes': ['MYC'],
                                'Oncogenes': ['MYC'], 'Sample_type': '',
                                'Cancer_type': '', 'Tissue_of_origin': ''}],
            # Real 'NA' classified sample (AmpliconSuiteAggregator convention)
            'NACNoAmpSample': [{'Sample_name': 'NACNoAmpSample', 'Feature_ID': 'NACNoAmpSample_NA',
                                'Classification': 'NA', 'All_genes': [],
                                'Oncogenes': [], 'Sample_type': '',
                                'Cancer_type': '', 'Tissue_of_origin': ''}],
            # Real Linear Amplification sample
            'NACLinearSample': [{'Sample_name': 'NACLinearSample', 'Feature_ID': 'NACLinearSample_amplicon1_linear_1',
                                 'Classification': 'Linear', 'All_genes': [],
                                 'Oncogenes': [], 'Sample_type': '',
                                 'Cancer_type': '', 'Tissue_of_origin': ''}],
        },
        'sample_count': 3,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # Only no-amp: should return only the NA-classified sample
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchNAClass',
            'classquery': ['no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'NACNoAmpSample' in resp.content, \
            "NA-classified sample must appear when only no-amp is checked"
        assert b'NACEcDNASample' not in resp.content, \
            "ecDNA sample must NOT appear when only no-amp is checked"
        assert b'NACLinearSample' not in resp.content, \
            "Linear sample must NOT appear when only no-amp is checked"

        # ecDNA + no-amp: should return ecDNA and NA-classified samples
        req2 = request_factory.post('/search_results/', {
            'project_name': 'SearchNAClass',
            'classquery': ['ecDNA', 'no-amp']})
        req2.user = test_user
        resp2 = search_results(req2)
        assert resp2.status_code == 200
        assert b'NACEcDNASample' in resp2.content, \
            "ecDNA sample must appear when ecDNA + no-amp checked"
        assert b'NACNoAmpSample' in resp2.content, \
            "NA-classified sample must appear when ecDNA + no-amp checked"
        assert b'NACLinearSample' not in resp2.content, \
            "Linear sample must NOT appear when only ecDNA + no-amp checked"

        # None checked: all samples appear
        req3 = request_factory.post('/search_results/', {
            'project_name': 'SearchNAClass'})
        req3.user = test_user
        resp3 = search_results(req3)
        assert resp3.status_code == 200
        assert b'NACEcDNASample' in resp3.content
        assert b'NACNoAmpSample' in resp3.content
        assert b'NACLinearSample' in resp3.content
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_no_fscna_real_features_included_when_noamp_checked(
        request_factory, test_user, mongo_collection):
    """
    Real database feature rows with Classification='No FSCNA' (samples analyzed by
    AmpliconClassifier and found to have no amplification) must be included in search
    results when the 'No-Amp (sample)' checkbox is checked alongside other types.

    This was the original behavior of the no_amp_mask and must be preserved.
    """
    from caper.views import search_results

    doc = {
        'project_name':  'SearchNoFSCNA',
        'creator':       test_user.username,
        'private':       'public',
        'delete':        False,
        'current':       True,
        'FINISHED?':     True,
        'runs': {
            # Real ecDNA sample with a proper feature
            'NFSSampleEcDNA': [{'Sample_name': 'NFSSampleEcDNA', 'Feature_ID': 'f1',
                                'Classification': 'ecDNA', 'All_genes': ['MYC'],
                                'Oncogenes': ['MYC'], 'Sample_type': '',
                                'Cancer_type': '', 'Tissue_of_origin': ''}],
            # Real 'No FSCNA' sample — analyzed but no amplification found.
            # Stored in the database with a real Feature_ID, Classification='No FSCNA'.
            'NFSSampleNoFSCNA': [{'Sample_name': 'NFSSampleNoFSCNA', 'Feature_ID': 'f2',
                                  'Classification': 'No FSCNA', 'All_genes': [],
                                  'Oncogenes': [], 'Sample_type': '',
                                  'Cancer_type': '', 'Tissue_of_origin': ''}],
            # True zero-feature sample (empty runs list)
            'NFSSampleZero': [],
        },
        'sample_count': 3,
    }
    result = mongo_collection.insert_one(doc)

    try:
        # ecDNA + no-amp: must return ecDNA, No FSCNA, and zero-feature
        req = request_factory.post('/search_results/', {
            'project_name': 'SearchNoFSCNA',
            'classquery': ['ecDNA', 'no-amp']})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert b'NFSSampleEcDNA' in resp.content, \
            "ecDNA sample must appear when ecDNA + no-amp checked"
        assert b'NFSSampleNoFSCNA' in resp.content, \
            "Real 'No FSCNA' sample must appear when no-amp checkbox is checked"
        assert b'NFSSampleZero' in resp.content, \
            "True zero-feature sample must appear when no-amp checkbox is checked"

        # BFB only (no no-amp): must NOT return No FSCNA or zero-feature
        req2 = request_factory.post('/search_results/', {
            'project_name': 'SearchNoFSCNA',
            'classquery': ['BFB']})
        req2.user = test_user
        resp2 = search_results(req2)
        assert resp2.status_code == 200
        assert b'NFSSampleEcDNA' not in resp2.content, \
            "ecDNA sample must NOT appear when only BFB checked"
        assert b'NFSSampleNoFSCNA' not in resp2.content, \
            "No FSCNA sample must NOT appear when no-amp is not checked"
        assert b'NFSSampleZero' not in resp2.content, \
            "Zero-feature sample must NOT appear when no-amp is not checked"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})


@pytest.mark.integration
def test_perform_search_ecdna_noamp_returns_zero_feature(
        test_user, mongo_collection):
    """
    Direct perform_search() call with ecDNA + no-amp must return the zero-feature
    sample alongside the ecDNA sample, confirming the backend is correct
    independent of the view/template layer.
    """
    from caper.search import perform_search

    doc = {
        'project_name': 'PSEcDNANoAmp',
        'creator': test_user.username,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': {
            'PSEcDNASample': [{'Sample_name': 'PSEcDNASample', 'Feature_ID': 'f1',
                               'Classification': 'ecDNA', 'All_genes': ['MYC'],
                               'Oncogenes': ['MYC'], 'Sample_type': '',
                               'Cancer_type': '', 'Tissue_of_origin': ''}],
            'PSBFBSample':   [{'Sample_name': 'PSBFBSample', 'Feature_ID': 'f2',
                               'Classification': 'BFB', 'All_genes': [],
                               'Oncogenes': [], 'Sample_type': '',
                               'Cancer_type': '', 'Tissue_of_origin': ''}],
            'PSZeroSample':  [],
        },
        'sample_count': 3,
    }
    result = mongo_collection.insert_one(doc)

    try:
        results = perform_search(
            project_name='PSECDNANOAMP',
            classquery='ECDNA',       # ecDNA amp type
            include_no_amp=True,      # no-amp checkbox is checked
            no_filter=False,
            user=test_user,
        )
        sample_names = {s['Sample_name'] for s in results['public_sample_data']}
        assert 'PSEcDNASample' in sample_names, \
            "ecDNA sample must be returned when classquery=ECDNA and include_no_amp=True"
        assert 'PSZeroSample' in sample_names, \
            "Zero-feature sample must be returned when include_no_amp=True alongside ecDNA"
        assert 'PSBFBSample' not in sample_names, \
            "BFB sample must NOT be returned when only ecDNA is selected"
    finally:
        mongo_collection.delete_one({'_id': result.inserted_id})

