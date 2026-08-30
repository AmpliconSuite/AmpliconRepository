"""The survey runs in its own process, and says so when it is done.

Before this it ran in a thread inside a gunicorn worker: it competed with
request serving for the process it shared, it could only be started by loading
a page, and the only way to learn it had finished was to reload that page. The
tests here hold the three properties that replaced those.
"""
import datetime

import pytest

from caper import ownership_survey


class FakeReports:
    def __init__(self):
        self.docs = {}
        self.updates = []

    def insert_one(self, doc):
        self.docs[doc['_id']] = dict(doc)

    def update_one(self, query, update):
        self.updates.append(update['$set'])
        self.docs.setdefault(query['_id'], {}).update(update['$set'])

    def find_one(self, query):
        return self.docs.get(query['_id'])


def _survey_of(documents, calls_progress=0):
    def survey(_projects, _fs_files, progress=None):
        for done in range(1, calls_progress + 1):
            progress(done * 25, done * 100)
        return {'documents': documents, 'total_files': 3, 'owned': 2,
                'residue': 1, 'counts': {'owned-by-a-live-document': 2},
                'per_project': [{'n': i} for i in range(500)]}
    return survey


# --- the walk reports where it is -----------------------------------------

def test_progress_reaches_the_row_while_the_survey_is_still_running(monkeypatch):
    """A six-minute walk that shows nothing is indistinguishable from a hang."""
    from caper import gridfs_ownership
    monkeypatch.setattr(gridfs_ownership, 'survey',
                        _survey_of(311, calls_progress=3))

    reports = FakeReports()
    report_id = 'r1'
    reports.insert_one({'_id': report_id, 'state': 'running'})
    ownership_survey.run_and_store(reports, None, None, report_id)

    progressed = [u for u in reports.updates if 'documents_done' in u]
    assert [u['documents_done'] for u in progressed] == [25, 50, 75]
    assert reports.docs[report_id]['state'] == 'done'


def test_a_progress_write_that_fails_does_not_lose_the_survey(monkeypatch):
    from caper import gridfs_ownership
    monkeypatch.setattr(gridfs_ownership, 'survey',
                        _survey_of(311, calls_progress=2))

    class Brittle(FakeReports):
        def update_one(self, query, update):
            if 'documents_done' in update['$set']:
                raise RuntimeError('no')
            super().update_one(query, update)

    reports = Brittle()
    reports.insert_one({'_id': 'r1', 'state': 'running'})
    ownership_survey.run_and_store(reports, None, None, 'r1')

    assert reports.docs['r1']['state'] == 'done'


def test_the_stored_snapshot_is_capped(monkeypatch):
    """A snapshot that grows without bound is one that cannot be written back."""
    from caper import gridfs_ownership
    monkeypatch.setattr(gridfs_ownership, 'survey', _survey_of(311))

    reports = FakeReports()
    reports.insert_one({'_id': 'r1', 'state': 'running'})
    ownership_survey.run_and_store(reports, None, None, 'r1')

    assert len(reports.docs['r1']['per_project']) == ownership_survey.PROJECT_ROWS


def test_a_failed_survey_says_so_on_the_row(monkeypatch):
    from caper import gridfs_ownership

    def boom(*_a, **_kw):
        raise ValueError('cursor died')
    monkeypatch.setattr(gridfs_ownership, 'survey', boom)

    reports = FakeReports()
    reports.insert_one({'_id': 'r1', 'state': 'running'})
    assert ownership_survey.run_and_store(reports, None, None, 'r1') is None

    assert reports.docs['r1']['state'] == 'failed'
    assert 'cursor died' in reports.docs['r1']['error']


# --- it tells someone ------------------------------------------------------

def test_the_result_is_mailed_to_whoever_started_it(monkeypatch):
    """The survey outlives the tab that started it, so the tab is not the answer."""
    from caper import gridfs_ownership
    monkeypatch.setattr(gridfs_ownership, 'survey', _survey_of(311))

    sent = []
    monkeypatch.setattr(ownership_survey, '_notify',
                        lambda *a, **k: sent.append((a, k)))

    reports = FakeReports()
    reports.insert_one({'_id': 'r1', 'state': 'running'})
    ownership_survey.run_and_store(reports, None, None, 'r1',
                                   notify='someone@example.org')

    assert len(sent) == 1
    assert sent[0][0][2] == 'someone@example.org'


def test_a_mail_that_will_not_send_does_not_lose_the_snapshot(monkeypatch):
    """The measurement is the point; the message is a convenience."""
    import django.core.mail

    def refuse(*_a, **_kw):
        raise RuntimeError('no smtp here')
    monkeypatch.setattr(django.core.mail, 'EmailMessage', refuse)

    reports = FakeReports()
    ownership_survey._notify(reports, 'r1', 'someone@example.org',
                             result={'documents': 1, 'total_files': 1,
                                     'counts': {}})


def test_nobody_to_tell_sends_nothing(monkeypatch):
    import django.core.mail
    monkeypatch.setattr(django.core.mail, 'EmailMessage',
                        lambda *a, **k: pytest.fail('mailed nobody'))

    ownership_survey._notify(FakeReports(), 'r1', None, result={})
    ownership_survey._notify(FakeReports(), 'r1', '', result={})


# --- the process it starts -------------------------------------------------

def test_spawn_builds_a_command_line_manage_py_accepts(monkeypatch):
    launched = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            launched['argv'] = argv
            launched['kwargs'] = kwargs

    monkeypatch.setattr(ownership_survey.subprocess, 'Popen', FakePopen)

    argv = ownership_survey.spawn('abc123', 'someone', 'someone@example.org')

    assert argv[1].endswith('manage.py')
    assert argv[2] == 'ownership_survey'
    assert '--report-id' in argv and 'abc123' in argv
    assert '--notify' in argv and 'someone@example.org' in argv
    # A new session, or a gunicorn reload signals the child along with the
    # worker that started it.
    assert launched['kwargs']['start_new_session'] is True
    # The database URI and the mail credentials are sourced into the worker's
    # environment after the container starts; inheriting is how the child gets
    # them, and PID 1 does not have them to give.
    assert launched['kwargs']['env']['PATH']


def test_spawn_returning_none_is_how_a_caller_learns_it_failed(monkeypatch):
    def refuse(*_a, **_kw):
        raise OSError('no fork for you')
    monkeypatch.setattr(ownership_survey.subprocess, 'Popen', refuse)

    assert ownership_survey.spawn('abc123') is None


# --- age is still the only evidence a process died ------------------------

def test_a_survey_whose_process_died_is_still_marked_abandoned():
    """Moved here with the runner; the page's behaviour must not change.

    A separate process is no more immortal than a thread was -- the container
    going away takes it too -- so the age rule stays exactly as it was.
    """
    now = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.timezone.utc)
    old = now - ownership_survey.ABANDONED_AFTER - datetime.timedelta(minutes=1)
    snapshots = [{'state': 'running', 'started_at': old},
                 {'state': 'running',
                  'started_at': now - datetime.timedelta(minutes=1)}]

    ownership_survey.mark_abandoned_surveys(snapshots, now=now)

    assert [s['state'] for s in snapshots] == ['abandoned', 'running']
