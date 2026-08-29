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


def test_running_a_survey_records_a_snapshot(request_factory, admin_user):
    """The button starts the walk and the page shows what it found."""
    from caper.views_admin import _ownership_reports

    reports = _ownership_reports()
    before = set(reports.distinct('_id'))

    response = _post(request_factory, admin_user, {'action': 'run'})
    assert response.status_code == 302

    new_ids = set(reports.distinct('_id')) - before
    assert len(new_ids) == 1
    report_id = new_ids.pop()

    try:
        deadline = time.time() + 120
        state = None
        while time.time() < deadline:
            snapshot = reports.find_one({'_id': report_id}) or {}
            state = snapshot.get('state')
            if state in ('done', 'failed'):
                break
            time.sleep(1)
        assert state == 'done', f'survey ended {state!r}: {snapshot.get("error")}'
        assert snapshot['total_files'] == (snapshot['owned'] + snapshot['residue'])
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
    '84 of 345',
    '80,170',
])
def test_the_page_carries_the_warning_that_the_numbers_are_evidence(phrase):
    assert phrase in TEMPLATE.read_text()
