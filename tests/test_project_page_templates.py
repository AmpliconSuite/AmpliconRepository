"""Regression tests for project-page template behavior."""

from pathlib import Path

from django.template.loader import render_to_string


TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "caper" / "templates" / "pages"
)


def test_loading_page_uses_requested_processing_message():
    source = (TEMPLATE_DIR / "loading.html").read_text()

    assert (
        "This project is being processed, please wait. "
        "The page will automatically refresh in 15 seconds ..."
    ) in source


def test_project_archive_inputs_accept_zip_and_edit_drop_zone_selects_files():
    sources = [
        (TEMPLATE_DIR / template_name).read_text()
        for template_name in ("create_project.html", "edit_project.html")
    ]

    for source in sources:
        assert 'accept=".tar.gz,.zip"' in source
        assert 'const validFileExtensions = [".tar.gz", ".zip"];' in source
        assert "validFileExtensions.some(extension =>" in source
        assert "Please select .tar.gz or .zip files only." in source
        assert (
            'href="https://docs.ampliconrepository.org/en/latest/getting-started/"'
            in source
        )

    source = sources[1]

    assert "fileDropArea.addEventListener('dragover'" in source
    assert "fileDropArea.addEventListener('drop'" in source
    assert "event.preventDefault();" in source
    assert "input.files = event.dataTransfer.files;" in source
    assert (
        "input.dispatchEvent(new Event('change', { bubbles: true }));" in source
    )


def test_sample_amplicon_table_has_continuation_footer():
    source = (TEMPLATE_DIR / "sample.html").read_text()

    assert 'id="amplicon-continuation"' in source
    assert 'id="amplicon-continuation-summary"' in source
    assert 'id="amplicon-next-page"' in source
    assert 'aria-live="polite"' in source
    assert 'aria-controls="myTable2"' in source
    assert "table.page.info()" in source
    assert "table.page('next').draw('page')" in source
    assert "remaining > 0" in source
    assert source.index('id="amplicon-next-page"') < source.index(
        'id="amplicon-continuation-summary"'
    )


def test_home_project_descriptions_expand_inline_and_remain_searchable():
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert source.count(
        "{% include 'includes/project_description_cell.html' %}"
    ) == 3
    assert "$('#unifiedProjectTable').on(" in source
    assert "'.project-description-toggle'" in source
    assert "button.attr('aria-expanded'" in source
    assert "'aria-label'," in source
    # The toggle is the page's link blue and its hover, both taken from the
    # home page palette rather than from Bootstrap's defaults.
    assert "color: #1f66d0;" in source
    assert "color: #12489b;" in source
    assert "color: #386f9d;" not in source

    description_cell = (
        TEMPLATE_DIR.parent / "includes" / "project_description_cell.html"
    ).read_text()
    assert 'data-search="{{ project.description }}"' in description_cell
    assert "project.description|truncatechars:100" in description_cell
    assert "{{ project.description }}" in description_cell
    assert 'class="project-description-toggle"' in description_cell
    assert 'type="button"' in description_cell
    assert 'aria-expanded="false"' in description_cell
    assert 'data-project-name="{{ project.project_name }}"' in description_cell
    assert ">More<" in description_cell


def test_home_project_rows_distinguish_featured_from_public_and_private():
    """Featured and private rows are tagged; a plain public row is the default.

    The redesign dropped the "Public" badge -- a tag on two thirds of the rows
    is not information -- so what has to hold is that the tags which remain are
    told apart by colour rather than only by their text, and that a featured row
    is still marked when its tag scrolls out of a narrow viewport.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert '<span class="home-chip home-chip-featured">Featured</span>' in source
    assert '<span class="home-chip home-chip-private">Private</span>' in source
    assert '<span class="badge badge-primary">Public</span>' not in source

    # Distinct colours, not just distinct labels.
    assert ".home-chip-featured { background: #28a745; color: #fff; }" in source
    assert ".home-chip-private  { background: #f0f1f3; color: #5a5d63; }" in source

    # The featured rule on the row itself, so the row stays marked even where
    # the chip is off-screen.
    assert "tr.home-featured td:first-child" in source
    assert "box-shadow: inset 3px 0 0 #28a745;" in source


def test_home_project_description_toggle_only_renders_for_long_text():
    long_description = "A" * 101
    long_html = render_to_string(
        "includes/project_description_cell.html",
        {"project": {"description": long_description, "project_name": "Long"}},
    )
    short_html = render_to_string(
        "includes/project_description_cell.html",
        {"project": {"description": "A" * 100, "project_name": "Short"}},
    )

    assert long_description in long_html
    assert "project-description-toggle" in long_html
    assert "project-description-toggle" not in short_html


def test_home_figure_disclosures_point_at_panels_that_exist():
    """Every figure that offers a breakdown must open one.

    The buttons and the panels are matched by hand-written ids, and a typo in
    either produces a caret that opens nothing -- which looks like a broken
    page but throws no error, so nothing else would catch it.
    """
    import re

    source = (TEMPLATE_DIR / "index.html").read_text()

    panels = set(re.findall(r'class="home-breakdown" id="([\w-]+)"', source))
    targets = set(re.findall(r'data-panel="([\w-]+)"', source))
    controls = set(re.findall(r'aria-controls="([\w-]+)"', source))

    assert targets, "no figure disclosures found"
    assert targets <= panels, f"buttons point at missing panels: {targets - panels}"
    assert panels <= targets, f"panels no button opens: {panels - targets}"
    # A screen reader needs the same relationship the click handler uses.
    assert targets <= controls


def test_home_table_pager_is_moved_out_of_the_scrolling_wrapper():
    """The row count and the pager belong together, and outside the scroller.

    Inside .home-table-scroll they scroll sideways out of view on a narrow
    screen, which is most of why the table looked like it had no more rows.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert 'id="home-table-foot"' in source
    assert "$('#unifiedProjectTable_info, #unifiedProjectTable_paginate')" in source
    assert ".detach().appendTo('#home-table-foot')" in source
    # The count names its unit and the pager is arrowed, so the footer reads as
    # a control rather than as a caption.
    assert 'info: "Showing _START_ to _END_ of <b>_TOTAL_</b> projects"' in source
    assert 'next: "Next \\u2192"' in source

    foot_marker = source.index('id="home-table-foot"')
    scroll_close = source.index('id="unifiedProjectTable"')
    assert foot_marker > scroll_close
