def test_get_tool_versions_accepts_aggregator_7_names():
    from caper.views import get_tool_versions

    project = {}
    runs = {
        'sample_1': [{
            'AmpliconArchitect version': '1.3.r2',
            'AmpliconClassifier version': '2.0.0',
            'AmpliconSuite-pipeline version': '1.3.5',
            'Reconstruction tool': 'AmpliconArchitect',
        }],
    }

    get_tool_versions(project, runs)

    assert project['AA_version'] == '1.3.r2'
    assert project['AC_version'] == '2.0.0'
    assert project['ASP_version'] == '1.3.5'
    assert project['Reconstruction_tools'] == 'AmpliconArchitect'


def test_get_tool_versions_retains_legacy_names():
    from caper.views import get_tool_versions

    project = {}
    runs = {
        'sample_1': [{
            'AA version': '1.2.3',
            'AC version': '1.0.1',
            'AS-p version': '1.3.1',
        }],
    }

    get_tool_versions(project, runs)

    assert project['AA_version'] == '1.2.3'
    assert project['AC_version'] == '1.0.1'
    assert project['ASP_version'] == '1.3.1'


def test_update_form_supports_coral_version():
    from caper.forms import UpdateForm

    form = UpdateForm()

    assert form.fields['CoRAL_version'].label == 'CoRAL version(s)'


def test_classify_ac_version_current():
    from caper.utils import classify_ac_version, AC_VERSION_CURRENT

    for value in ('2.0.0', '2.1.3', '2', '3.0', 'v2.0.0'):
        assert classify_ac_version(value) == AC_VERSION_CURRENT, value


def test_classify_ac_version_outdated():
    from caper.utils import classify_ac_version, AC_VERSION_OUTDATED

    for value in ('1.1.2', '0.4.9', '1.9.9', 'v1.1.0'):
        assert classify_ac_version(value) == AC_VERSION_OUTDATED, value


def test_classify_ac_version_multiple_versions_flags_oldest():
    from caper.utils import (
        classify_ac_version, AC_VERSION_OUTDATED, AC_VERSION_CURRENT,
    )

    # Any pre-v2 version present makes the whole project outdated...
    assert classify_ac_version('2.0.0, 1.1.2') == AC_VERSION_OUTDATED
    assert classify_ac_version('1.1.2, 2.0.0') == AC_VERSION_OUTDATED
    # ...but all-current stays current.
    assert classify_ac_version('2.0.0, 2.1.0') == AC_VERSION_CURRENT


def test_classify_ac_version_unidentified():
    from caper.utils import classify_ac_version, AC_VERSION_UNIDENTIFIED

    # None, empty, and placeholder/garbage strings with no parseable version.
    for value in (None, '', 'NA', 'None', 'Not Provided', 'unknown'):
        assert classify_ac_version(value) == AC_VERSION_UNIDENTIFIED, value
