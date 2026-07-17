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
