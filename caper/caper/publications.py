"""Resolving the free-text ``publication_link`` field.

The upload form asks for "a PMID or link to a publication", so the stored value
is whatever the uploader typed.  Two callers need to make sense of it: the
template filter that renders the link glyph on the home page, and the site
statistics that count how many distinct papers the repository covers.

It lives in its own module rather than in ``templatetags`` or ``utils`` because
both of those would be the wrong dependency for one of the callers: statistics
should not import a template library, and a template filter should not drag in
``utils``, which opens a Mongo connection at import time.
"""

from re import match, IGNORECASE

PUBMED_URL = 'https://pubmed.ncbi.nlm.nih.gov/{}/'
DOI_URL = 'https://doi.org/{}'


def publication_url(value):
    """Resolve a free-text publication reference into a URL, or '' if it cannot
    be resolved without guessing.

    Every value on the live site is currently a URL, but that is a habit rather
    than a guarantee, so the bare forms the form invites -- a PMID, a DOI -- are
    resolved too.  Anything else returns '' and the caller shows nothing: a
    breadcrumb pointing at a URL we invented is worse than no breadcrumb.  Only
    http(s) is accepted, so a stored ``javascript:`` value cannot become a link.
    """
    if not value:
        return ''

    text = value.strip()
    if not text:
        return ''

    lowered = text.lower()
    if lowered.startswith(('http://', 'https://')):
        return text
    if lowered.startswith('www.'):
        return 'https://' + text

    pmid = match(r'^(?:pmid\s*[:.]?\s*)?(\d{4,9})$', text, IGNORECASE)
    if pmid:
        return PUBMED_URL.format(pmid.group(1))

    doi = match(r'^(?:doi\s*[:.]?\s*)?(10\.\d{4,9}/\S+)$', text, IGNORECASE)
    if doi:
        return DOI_URL.format(doi.group(1))

    return ''


def publication_key(url):
    """Reduce a resolved URL to a value two references to the same paper share.

    Uploaders reach the same paper by different routes -- one stores a bare
    PMID, another pastes the PubMed URL, a third links the journal over http --
    so counting distinct papers means counting distinct keys, not distinct
    stored strings.  This normalizes only the parts that are genuinely
    case- and form-insensitive: the scheme, the host, and a trailing slash.
    """
    if not url:
        return ''

    key = url.strip().lower()
    for scheme in ('https://', 'http://'):
        if key.startswith(scheme):
            key = key[len(scheme):]
            break
    if key.startswith('www.'):
        key = key[4:]

    return key.rstrip('/')


def count_unique_publications(urls):
    """Number of distinct papers in a list of resolved publication URLs."""
    return len({publication_key(url) for url in urls if publication_key(url)})
