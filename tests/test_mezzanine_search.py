"""
Tests for /search/.

``mezzanine.urls`` mounts its CMS search there, and with the blog app not
installed it raised ImproperlyConfigured on every request: a 500 for anyone who
typed the URL (prod, 2026-10-20, from a path scanner). The site's own search is
/search_results/, so /search/ is shadowed with a plain 404.
"""

import pytest
from django.http import Http404


@pytest.mark.integration
def test_search_route_is_ours_not_mezzanines():
    """Our pattern must sit ahead of Mezzanine's include, or the 500 is back."""
    from django.urls import resolve
    from caper import views

    assert resolve('/search/').func is views.mezzanine_search


@pytest.mark.integration
def test_search_answers_404(request_factory):
    from caper.views import mezzanine_search

    with pytest.raises(Http404):
        mezzanine_search(request_factory.get('/search/', {'q': 'MYC'}))
