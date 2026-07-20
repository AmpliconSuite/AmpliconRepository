"""Regression tests for project-page template behavior."""

from pathlib import Path


TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "caper" / "templates" / "pages"
)


def test_loading_page_uses_requested_processing_message():
    source = (TEMPLATE_DIR / "loading.html").read_text()

    assert (
        "This project is being processed, please wait. "
        "The page will automatically refresh in 15 seconds ..."
    ) in source


def test_edit_project_drop_zone_prevents_browser_default_and_selects_files():
    source = (TEMPLATE_DIR / "edit_project.html").read_text()

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
