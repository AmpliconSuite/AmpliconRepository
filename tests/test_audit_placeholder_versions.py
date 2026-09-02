"""The audit page must not read a placeholder as a disagreement.

Audit events written before 2026-08-30 stored whatever the upload form held for
the tool versions, and that form defaults to ``NA``.  The real versions are
detected from the uploaded data afterwards and written to the project document.
So a pre-fix entry says ``NA`` where the document says ``1.3.r2``, and the page
reported that as a mismatch -- which is what sent someone looking at PCAWG on
dev for a fault that was not there.

Only one value was ever recorded.  A comparison needs two.
"""

import pytest

from caper.views_admin import (
    AUDIT_STATUS_CHOICES,
    _audit_status_counts,
    _run_audit_checks,
)


def _check(result, field):
    return next(c for c in result['checks'] if c['field'] == field)


def test_a_placeholder_in_the_log_is_not_a_mismatch():
    """The shape that produced the PCAWG report on dev."""
    entry = {'AA_version': 'NA', 'AC_version': 'NA', 'ASP_version': 'NA',
             'sample_count': 2095}
    project = {'AA_version': '1.3.r2', 'AC_version': '1.3.1',
               'ASP_version': 'NA', 'sample_count': 2095}

    result = _run_audit_checks(project, entry)

    assert result['any_mismatch'] is False, (
        'NA is the form placeholder, not a recorded version; there is nothing '
        'for it to disagree with')
    assert result['status'] == 'not_recorded'
    assert result['not_recorded'] == ['AA Version', 'AC Version']

    aa = _check(result, 'AA Version')
    assert aa['not_recorded'] is True
    assert aa['missing'] is True
    assert aa['log_value'] == 'NA', (
        'the stored placeholder stays on screen -- blanking it hides the one '
        'thing that explains why nothing was compared')
    assert aa['live_value'] == '1.3.r2'


def test_two_placeholders_still_agree():
    """Both sides saying "not provided" is agreement, not a warning.

    Demoting this would turn a page of green into a page of yellow and tell an
    admin nothing they did not already know.
    """
    result = _run_audit_checks({'ASP_version': 'NA'}, {'ASP_version': 'NA'})
    asp = _check(result, 'ASP Version')
    assert asp['match'] is True
    assert asp['not_recorded'] is False


def test_a_document_that_lost_a_recorded_version_is_still_a_mismatch():
    """The reverse direction is a real finding and must stay red.

    The log holds a version somebody's upload actually produced and the
    document now says ``NA``.  Something removed it, and that is exactly the
    kind of thing this page exists to surface.
    """
    result = _run_audit_checks({'AA_version': 'NA'}, {'AA_version': '1.3.r2'})
    aa = _check(result, 'AA Version')
    assert aa['match'] is False
    assert aa['not_recorded'] is False
    assert aa['missing'] is False
    assert result['any_mismatch'] is True
    assert result['status'] == 'mismatch'


def test_two_real_versions_that_differ_are_still_a_mismatch():
    result = _run_audit_checks({'AA_version': '1.3.r3'}, {'AA_version': '1.3.r2'})
    assert result['any_mismatch'] is True
    assert result['status'] == 'mismatch'


@pytest.mark.parametrize('placeholder', ['NA', 'na', 'n/a', ' NA ', 'Not Provided'])
def test_placeholder_spellings(placeholder):
    """The form's default has been written several ways over the years."""
    result = _run_audit_checks({'AA_version': '1.3.r2'},
                               {'AA_version': placeholder})
    assert _check(result, 'AA Version')['not_recorded'] is True


def test_only_version_fields_take_placeholders():
    """A sample count of NA is a different problem and must not be excused."""
    result = _run_audit_checks({'sample_count': 40},
                               {'sample_count': 'NA'})
    count = _check(result, 'Sample Count')
    assert count['not_recorded'] is False
    assert count['match'] is False
    assert result['any_mismatch'] is True


def test_a_real_mismatch_outranks_a_placeholder_in_the_same_entry():
    """Status names the worst thing found, not the most common one."""
    result = _run_audit_checks(
        {'AA_version': '1.3.r2', 'sample_count': 41},
        {'AA_version': 'NA', 'sample_count': 40})
    assert result['status'] == 'mismatch'


def test_the_filter_offers_only_states_something_is_in():
    """A filter choice that hides every row is a small trap."""
    projects = [{'validation_status': 'mismatch'},
                {'validation_status': 'not_recorded'},
                {'validation_status': 'not_recorded'}]

    counts = _audit_status_counts(projects)

    assert [c['key'] for c in counts] == ['mismatch', 'not_recorded']
    assert [c['count'] for c in counts] == [1, 2]
    assert all(c['count'] > 0 for c in counts)


def test_every_status_the_page_can_produce_is_offered_by_the_filter():
    """A state with no choice is a state you cannot filter down to."""
    import inspect
    from caper import views_admin

    source = inspect.getsource(views_admin._get_audit_log_context)
    source += inspect.getsource(views_admin._run_audit_checks)

    offered = {key for key, _ in AUDIT_STATUS_CHOICES}
    for status in ('pass', 'mismatch', 'not_recorded', 'missing_data',
                   'no_log', 'lifecycle_only', 'reconstructed_only', 'error'):
        assert f"'{status}'" in source, (
            f'{status} is no longer produced; drop it from AUDIT_STATUS_CHOICES')
        assert status in offered, (
            f'{status} can appear on a row but the filter does not offer it')


# ---------------------------------------------------------------------------
# Two records that both say "nothing" agree
#
# Reported from dev 2026-09-02: an empty project was badged "⚠️ Partial" for
# having no payload on either side. Nothing about it is partial -- the log and
# the document make the same statement, which is the same reason two NA
# placeholders have always read as a match.
# ---------------------------------------------------------------------------

def test_an_empty_project_is_a_pass_not_partial():
    """'GBM39 with metadata' on dev: 0 samples, no s3_uri, amber for it."""
    project = {'sample_count': 0}
    entry = {'sample_count': 0}          # no s3_uri, so no size on either side

    result = _run_audit_checks(project, entry)

    assert result['status'] == 'pass', [
        (c['field'], c['log_value'], c['live_value'], c['missing'])
        for c in result['checks']]


def test_zero_equals_zero():
    """A count of 0 is a recorded value, not an absent one."""
    result = _run_audit_checks({'sample_count': 0}, {'sample_count': 0})

    count = _check(result, 'Sample Count')
    assert count['match'] and not count['missing']
    assert count['log_value'] == '0' and count['live_value'] == '0'


def test_zero_against_a_real_count_is_still_a_mismatch():
    """The guard on the test above: 0 must not become a synonym for absent."""
    result = _run_audit_checks({'sample_count': 7}, {'sample_count': 0})

    assert result['status'] == 'mismatch'


def test_a_placeholder_against_an_absent_field_agrees():
    """'siavash test' on dev: log 'NA', document has no AA_version at all.

    Both say no version was recorded. Reporting that as incomplete data sent a
    reader looking for a fault that is not there.
    """
    result = _run_audit_checks({'sample_count': 4}, {'AA_version': 'NA',
                                                     'sample_count': 4})

    aa = _check(result, 'AA Version')
    assert aa['match'] and not aa['missing'] and not aa['not_recorded']


def test_a_recorded_value_against_an_absent_one_is_still_reported():
    """Unchanged, and the reason 'both empty' is scoped to *both*.

    The document losing a version the log holds is a real finding and must not
    be swept up by the agreement rule.
    """
    result = _run_audit_checks({'sample_count': 4},
                               {'AA_version': '1.3.r2', 'sample_count': 4})

    aa = _check(result, 'AA Version')
    assert not aa['match']
    assert result['status'] in ('mismatch', 'missing_data')


def test_the_filter_names_the_state_a_reader_sees():
    """'Lifecycle only' named the mechanism; the badge has to name the meaning.

    Both dev projects in this state carry `create` and `edit_new_version`
    entries that the events table shows -- they are excluded only because the
    backfill wrote them -- so "no payload entry" read as a contradiction.
    """
    labels = dict(AUDIT_STATUS_CHOICES)
    assert labels['lifecycle_only'] == 'No payload logged'
