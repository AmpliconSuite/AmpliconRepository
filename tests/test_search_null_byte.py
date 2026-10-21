"""
A NUL byte in any /search_results/ field must not 500.

Every field ends up inside a MongoDB ``$regex``, and the server refuses a
pattern with an embedded NUL ("Regular expression cannot contain an embedded
null byte"). No real search contains one; a vulnerability scanner sent 21 of
them to prod on 2026-10-20/21 and each became a 500 and an error email.

Prod serves search from the feature index and a laptop usually does not, and
the two paths build their regexes separately, so both are exercised.
"""

import pytest


@pytest.fixture(params=['scan', 'index'])
def search_path(request, monkeypatch):
    """Route perform_search down one path. The index path is forced usable:
    the server rejects the pattern before reading a row, so an empty or stale
    local index still reproduces the failure."""
    if request.param == 'index':
        from django.conf import settings
        monkeypatch.setattr(settings, 'USE_FEATURE_INDEX_SEARCH', True, raising=False)
        monkeypatch.setattr('caper.feature_index.index_is_usable', lambda: True)
        monkeypatch.setattr('caper.search_index.can_serve', lambda **kwargs: True)
    return request.param


FIELDS = [
    'genequery',
    'project_name',
    'metadata_sample_name',
    'metadata_sample_type',
    'metadata_cancer_tissue',
]


@pytest.mark.integration
@pytest.mark.parametrize('field', FIELDS)
def test_null_byte_in_text_field_is_not_a_500(request_factory, test_user, search_path, field):
    from caper.views import search_results

    req = request_factory.post('/search_results/', {field: 'MYC\x00'})
    req.user = test_user
    assert search_results(req).status_code == 200


@pytest.mark.integration
def test_null_byte_in_classification_is_not_a_500(request_factory, test_user, search_path):
    from caper.views import search_results

    req = request_factory.post('/search_results/', {'classquery': ['ecDNA\x00']})
    req.user = test_user
    assert search_results(req).status_code == 200


@pytest.mark.integration
def test_null_byte_is_dropped_not_the_term(request_factory, test_user, mongo_collection):
    """'Null\\x00Proj' searches for 'NullProj', not for nothing."""
    from caper.views import search_results

    doc = {'project_name': 'NullByteProj_One', 'creator': test_user.username,
           'private': 'public', 'delete': False, 'current': True,
           'FINISHED?': True, 'sample_count': 1,
           'runs': {'r': [{'Sample_name': 'S1', 'Features': []}]}}
    inserted = mongo_collection.insert_one(doc)
    try:
        req = request_factory.post('/search_results/', {'project_name': 'NullByte\x00Proj'})
        req.user = test_user
        resp = search_results(req)
        assert resp.status_code == 200
        assert 'NullByteProj_One' in resp.content.decode()
    finally:
        mongo_collection.delete_one({'_id': inserted.inserted_id})
