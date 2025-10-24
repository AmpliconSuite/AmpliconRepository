from re import sub
from django import template

register = template.Library()

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

