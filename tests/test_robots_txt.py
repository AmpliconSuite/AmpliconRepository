"""
Tests for the robots.txt view.

The view used to read ``settings.STATIC_ROOT``, which on the servers resolves
into a collectstatic output directory that the runtime bind-mount shadows and
that a source-only deploy never refreshes.  On production that copy was dated
Apr 2024, so edits to the tracked ``caper/static/robots.txt`` silently had no
effect.  These tests pin the view to the file that git actually deploys.
"""

import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED_ROBOTS = os.path.join(REPO_ROOT, 'caper', 'static', 'robots.txt')


@pytest.mark.integration
def test_robots_view_serves_the_tracked_file(request_factory):
    """What the view returns must match the file in the source tree."""
    from caper.views import robots

    response = robots(request_factory.get('/robots.txt'))
    assert response.status_code == 200
    assert response['Content-Type'] == 'text/plain'

    with open(TRACKED_ROBOTS) as fh:
        expected = fh.read()

    body = response.content.decode()
    assert body == expected, (
        "robots.txt view is not serving caper/static/robots.txt — it is most "
        "likely reading a stale collectstatic output directory instead."
    )


@pytest.mark.integration
def test_robots_disallows_bulk_downloads_but_not_pages(request_factory):
    """Crawlers should be able to index pages while being refused the data files."""
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    assert 'Disallow: /*/download' in body, \
        "download endpoints are not disallowed — bots can pull project data"

    # Sample and project pages must stay crawlable for search visibility.
    for page_path in ('Disallow: /project', 'Disallow: /\n', 'Disallow: /*/sample'):
        assert page_path not in body.split('User-agent: Amazonbot')[0], \
            f"{page_path!r} would block indexing of content pages for all crawlers"
