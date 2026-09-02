"""The admin File Ownership page.

Two things are worth holding still here. The page must be staff-only, like every
other page under the Admin menu. And it must stay a **report**: the whole reason
this measurement is on a screen rather than in a script is so that a person can
look at the residue before anyone acts on it, and a delete control next to those
numbers would undo that. Both storage incidents behind this work were a count
like this one being acted on rather than investigated.
"""

import time
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).parents[1] / 'caper' / 'templates' / 'pages' /
            'admin_file_ownership.html')


def _get(request_factory, user, query=''):
    from caper.views_admin import admin_file_ownership
    request = request_factory.get(f'/admin-file-ownership/{query}')
    request.user = user
    return admin_file_ownership(request)


def _post(request_factory, user, data):
    from caper.views_admin import admin_file_ownership
    request = request_factory.post('/admin-file-ownership/', data)
    request.user = user
    return admin_file_ownership(request)


def test_a_non_staff_user_is_sent_away(request_factory, non_member_user):
    response = _get(request_factory, non_member_user)

    assert response.status_code == 302
    assert response['Location'] == '/accounts/logout'


def test_a_staff_user_sees_the_page(request_factory, admin_user):
    response = _get(request_factory, admin_user)

    assert response.status_code == 200


def test_the_command_measures_and_stores_a_snapshot():
    """The management command is the survey's only runner; this runs it.

    Called through ``call_command`` rather than by importing the function it
    wraps, because the wiring -- argument names, the report row it fills in,
    the collections it picks -- is the part that a refactor breaks silently.
    """
    from django.core.management import call_command

    from caper.views_admin import _ownership_reports

    reports = _ownership_reports()
    before = set(reports.distinct('_id'))
    call_command('ownership_survey')

    new_ids = set(reports.distinct('_id')) - before
    assert len(new_ids) == 1
    report_id = new_ids.pop()
    try:
        snapshot = reports.find_one({'_id': report_id})
        assert snapshot['state'] == 'done', snapshot.get('error')
        assert snapshot['total_files'] == (snapshot['owned'] +
                                           snapshot['residue'])
    finally:
        reports.delete_one({'_id': report_id})


def test_the_button_starts_the_survey_outside_this_process(request_factory,
                                                           admin_user,
                                                           monkeypatch):
    """The walk is minutes long; a gunicorn worker has traffic to serve.

    What the button must do is start a process and return. If it ever goes back
    to doing the work inline, this test fails on the survey never being
    spawned -- and ``gridfs_ownership.survey`` raising proves the request
    thread did not run it.
    """
    from caper import gridfs_ownership, ownership_survey
    from caper.views_admin import _ownership_reports

    started = []
    monkeypatch.setattr(ownership_survey, 'spawn',
                        lambda report_id, *a, **k: started.append(
                            (report_id, a, k)) or ['manage.py'])
    monkeypatch.setattr(gridfs_ownership, 'survey', _must_not_run)

    reports = _ownership_reports()
    before = set(reports.distinct('_id'))
    response = _post(request_factory, admin_user, {'action': 'run'})
    assert response.status_code == 302

    new_ids = set(reports.distinct('_id')) - before
    assert len(new_ids) == 1
    report_id = new_ids.pop()
    try:
        assert [call[0] for call in started] == [report_id]
        row = reports.find_one({'_id': report_id})
        assert row['state'] == 'running'
        assert row['started_by'] == str(admin_user)
    finally:
        reports.delete_one({'_id': report_id})


def _must_not_run(*_args, **_kwargs):
    raise AssertionError('the survey ran inside the request')


def test_the_command_line_the_button_builds_is_runnable():
    """The child is a real command, spelled the way manage.py accepts it."""
    from django.core.management import get_commands

    from caper import ownership_survey

    assert ownership_survey.COMMAND in get_commands()
    assert ownership_survey.MANAGE_PY.is_file()


def test_a_failed_spawn_does_not_leave_the_button_disabled_forever(
        request_factory, admin_user, monkeypatch):
    """A row stuck on 'running' disables Run, and only age would clear it.

    ``spawn`` returns None when the process would not start, which is knowable
    immediately -- so the row says failed now rather than in an hour.
    """
    from caper import ownership_survey
    from caper.views_admin import _ownership_reports

    monkeypatch.setattr(ownership_survey, 'spawn', lambda *a, **k: None)

    reports = _ownership_reports()
    before = set(reports.distinct('_id'))
    _post(request_factory, admin_user, {'action': 'run'})

    report_id = (set(reports.distinct('_id')) - before).pop()
    try:
        assert reports.find_one({'_id': report_id})['state'] == 'failed'
    finally:
        reports.delete_one({'_id': report_id})


def test_a_snapshot_can_be_removed_without_touching_a_file(request_factory,
                                                           admin_user):
    from bson.objectid import ObjectId

    from caper.views_admin import _ownership_reports

    reports = _ownership_reports()
    report_id = ObjectId()
    reports.insert_one({'_id': report_id, 'state': 'done', 'total_files': 1})

    _post(request_factory, admin_user,
          {'action': 'remove_snapshot', 'report_id': str(report_id)})

    assert reports.find_one({'_id': report_id}) is None


def test_the_page_offers_no_way_to_delete_a_file():
    """The only destructive control removes a stored measurement, not data."""
    template = TEMPLATE.read_text()

    actions = {line.split('value="', 1)[1].split('"', 1)[0]
               for line in template.splitlines()
               if 'name="action"' in line and 'value="' in line}

    assert actions == {'run', 'remove_snapshot'}, actions
    # The one destructive action names what it removes, and the template says
    # so where a reader will see it.
    assert 'It does not\n        touch a single file.' in template


def test_the_page_says_an_unlabelled_row_is_not_an_orphan():
    """Before the backfill, every residue row is unlabelled.

    Reading that number as an orphan count is precisely the mistake that made
    80,170 live files look like garbage, so the page has to say so on the
    screen rather than in a docstring nobody opens.
    """
    template = TEMPLATE.read_text()

    assert 'the question not yet' in template
    assert '<em>not</em> a count of orphans' in template


@pytest.mark.parametrize('phrase', [
    'This page only counts.',
    'not on its own a reason to delete one',
    'believed and acted on',
])
def test_the_page_carries_the_warning_that_the_numbers_are_evidence(phrase):
    """The warning must be on the screen, in substance rather than in figures.

    An earlier version of this pinned the two incidents' actual counts. They do
    not go stale -- they record what happened -- but on a page whose whole
    subject is current counts they read as current counts, and a reader has no
    way to tell the historical figure from the live one. So the page states what
    went wrong and this asserts the warning is there, without pinning digits a
    reader could mistake for today's.
    """
    assert phrase in TEMPLATE.read_text()


def test_a_survey_whose_worker_died_stops_blocking_the_button():
    """A 'running' row with no thread behind it must not disable Run forever.

    The worker writes 'failed' when the survey raises, but nothing writes it
    when the process goes away underneath -- a restart, a kill, a deploy. Age
    is the only evidence left, so age is what the page reads.
    """
    import datetime

    from caper.views_admin import (OWNERSHIP_SURVEY_ABANDONED_AFTER,
                                   mark_abandoned_surveys)

    now = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.timezone.utc)
    old = now - OWNERSHIP_SURVEY_ABANDONED_AFTER - datetime.timedelta(minutes=1)
    snapshots = [
        {'state': 'running', 'started_at': now - datetime.timedelta(minutes=2)},
        {'state': 'running', 'started_at': old},
        {'state': 'running', 'started_at': old.replace(tzinfo=None)},
        {'state': 'done', 'started_at': old},
    ]

    mark_abandoned_surveys(snapshots, now=now)

    assert [row['state'] for row in snapshots] == [
        'running', 'abandoned', 'abandoned', 'done']


def test_an_abandoned_survey_is_shown_as_such():
    """The state reaches the reader; it is not just corrected in memory."""
    assert '{{ snapshot.state }}' in TEMPLATE.read_text()


def test_the_page_does_not_build_the_audit_table(request_factory, admin_user,
                                                 monkeypatch):
    """The cost this page used to carry, and the reason it is a test.

    ``_get_audit_log_context()`` fetches every live project document and then
    runs two queries and an S3 ``head_object`` per project. This page displays
    none of what it returns. Measured on prod from the gunicorn access log on
    2026-08-29, the two views that called it were the site's two slowest URLs:
    this one averaged 91s over 13 requests against 3.5s for the next admin
    page. Spreading that context into the template dict is one line, which is
    how it got here; this test is what makes removing it stick.
    """
    from caper import views_admin

    def _refuse(request):
        raise AssertionError(
            'admin_file_ownership built the audit-log context; it renders none '
            'of those keys and the query behind them reads every live project')

    monkeypatch.setattr(views_admin, '_get_audit_log_context', _refuse)

    assert _get(request_factory, admin_user).status_code == 200


def test_the_audit_table_query_leaves_the_payload_fields_behind():
    """The projection is the fix for the page that *does* need this context."""
    from caper.views_admin import _AUDIT_PROJECT_FIELDS

    for heavy in ('runs', 'sample_data', 'aggregate_df', 'features_list'):
        assert heavy not in _AUDIT_PROJECT_FIELDS, (
            f'{heavy!r} is the bulk of a project document and the audit table '
            f'does not display it')
    assert _AUDIT_PROJECT_FIELDS['project_name'] == 1
    assert _AUDIT_PROJECT_FIELDS['sample_count'] == 1


def test_an_unfetched_sample_count_reads_as_missing_not_zero():
    """A projected-away ``runs`` must not become a confident zero."""
    from caper.views_admin import _run_audit_checks

    entry = {'sample_count': 12, 'AA_version': '1.0'}

    projected = _run_audit_checks({'AA_version': '1.0'}, entry)
    count = next(c for c in projected['checks'] if c['field'] == 'Sample Count')
    assert count['missing'] is True
    assert count['live_value'] == '—'

    loaded = _run_audit_checks({'AA_version': '1.0', 'runs': {'a': 1, 'b': 2}},
                               entry)
    count = next(c for c in loaded['checks'] if c['field'] == 'Sample Count')
    assert count['live_value'] == '2'
    assert count['missing'] is False
