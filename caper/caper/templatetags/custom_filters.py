from re import sub, match, IGNORECASE
from django import template

register = template.Library()


@register.filter
def publication_url(value):
    """Resolve a free-text ``publication_link`` into a URL, or '' if it cannot
    be resolved without guessing.

    The upload form asks for "a PMID or link to a publication", so the stored
    value is whatever the uploader typed.  Every value on the live site is
    currently a URL, but that is a habit rather than a guarantee, so the bare
    forms the form invites -- a PMID, a DOI -- are resolved too.  Anything
    else returns '' and the caller shows nothing: a breadcrumb pointing at a
    URL we invented is worse than no breadcrumb.  Only http(s) is accepted, so
    a stored ``javascript:`` value cannot become a link.
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
        return 'https://pubmed.ncbi.nlm.nih.gov/{}/'.format(pmid.group(1))

    doi = match(r'^(?:doi\s*[:.]?\s*)?(10\.\d{4,9}/\S+)$', text, IGNORECASE)
    if doi:
        return 'https://doi.org/' + doi.group(1)

    return ''

@register.filter
def replace_urls(content):
    if not content:
        return content

    url_regex = r'(((https?://)|(www\.))[^\s]+)'
    return sub(url_regex, lambda x: '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>'.format(x.group(), x.group()), content)

@register.filter
def sort_dict_by_value_desc(dictionary):
    """
    Sort a dictionary by its values in descending order.
    Returns a list of tuples (key, value) sorted by value.
    """
    if not dictionary:
        return []
    return sorted(dictionary.items(), key=lambda x: x[1], reverse=True)

@register.filter
def lookup(dictionary, key):
    """
    Look up a value in a dictionary by key.
    Returns empty string if key not found.
    """
    if not dictionary:
        return ''
    return dictionary.get(key, '')

