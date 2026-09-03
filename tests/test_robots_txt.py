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
    # Only the wildcard block governs "all crawlers", so isolate it rather than
    # splitting on whichever named agent happens to come first in the file.
    wildcard_block = _agent_block(body, '*')
    for page_path in ('Disallow: /project', 'Disallow: /\n', 'Disallow: /*/sample'):
        assert page_path not in wildcard_block, \
            f"{page_path!r} would block indexing of content pages for all crawlers"


@pytest.mark.integration
def test_robots_keeps_search_indexers_allowed(request_factory):
    """Blocking a search engine here would deindex the site."""
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    for indexer in ('Googlebot', 'bingbot', 'Applebot', 'DuckDuckBot', 'YandexBot'):
        assert _agent_block(body, indexer) is None, \
            f"{indexer} has its own robots.txt block — that risks deindexing the site"


@pytest.mark.integration
def test_robots_lets_ai_assistants_read_the_pages(request_factory):
    """Deliberate policy, not an oversight.

    We want an assistant asked about ecDNA or about a project hosted here to
    know this site and answer from it.  Blocking these would make the site
    invisible to exactly the tool people increasingly ask first.  Crawl volume
    is caper/middleware.py's problem now, not robots.txt's.
    """
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    for agent in ('GPTBot', 'ClaudeBot', 'PerplexityBot', 'CCBot', 'anthropic-ai',
                  'Google-Extended', 'Applebot-Extended', 'ChatGPT-User',
                  'Claude-User', 'OAI-SearchBot', 'cohere-ai'):
        assert _agent_block(body, agent) is None, \
            (f"{agent} is blocked — that removes this site from AI answers. If "
             f"this is deliberate, update the policy comment in robots.txt too.")


@pytest.mark.integration
def test_robots_still_blocks_extraction_only_crawlers(request_factory):
    """The ones that send no readers and feed no assistant stay out."""
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    for agent in ('AhrefsBot', 'SemrushBot', 'DataForSeoBot', 'MJ12bot',
                  'Amazonbot', 'Bytespider', 'PetalBot'):
        assert 'Disallow: /' in (_agent_block(body, agent) or ''), \
            f"{agent} should be disallowed"


@pytest.mark.integration
def test_allowed_crawlers_are_not_given_their_own_group(request_factory):
    """A group of its own would release a bot from the wildcard disallows.

    robots.txt agents obey only the single most specific matching group, so
    adding "User-agent: GPTBot / Allow: /" would silently let it into
    /*/download and /api/ -- the opposite of what such an edit intends.
    """
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith('user-agent:'):
            continue
        assert not stripped.lower().startswith('allow:'), \
            ("robots.txt has an Allow: rule. Allowed crawlers must simply be "
             "absent from this file so the wildcard block governs them.")


def _agent_block(body, agent):
    """Return the directives that apply to `agent`, or None if it has no block.

    Matching is exact on the agent token, so 'Applebot' does not match
    'Applebot-Extended'.
    """
    lines = body.splitlines()
    collecting = False
    block = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith('user-agent:'):
            named = stripped.split(':', 1)[1].strip()
            if collecting and named.lower() != agent.lower():
                break
            collecting = named.lower() == agent.lower()
            continue
        if collecting and stripped and not stripped.startswith('#'):
            block.append(stripped)
    return '\n'.join(block) + '\n' if collecting or block else None


# ---------------------------------------------------------------------------
# The development server's Disallow-everything variant
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_dev_disallow_all_file_is_off_by_default(request_factory):
    """Production must not start refusing crawlers because this shipped."""
    from caper.views import robots

    body = robots(request_factory.get('/robots.txt')).content.decode()

    assert _agent_block(body, '*').strip() != 'Disallow: /'


@pytest.mark.integration
def test_dev_robots_disallow_all_serves_the_dev_file(request_factory):
    """Stage two of removing dev from search results.

    Deliberately a separate flag from DEV_GATE_ENABLED rather than following
    it: the noindex header has to be seen by a crawler before the crawl is
    refused, and refusing the crawl first would strand the indexed URLs.  See
    caper/static/robots-dev.txt.
    """
    from django.test import override_settings

    from caper.views import robots

    with override_settings(DEV_ROBOTS_DISALLOW_ALL=True):
        body = robots(request_factory.get('/robots.txt')).content.decode()

    assert _agent_block(body, '*').strip() == 'Disallow: /'
    assert 'Disallow: /*/download' not in body, \
        'the dev variant should be the whole-site refusal, not the production file'
