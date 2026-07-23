"""Regression tests for focal-amplification classification charts."""

from caper.StackedBarChart import StackedBarChart
from pathlib import Path


def test_stacked_bar_chart_renders_fan_classification():
    samples = [
        {
            "Sample_name": "fan-sample",
            "Classification": "FAN",
            "AA_amplicon_number": 1,
        },
        {
            "Sample_name": "legacy-sample",
            "Classification": "ecDNA",
            "AA_amplicon_number": 1,
        },
    ]

    html = StackedBarChart(samples, {"FAN": "rgb(255, 128, 0)"})

    assert "FAN" in html


def test_stacked_bar_chart_does_not_crash_on_future_classification():
    samples = [{
        "Sample_name": "future-sample",
        "Classification": "Future-classification",
        "AA_amplicon_number": 1,
    }]

    html = StackedBarChart(samples, {})

    assert "Future-classification" in html


def test_site_focal_amplification_palette_is_muted_and_colorblind_friendly():
    views_source = (
        Path(__file__).parents[1] / "caper" / "caper" / "views.py"
    ).read_text()

    expected_colors = {
        "ecDNA": "#B33A3A",
        "FAN": "#D87524",
        "BFB": "#A85A8A",
        "Complex non-cyclic": "#C49A32",
        "Linear amplification": "#A7ADB4",
        "Virus": "#287C8E",
    }
    for classification, color in expected_colors.items():
        assert f"'{classification}': '{color}'" in views_source


def test_site_statistics_templates_show_fan_counts():
    template_dir = Path(__file__).parents[1] / "caper" / "templates" / "pages"
    home_template = (template_dir / "index.html").read_text()
    admin_template = (template_dir / "admin_stats.html").read_text()

    assert "public_amplicon_classifications_count.FAN" in home_template
    assert "public_amplicon_classifications_count.FAN" in admin_template
    assert "all_private_amplicon_classifications_count.FAN" in admin_template
    assert "Focal amplification in neochromosome" in home_template
    assert "Focal amplification in neochromosome" in admin_template


def test_project_template_labels_coral_projects_and_versions():
    template = (
        Path(__file__).parents[1] / "caper" / "templates" / "pages" / "project.html"
    ).read_text()

    assert 'project.Reconstruction_tools' in template
    assert '>CoRAL</span>' in template
    assert 'project.CoRAL_version' in template
    assert 'version.CoRAL_version' in template


def test_project_lists_label_coral_projects():
    template_dir = Path(__file__).parents[1] / "caper" / "templates" / "pages"

    for filename in ("index.html", "profile.html", "admin_featured_projects.html",
                     "admin_stats.html"):
        template = (template_dir / filename).read_text()
        assert "project.Reconstruction_tools" in template
        assert ">CoRAL</span>" in template


def test_feature_type_labels_have_compact_explanations():
    template_root = Path(__file__).parents[1] / "caper" / "templates"
    searchbox = (template_root / "includes" / "searchbox.html").read_text()
    home = (template_root / "pages" / "index.html").read_text()
    admin = (template_root / "pages" / "admin_stats.html").read_text()

    for template in (searchbox, home, admin):
        assert "Focal amplification in neochromosome" in template
        assert "Breakage-fusion-bridge" in template
        assert "fa-question-circle" in template
        # the FAN help icon links out to the AmpliconClassifier FAN primer
        assert "AmpliconClassifier/blob/main/docs/fan_primer.md" in template


def test_admin_statistics_template_shows_coral_counts():
    template = (
        Path(__file__).parents[1] / "caper" / "templates" / "pages" /
        "admin_stats.html"
    ).read_text()

    assert "public_coral_project_count" in template
    assert "all_private_coral_project_count" in template
    assert "public_coral_sample_count" in template
    assert "all_private_coral_sample_count" in template
