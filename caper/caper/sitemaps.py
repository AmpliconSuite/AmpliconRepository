"""
Sitemap classes for the site.

Mezzanine wires its own ``sitemap.xml`` inside ``mezzanine.urls``.  That
include carries a catch-all pattern and therefore sits last in ``urls.py``, so
registering the route below ahead of it is what makes this class win.
"""

from mezzanine.core.sitemaps import DisplayableSitemap


class HttpsDisplayableSitemap(DisplayableSitemap):
    """
    Mezzanine's sitemap, pinned to https.

    Django 4.0 leaves ``Sitemap.protocol`` as ``None`` and ``get_protocol()``
    then falls back to ``"http"`` -- with a RemovedInDjango50Warning saying the
    default flips to ``"https"`` in Django 5.0.  Mezzanine never sets it, so
    this HTTPS-only site was advertising ``http://ampliconrepository.org/`` in
    its own sitemap: a scheme the ALB immediately redirects away from, which
    costs a crawler a wasted round trip and reads as a misconfiguration.

    Setting it explicitly is correct under both Django versions and silences
    the deprecation warning.

    Scope note: this sitemap lists Mezzanine ``Displayable`` objects -- the CMS
    pages -- and nothing else.  Project and sample pages are deliberately absent
    and should stay that way.  Project URLs are not stable identities: a
    reaggregation mints a new ``/project/<linkid>`` rather than updating the old
    one, so any project URL written here goes stale the next time a project is
    reaggregated.  Sample pages are worse: there are ~19,400 of them, they are
    the most expensive page the site renders, and ``middleware.py`` caps
    concurrent *unreferred* sample-page renders at 2 precisely because that is
    the crawler traffic shape.  Listing them would invite exactly that shape and
    earn mass 503s from the limiter.  Both classes are already reachable by
    ordinary link-following from the home page, so their absence here costs no
    discoverability.
    """

    protocol = "https"
