from re import sub
from django import template

register = template.Library()

@register.filter
def replace_urls(content):
    if not content:
        return content

    url_regex = r'(((https?://)|(www\.))[^\s]+)'
    return sub(url_regex, lambda x: '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>'.format(x.group(), x.group()), content)
