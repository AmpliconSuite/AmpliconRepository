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
