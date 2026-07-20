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


def test_home_project_descriptions_expand_inline_and_remain_searchable():
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert source.count(
        "{% include 'includes/project_description_cell.html' %}"
    ) == 3
    assert "$('#unifiedProjectTable').on(" in source
    assert "'.project-description-toggle'" in source
    assert "button.attr('aria-expanded'" in source
    assert "'aria-label'," in source
    assert "color: #007bff;" in source
    assert "color: #0056b3;" in source
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


def test_home_project_type_badge_colors_distinguish_featured_and_public():
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert '<span class="badge badge-success">Featured</span>' in source
    assert '<span class="badge badge-primary">Public</span>' in source
    assert '<span class="badge badge-primary">Featured</span>' not in source
    assert '<span class="badge badge-success">Public</span>' not in source


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
