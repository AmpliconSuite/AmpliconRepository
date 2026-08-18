from re import sub
from django import template

from caper.publications import publication_url as resolve_publication_url

register = template.Library()

# The resolver itself lives in caper.publications: the site statistics count
# distinct publications with the same logic, and they cannot import a template
# library to get at it.
register.filter('publication_url', resolve_publication_url)

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

