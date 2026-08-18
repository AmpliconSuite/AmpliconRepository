"""
Tests for resolving the free-text ``publication_link`` field.

``publication_link`` is free text -- the upload form asks for "a PMID or link
to a publication" -- so the resolver has to turn whatever an uploader typed into
something safe to put in an ``href``.  Two properties matter and are pinned
here: the bare forms the form invites resolve to a real URL, and anything the
resolver cannot resolve returns '' so the caller renders no link at all rather
than a link to an address nobody stored.

``count_unique_publications`` is the other half: the home page reports how many
distinct papers the repository covers, and uploaders reach the same paper by
different routes, so equal papers have to count once however they were typed.
"""

import pytest

from caper.publications import count_unique_publications, publication_url
from caper.templatetags.custom_filters import register


@pytest.mark.parametrize('stored, expected', [
    # Absolute URLs pass through untouched; this is every value on the live
    # site today.
    ('https://pubmed.ncbi.nlm.nih.gov/39402156/',
     'https://pubmed.ncbi.nlm.nih.gov/39402156/'),
    ('http://example.org/paper', 'http://example.org/paper'),
    ('  https://doi.org/10.1158/2159-8290.CD-24-1532  ',
     'https://doi.org/10.1158/2159-8290.CD-24-1532'),
    # A scheme-less host is a link an uploader pasted out of a browser bar.
    ('www.nature.com/articles/s41586-024-07802-5',
     'https://www.nature.com/articles/s41586-024-07802-5'),
    # The bare forms the form's placeholder asks for.
    ('39402156', 'https://pubmed.ncbi.nlm.nih.gov/39402156/'),
    ('PMID: 39402156', 'https://pubmed.ncbi.nlm.nih.gov/39402156/'),
    ('pmid 12345678', 'https://pubmed.ncbi.nlm.nih.gov/12345678/'),
    ('10.1158/2159-8290.CD-24-1532',
     'https://doi.org/10.1158/2159-8290.CD-24-1532'),
    ('doi:10.1038/s41586-021-04116-8',
     'https://doi.org/10.1038/s41586-021-04116-8'),
])
def test_resolvable_values_become_urls(stored, expected):
    assert publication_url(stored) == expected


@pytest.mark.parametrize('stored', [
    '',
    '   ',
    None,
    'manuscript in preparation',
    'Smith et al. 2024',
    '123',                    # too short to be a PMID
    '12345678901234',         # too long to be a PMID
    'ftp://files.example.org/paper.pdf',
])
def test_unresolvable_values_yield_no_link(stored):
    """A guessed destination is worse than no breadcrumb at all."""
    assert publication_url(stored) == ''


@pytest.mark.parametrize('stored', [
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
])
def test_script_schemes_never_become_links(stored):
    """Only http(s) is accepted, so a stored script URL cannot be clicked."""
    assert publication_url(stored) == ''


def test_the_template_filter_is_the_same_resolver():
    """The home page glyph and the site statistics must agree on what resolves."""
    assert register.filters['publication_url'] is publication_url


@pytest.mark.parametrize('stored_values, expected', [
    # The same paper reached three ways: a bare PMID, the PubMed URL, and the
    # PubMed URL without its trailing slash.
    (['39402156',
      'https://pubmed.ncbi.nlm.nih.gov/39402156/',
      'https://pubmed.ncbi.nlm.nih.gov/39402156'], 1),
    # Scheme and www. are not part of a paper's identity.
    (['http://www.nature.com/articles/s41586-019-1763-5',
      'https://nature.com/articles/s41586-019-1763-5'], 1),
    # A DOI typed bare and typed as a doi.org URL.
    (['10.1038/s41588-024-01949-7',
      'https://doi.org/10.1038/s41588-024-01949-7'], 1),
    # Different papers stay different.
    (['39402156', '31554527', '10.1038/s41588-024-01949-7'], 3),
    # Values that resolve to nothing contribute nothing.
    (['manuscript in preparation', '', 'Smith et al. 2024'], 0),
    ([], 0),
])
def test_unique_publication_counting(stored_values, expected):
    resolved = [publication_url(value) for value in stored_values]
    assert count_unique_publications(resolved) == expected


# ---------------------------------------------------------------------------
# The project page line
# ---------------------------------------------------------------------------
# The home page has had the resolver behind its glyph for a while; the project
# page had not, and rendered the stored text through replace_urls, which only
# linkifies what already looks like a URL.  A project whose publication was
# recorded as a PMID therefore showed a bare number and no way to reach the
# paper.

def _publication_line(stored):
    from django.template.loader import render_to_string

    return render_to_string(
        'includes/project_publication_line.html',
        {'project': {'publication_link': stored}},
    )


@pytest.mark.parametrize('stored, expected_url', [
    ('39402156', 'https://pubmed.ncbi.nlm.nih.gov/39402156/'),
    ('PMID: 39402156', 'https://pubmed.ncbi.nlm.nih.gov/39402156/'),
    ('10.1158/2159-8290.CD-24-1532',
     'https://doi.org/10.1158/2159-8290.CD-24-1532'),
    ('www.nature.com/articles/s41586-024-07802-5',
     'https://www.nature.com/articles/s41586-024-07802-5'),
])
def test_project_page_links_a_reference_that_is_not_already_a_url(
        stored, expected_url):
    """The address is shown as well as linked: a reader who stored a PMID gets
    to see which paper it is without following the link."""
    html = _publication_line(stored)

    assert f'href="{expected_url}"' in html
    assert f'>{expected_url}</a>' in html


def test_project_page_leaves_a_stored_url_reading_as_it_was_stored():
    html = _publication_line('https://pubmed.ncbi.nlm.nih.gov/39402156/')

    assert 'href="https://pubmed.ncbi.nlm.nih.gov/39402156/"' in html
    assert html.count('<a ') == 1


def test_project_page_keeps_free_text_it_cannot_resolve():
    """Free text is still worth showing -- and a URL buried in it is still
    worth linking, which is what the old rendering did well."""
    html = _publication_line('Smith et al. 2024, https://example.org/paper')

    assert 'Smith et al. 2024' in html
    assert 'href="https://example.org/paper"' in html


def test_project_page_omits_the_line_when_nothing_is_stored():
    """A label with nothing after it is not information."""
    assert 'Publication link' not in _publication_line('')
