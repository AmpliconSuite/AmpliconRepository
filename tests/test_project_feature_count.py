"""Regression tests for per-sample feature totals on the project page."""

from types import SimpleNamespace

import pandas as pd
import pytest


@pytest.mark.integration
def test_project_page_counts_all_features_not_unique_classifications(
    monkeypatch, request_factory
):
    from caper import views

    project_id = "64b000000000000000000001"
    features = [
        {
            "Sample_name": "sample-a",
            "Oncogenes": [],
            "Classification": "ecDNA",
            "Feature_ID": f"feature-{index}",
        }
        for index in range(1, 4)
    ]
    project = {
        "_id": project_id,
        "linkid": project_id,
        "project_name": "feature-count-regression",
        "private": "public",
        "delete": False,
        "current": True,
        "FINISHED?": True,
        "project_members": [],
        "runs": {"sample_1": features},
    }

    monkeypatch.setattr(views, "get_one_project", lambda _project_id: project)
    monkeypatch.setattr(views, "validate_project", lambda value, _name: value)
    monkeypatch.setattr(views, "previous_versions", lambda _project: ([], None))
    monkeypatch.setattr(views, "set_project_edit_OK_flag", lambda *_args: None)
    monkeypatch.setattr(views, "initialize_ecDNA_context", lambda *_args: None)
    monkeypatch.setattr(views, "reference_genome_from_project", lambda *_args: "hg38")
    monkeypatch.setattr(
        views,
        "create_aggregate_df",
        lambda *_args: (pd.DataFrame(), "/tmp/unused-feature-count.csv"),
    )
    monkeypatch.setattr(views, "get_cached_chart", lambda *_args: "")
    monkeypatch.setattr(views, "session_visit", lambda *_args: (0, 0))
    monkeypatch.setattr(
        views,
        "collection_handle",
        SimpleNamespace(update_one=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        views,
        "render",
        lambda _request, _template, context: context,
    )

    request = request_factory.get(f"/project/{project_id}")
    context = views.project_page(request, project_id)

    assert context["sample_data"][0]["Features"] == 3
    assert context["sample_data"][0]["Classifications_counted"] == ["ecDNA (3)"]
