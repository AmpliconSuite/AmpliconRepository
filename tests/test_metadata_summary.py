"""
Unit tests for the project page's metadata summary: coverage counts, the cancer
type breakdown, and the search links generated from it.

These are all pure functions over a project dict, so nothing here needs Mongo.
"""

import pytest


def _project(name, samples):
    """Build a project dict shaped like the one the summary helpers read.

    ``samples`` maps a sample name to its metadata dict, or to None for a sample
    with no uploaded metadata.
    """
    return {
        'project_name': name,
        'runs': {
            sample_name: [{
                'Sample_name': sample_name,
                'Feature_ID': sample_name + '_1',
                'extra_metadata_from_csv': metadata,
            }]
            for sample_name, metadata in samples.items()
        },
    }


# ---------------------------------------------------------------------------
# metadata_coverage
# ---------------------------------------------------------------------------

class TestMetadataCoverage:

    def test_counts_covered_and_total(self):
        from caper.extra_metadata import metadata_coverage
        project = _project('P', {
            'S1': {'cancer_type': 'Lung'},
            'S2': {'cancer_type': 'Lung'},
            'S3': None,
        })
        assert metadata_coverage(project) == {'total': 3, 'covered': 2}

    def test_no_metadata_at_all(self):
        from caper.extra_metadata import metadata_coverage
        project = _project('P', {'S1': None, 'S2': None})
        assert metadata_coverage(project) == {'total': 2, 'covered': 0}

    def test_empty_project(self):
        from caper.extra_metadata import metadata_coverage
        assert metadata_coverage({'runs': {}}) == {'total': 0, 'covered': 0}


# ---------------------------------------------------------------------------
# summarize_project_metadata
# ---------------------------------------------------------------------------

class TestSummarizeProjectMetadata:

    def test_unavailable_when_nothing_is_covered(self):
        from caper.extra_metadata import summarize_project_metadata
        summary = summarize_project_metadata(_project('P', {'S1': None}))
        assert summary['available'] is False
        assert summary['total_samples'] == 1
        assert summary['samples_with_metadata'] == 0

    def test_counts_by_cancer_type_largest_first(self):
        from caper.extra_metadata import summarize_project_metadata
        project = _project('P', {
            'S1': {'cancer_type': 'Lung'},
            'S2': {'cancer_type': 'Lung'},
            'S3': {'cancer_type': 'Breast'},
            'S4': None,
        })
        summary = summarize_project_metadata(project)
        assert summary['available'] is True
        assert summary['total_samples'] == 4
        assert summary['samples_with_metadata'] == 3
        assert summary['cancer_types'] == [
            {'name': 'Lung', 'count': 2},
            {'name': 'Breast', 'count': 1},
        ]

    def test_equal_counts_break_ties_alphabetically(self):
        """Stable order matters — the pie must not reshuffle between page loads."""
        from caper.extra_metadata import summarize_project_metadata
        project = _project('P', {
            'S1': {'cancer_type': 'Zebra'},
            'S2': {'cancer_type': 'Alpha'},
        })
        names = [e['name'] for e
                 in summarize_project_metadata(project)['cancer_types']]
        assert names == ['Alpha', 'Zebra']

    def test_missing_cancer_type_falls_into_unspecified_bucket(self):
        from caper.extra_metadata import (summarize_project_metadata,
                                          UNSPECIFIED_CATEGORY)
        project = _project('P', {
            'S1': {'sample_type': 'cell line'},
            'S2': {'cancer_type': 'Lung'},
        })
        summary = summarize_project_metadata(project)
        assert {e['name'] for e in summary['cancer_types']} == {
            'Lung', UNSPECIFIED_CATEGORY}
        assert summary['has_cancer_type'] is True

    def test_has_cancer_type_false_when_only_unspecified(self):
        from caper.extra_metadata import summarize_project_metadata
        project = _project('P', {'S1': {'sample_type': 'cell line'}})
        assert summarize_project_metadata(project)['has_cancer_type'] is False


# ---------------------------------------------------------------------------
# annotate_metadata_summary_search_links
# ---------------------------------------------------------------------------

class TestAnnotateSearchLinks:
    """
    Every cancer type the popup shows becomes a search for that exact type
    within that exact project, so both names go out quoted.
    """

    def _annotate(self, project_name, samples):
        from caper.extra_metadata import summarize_project_metadata
        from caper.views import annotate_metadata_summary_search_links
        project = _project(project_name, samples)
        summary = summarize_project_metadata(project)
        annotate_metadata_summary_search_links(summary, project)
        return summary

    def test_names_are_quoted(self):
        summary = self._annotate('PCAWG filtered', {'S1': {'cancer_type': 'Lung'}})
        assert summary['search_project_name'] == '"PCAWG filtered"'
        entry = summary['cancer_types'][0]
        assert entry['searchable'] is True
        assert entry['search_term'] == '"Lung"'

    def test_operator_in_name_is_still_searchable(self):
        """Before quoting existed, '&' in a name meant no link at all."""
        summary = self._annotate('Head & Neck cohort',
                                 {'S1': {'cancer_type': 'Lung|Pleura'}})
        assert summary['search_project_name'] == '"Head & Neck cohort"'
        assert summary['cancer_types'][0]['search_term'] == '"Lung|Pleura"'
        assert summary['cancer_types'][0]['searchable'] is True

    def test_double_quote_in_project_name_disables_all_links(self):
        """A quote cannot be escaped, so no row gets a link rather than a wrong one."""
        summary = self._annotate('The "Big" Study', {'S1': {'cancer_type': 'Lung'}})
        assert summary['search_project_name'] == ''
        assert summary['cancer_types'][0]['searchable'] is False
        assert summary['cancer_types'][0]['search_term'] == ''

    def test_double_quote_in_type_name_disables_that_row_only(self):
        summary = self._annotate('P', {
            'S1': {'cancer_type': 'Lung'},
            'S2': {'cancer_type': 'a "quoted" type'},
        })
        by_name = {e['name']: e for e in summary['cancer_types']}
        assert by_name['Lung']['searchable'] is True
        assert by_name['a "quoted" type']['searchable'] is False

    def test_unspecified_bucket_is_never_searchable(self):
        """
        'Not specified' is a label this page invents for samples with no cancer
        type; no stored value contains it, so a link would return nothing.
        """
        from caper.extra_metadata import UNSPECIFIED_CATEGORY
        summary = self._annotate('P', {
            'S1': {'sample_type': 'cell line'},
            'S2': {'cancer_type': 'Lung'},
        })
        by_name = {e['name']: e for e in summary['cancer_types']}
        assert by_name[UNSPECIFIED_CATEGORY]['searchable'] is False
        assert by_name['Lung']['searchable'] is True

    def test_unavailable_summary_is_left_alone(self):
        summary = self._annotate('P', {'S1': None})
        assert 'search_project_name' not in summary
