"""
Tests for the ``publication_url`` template filter.

``publication_link`` is free text -- the upload form asks for "a PMID or link
to a publication" -- so the filter has to turn whatever an uploader typed into
something safe to put in an ``href``.  Two properties matter and are pinned
here: the bare forms the form invites resolve to a real URL, and anything the
filter cannot resolve returns '' so the caller renders no link at all rather
than a link to an address nobody stored.
"""

import pytest

from caper.templatetags.custom_filters import publication_url


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
