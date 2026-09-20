"""``<link rel="canonical">`` for every rendered page.

Production answers on two hostnames (``www.`` and the apex), a project page
answers under every superseded ``linkid`` of its chain, and a query string
(``?display_all_chr=1``, a search) makes a fresh URL of the same page.  None
of those told a search engine which URL is the page, so it chose for itself:
Search Console listed 241 URLs under "Duplicate without user-selected
canonical" on 2026-09-20, and had crawled both hostnames.

The canonical is built from ``SITE_URL`` -- already the apex on production
and set per deployment -- so the host is a setting, never
``request.get_host()``, which is the thing being corrected.  The path is the
request path with the query string dropped.  Project and sample pages, which
are the pages that exist under several ids, override the path with the head
of the project's chain.

A deployment with the dev gate on emits none: it serves ``X-Robots-Tag:
noindex`` on every response, and a canonical alongside noindex is a
contradictory signal, not a stronger one.
"""

from django.conf import settings
from django.urls import reverse

from . import lineage


def canonical_host():
    """The scheme and host canonical URLs are built on, or ``''`` for none."""
    if getattr(settings, 'DEV_GATE_ENABLED', False):
        return ''
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/')


def canonical_url(path):
    """*path* (which must start with ``/``) as an absolute canonical URL, or
    ``None`` when this deployment emits no canonicals."""
    host = canonical_host()
    if not host:
        return None
    return host + path


def canonical_url_context(request):
    """Context processor: the default canonical for the page being rendered.

    A view whose page exists under several URLs puts its own ``CANONICAL_URL``
    in the render context, which takes precedence over this one.
    """
    if request.method not in ('GET', 'HEAD'):
        return {}
    url = canonical_url(request.path)
    return {'CANONICAL_URL': url} if url else {}


def canonical_project_id(collection, project):
    """The id a search engine should file this project under: the head of its
    chain, which is the project itself unless an older version is being viewed.

    One indexed read of the chain's pointers.  A document without pointers has
    no chain to look up and is its own head.
    """
    current = lineage.latest_version(collection, project, lineage.POINTER_PROJECTION)
    if current is None:
        return str(project['_id'])
    return str(current['_id'])


def project_canonical_url(collection, project):
    return canonical_url(reverse('project_page', args=[canonical_project_id(collection, project)]))


def sample_canonical_url(collection, project, sample_name):
    """The sample under the head of its project's chain.  The head may have
    lost the sample since (a reaggregation can drop one); a canonical that
    points at a 404 is ignored by the crawler, which is the right outcome for
    a page only reachable through a superseded id."""
    return canonical_url(reverse('sample_page',
                                 args=[canonical_project_id(collection, project), sample_name]))
