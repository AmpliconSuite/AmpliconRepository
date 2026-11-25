"""
The staff-facing 'Manage Deleted Projects' page: deleting in bulk, and the
dangling-history bug that made bulk deletion unsafe to add without fixing.

The page deleted one project per round trip, and each round trip deleted the
project *and* every older version its history names.  Resolving those older
versions goes through get_one_deleted_project(), which returns None both for a
reference to a document that is no longer in the collection and for one that is
not flagged deleted.  The old code dereferenced that None before deleting
anything, so a project carrying one dangling reference could not be deleted at
all -- the button appeared to do nothing.  One such project exists on dev
(66f74580d2276a48f30a3d40, "what isn't working", confirmed 2026-08-27).

That defect is a nuisance one project at a time and a real problem in bulk: an
unhandled exception halfway through a selection abandons every project after
it, having already deleted the ones before.  So these tests cover the skip and
the batch's tolerance of a single bad row together.
"""

import uuid

import pytest


ADMIN_URL = '/admin-delete-project/'


@pytest.fixture
def projects(request):
    """Soft-deleted project documents, removed again whatever the test does."""
    from caper.utils import audit_log_handle, collection_handle_primary

    marker = f'admindelproj-{uuid.uuid4().hex[:12]}'

    def _cleanup():
        collection_handle_primary.delete_many({'project_name': {'$regex': f'^{marker}'}})
        audit_log_handle.delete_many({'project_name': {'$regex': f'^{marker}'}})
    request.addfinalizer(_cleanup)

    def make(suffix='', *, previous_versions=None, delete=True, **extra):
        doc = {
            'project_name': f'{marker}{suffix}',
            'description': '', 'private': True, 'project_members': [],
            'delete': delete, 'current': True,
        }
        if previous_versions is not None:
            doc['previous_versions'] = previous_versions
        doc.update(extra)
        return collection_handle_primary.insert_one(doc).inserted_id

    make.marker = marker
    make.collection = collection_handle_primary
    return make


@pytest.fixture
def deletions(monkeypatch):
    """Record what admin_permanent_delete_project() is asked to delete."""
    from caper import views_admin

    calls = []

    def fake(project_id, project, project_name):
        calls.append(str(project_id))
        return ''

    monkeypatch.setattr(views_admin, 'admin_permanent_delete_project', fake)
    return calls


# ---------------------------------------------------------------------------
# The history walk
# ---------------------------------------------------------------------------

def test_a_history_entry_naming_a_missing_document_is_skipped_not_raised(projects, deletions):
    from caper.views_admin import permanently_delete_with_history

    gone = '66f74508d2276a48f30a3b10'
    project_id = projects('-dangling', previous_versions=[{'date': '2024-09-27',
                                                           'linkid': gone}])
    project = projects.collection.find_one({'_id': project_id})

    message = permanently_delete_with_history(project_id, project,
                                              project['project_name'])

    assert str(project_id) in deletions, 'the project itself was not deleted'
    assert gone not in deletions
    assert gone in message, 'the skipped entry was not reported to the operator'


def test_an_older_version_that_resolves_is_deleted_with_its_parent(projects, deletions):
    from caper.views_admin import permanently_delete_with_history

    older_id = projects('-older')
    project_id = projects('-head', previous_versions=[
        {'date': '2024-09-27', 'linkid': str(older_id)}])
    project = projects.collection.find_one({'_id': project_id})

    permanently_delete_with_history(project_id, project, project['project_name'])

    assert str(older_id) in deletions
    assert str(project_id) in deletions


def test_the_project_is_deleted_once_not_twice(projects, deletions):
    """
    The walk used to read previous_versions(), the function that builds the
    history table on the project page.  That list always ends with an entry for
    the version being viewed, so every delete ran the whole teardown against
    the project itself twice -- once from inside the history loop, once at the
    end.  Wasted work while it succeeded, and while it failed it failed in the
    loop, where the exception abandons the rest of a batch.
    """
    from caper.views_admin import permanently_delete_with_history

    project_id = projects('-solo')
    project = projects.collection.find_one({'_id': project_id})

    permanently_delete_with_history(project_id, project, project['project_name'])

    assert deletions.count(str(project_id)) == 1


def test_deleting_an_older_version_does_not_reach_its_head(projects, deletions):
    """
    Asked for a project that is not the head of its chain, previous_versions()
    returns the *head's* history with the head appended -- so the walk would
    delete the head and every sibling, none of which were selected.  The stored
    previous_versions field names ancestors only, which is what the walk wants.
    """
    from caper.views_admin import permanently_delete_with_history

    older_id = projects('-older')
    head_id = projects('-head', previous_versions=[
        {'date': '2024-09-27', 'linkid': str(older_id)}])
    older = projects.collection.find_one({'_id': older_id})

    permanently_delete_with_history(older_id, older, older['project_name'])

    assert str(head_id) not in deletions
    assert projects.collection.find_one({'_id': head_id}) is not None


def test_a_tombstoned_ancestor_goes_with_the_project(projects, deletions):
    """The array cannot name a deleted version, but the chain can.

    Deleting a version removes it from every previous_versions[] while it stays
    in the chain holding its ordinal. Left behind by a permanent delete it
    would outlive the project it belonged to, as a tombstone redirecting at an
    id that no longer exists.
    """
    from bson import ObjectId
    from caper.views_admin import permanently_delete_with_history

    chain_id = ObjectId()
    tombstone_id = projects('-deleted-v1', version_chain_id=chain_id,
                            version_ordinal=1, is_latest=False, current=False,
                            version_deleted_from_history=True,
                            payload_purged=True)
    head_id = projects('-head', previous_versions=[],
                       version_chain_id=chain_id, version_ordinal=2,
                       is_latest=True)
    head = projects.collection.find_one({'_id': head_id})

    permanently_delete_with_history(head_id, head, head['project_name'])

    assert str(tombstone_id) in deletions
    assert str(head_id) in deletions


def test_the_chain_walk_still_does_not_reach_forward(projects, deletions):
    """Ancestors only -- strictly lower ordinal.

    Selecting an older version must not delete the head, which is the hazard
    the array-based walk was narrowed to avoid in the first place.
    """
    from bson import ObjectId
    from caper.views_admin import permanently_delete_with_history

    chain_id = ObjectId()
    older_id = projects('-older', version_chain_id=chain_id,
                        version_ordinal=1, is_latest=False, current=False)
    head_id = projects('-head', version_chain_id=chain_id,
                       version_ordinal=2, is_latest=True,
                       version_deleted_from_history=True, payload_purged=True)
    older = projects.collection.find_one({'_id': older_id})

    permanently_delete_with_history(older_id, older, older['project_name'])

    assert str(head_id) not in deletions
    assert projects.collection.find_one({'_id': head_id}) is not None


def test_a_surviving_ancestor_is_not_swept_up_by_the_chain_walk(projects, deletions):
    """Only tombstones are added from the chain.

    A superseded ancestor the array does not name is a divergence the validator
    reports under I11; quietly deleting it here would destroy the evidence.
    """
    from bson import ObjectId
    from caper.views_admin import permanently_delete_with_history

    chain_id = ObjectId()
    older_id = projects('-older', version_chain_id=chain_id,
                        version_ordinal=1, is_latest=False, current=False)
    head_id = projects('-head', previous_versions=[],
                       version_chain_id=chain_id, version_ordinal=2,
                       is_latest=True)
    head = projects.collection.find_one({'_id': head_id})

    permanently_delete_with_history(head_id, head, head['project_name'])

    assert str(older_id) not in deletions


def test_a_history_entry_pointing_at_a_live_project_is_skipped(projects, deletions):
    """
    get_one_deleted_project() matches only {'delete': True}, so a reference to
    a project that is still live resolves to None just as a missing one does.
    Deleting it would destroy a project nobody asked to delete.
    """
    from caper.views_admin import permanently_delete_with_history

    live_id = projects('-live', delete=False)
    project_id = projects('-head', previous_versions=[
        {'date': '2024-09-27', 'linkid': str(live_id)}])
    project = projects.collection.find_one({'_id': project_id})

    permanently_delete_with_history(project_id, project, project['project_name'])

    assert str(live_id) not in deletions
    assert projects.collection.find_one({'_id': live_id}) is not None


# ---------------------------------------------------------------------------
# Deleting a selection
# ---------------------------------------------------------------------------

def test_every_selected_project_is_deleted(projects, deletions):
    from caper.views_admin import delete_selected_projects

    ids = [str(projects(f'-{n}')) for n in range(3)]

    message = delete_selected_projects(ids)

    for project_id in ids:
        assert project_id in deletions
    assert 'Permanently deleted 3 of 3' in message
    assert 'Problem' not in message


def test_one_failing_project_does_not_abandon_the_rest(projects, monkeypatch):
    from caper import views_admin

    first, bad, last = (str(projects('-first')), str(projects('-bad')),
                        str(projects('-last')))
    deleted = []

    def fake(project_id, project, project_name):
        if str(project_id) == bad:
            raise RuntimeError('GridFS is unhappy')
        deleted.append(str(project_id))
        return ''

    monkeypatch.setattr(views_admin, 'admin_permanent_delete_project', fake)

    message = views_admin.delete_selected_projects([first, bad, last])

    assert first in deleted and last in deleted
    assert 'Permanently deleted 2 of 3' in message
    # The banner turns from success to warning on this word.
    assert 'Problem' in message


def test_a_selected_id_that_no_longer_resolves_is_reported(projects, deletions):
    from caper.views_admin import delete_selected_projects

    live_id = str(projects('-live', delete=False))

    message = delete_selected_projects([live_id])

    assert deletions == []
    assert 'Permanently deleted 0 of 1' in message
    assert live_id in message


def test_the_batch_stops_before_gunicorn_kills_it(projects, monkeypatch):
    """
    A worker killed at 900 seconds takes the report with it, so the operator
    cannot tell which of the selection was deleted.  Stopping early keeps the
    report, and costs nothing: each project is finished before the next starts.
    """
    from caper import views_admin

    ids = [str(projects(f'-{n}')) for n in range(4)]
    deleted = []
    ticks = {'n': 0}

    def slow(project_id, project, project_name):
        deleted.append(str(project_id))
        return ''

    def clock():
        ticks['n'] += 1
        return ticks['n']

    monkeypatch.setattr(views_admin, 'admin_permanent_delete_project', slow)
    monkeypatch.setattr(views_admin.time, 'monotonic', clock)

    message = views_admin.delete_selected_projects(ids, time_budget=1)

    assert len(deleted) < len(ids), 'the budget did not stop the batch'
    assert 'not attempted' in message
    assert 'Permanently deleted' in message


def test_the_first_project_is_always_attempted(projects, monkeypatch):
    """An exhausted budget must not turn a one-project selection into a no-op."""
    from caper import views_admin

    project_id = str(projects('-solo'))
    deleted = []
    monkeypatch.setattr(views_admin, 'admin_permanent_delete_project',
                        lambda pid, p, n: deleted.append(str(pid)))

    message = views_admin.delete_selected_projects([project_id], time_budget=0)

    assert deleted == [project_id]
    assert 'not attempted' not in message


def test_an_empty_selection_deletes_nothing(projects, deletions):
    from caper.views_admin import delete_selected_projects

    message = delete_selected_projects([])

    assert deletions == []
    assert 'Permanently deleted 0 of 0' in message


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

def _post(request_factory, admin_user, data):
    # HTTP_HOST because the staff-gate redirect is absolute and goes through
    # ALLOWED_HOSTS, which does not contain RequestFactory's 'testserver'.
    req = request_factory.post(ADMIN_URL, data, HTTP_HOST='localhost')
    req.user = admin_user
    from caper.views import admin_delete_project
    return admin_delete_project(req)


def test_the_view_deletes_the_posted_selection(projects, request_factory, admin_user):
    """
    End to end against Mongo, unpatched: the documents are really gone.
    """
    kept = projects('-kept')
    selected = [str(projects('-a')), str(projects('-b'))]

    response = _post(request_factory, admin_user,
                     {'action': 'delete-selected', 'project_ids': selected})

    assert response.status_code == 200
    for project_id in selected:
        from bson import ObjectId
        assert projects.collection.find_one({'_id': ObjectId(project_id)}) is None
    assert projects.collection.find_one({'_id': kept}) is not None


def test_the_bulk_action_does_not_go_through_the_single_project_form(
        projects, request_factory, admin_user, deletions):
    """
    DeletedProjectForm describes one project and has no project_ids field, so
    the bulk action has to be dispatched before the form is built.  Posting
    without project_name or project_id proves that is where it is dispatched.
    """
    project_id = str(projects('-solo'))

    response = _post(request_factory, admin_user,
                     {'action': 'delete-selected', 'project_ids': [project_id]})

    assert response.status_code == 200
    assert project_id in deletions


def _purge_events(project_id):
    from caper import provenance
    from caper.utils import audit_log_handle
    return list(audit_log_handle.find(
        {'event_type': provenance.PERMANENT_DELETE, 'project_uuid': str(project_id)}))


@pytest.fixture
def s3(monkeypatch):
    """A stand-in for S3 alone; set s3.error to make its delete fail.

    Everything else about the delete is real. A stand-in for the delete itself
    is what hid the audit log calling every success a failure (#773): it
    returned an empty message, which the real one never does.
    """
    from django.conf import settings
    from caper import views_admin

    class _S3:
        error = None

        def client(self, name):
            return self

        def delete_object(self, **kwargs):
            if self.error:
                raise self.error

    fake = _S3()
    for name, value in (('S3_DOWNLOADS_BUCKET', 'no-such-bucket'),
                        ('S3_DOWNLOADS_BUCKET_PATH', 'test/'),
                        ('AWS_PROFILE_NAME', 'no-such-profile')):
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(views_admin.boto3, 'Session', lambda **kwargs: fake)
    return fake


@pytest.fixture(params=['single', 'bulk'])
def delete_button(request, request_factory, admin_user, projects):
    """Press one of the page's two delete buttons for a project."""
    def press(project_id):
        if request.param == 'bulk':
            data = {'action': 'delete-selected', 'project_ids': [str(project_id)]}
        else:
            name = projects.collection.find_one({'_id': project_id})['project_name']
            data = {'action': 'delete', 'delete': 'on', 'project_id': str(project_id),
                    'project_name': name}
        response = _post(request_factory, admin_user, data)
        assert response.status_code == 200
        return response
    return press


def test_a_delete_is_recorded_once_as_removed(projects, s3, delete_button, admin_user):
    project_id = projects('-gone')

    delete_button(project_id)

    assert projects.collection.find_one({'_id': project_id}) is None
    event, = _purge_events(project_id)
    assert event['outcome'] == 'removed'
    assert event['error_message'] is None
    assert event['completed'] is True
    assert event['user_email'] == (admin_user.email or admin_user.username)


def test_a_bulk_delete_records_an_event_for_each_project(projects, s3):
    from caper.views_admin import delete_selected_projects
    ids = [projects(f'-prov-{n}') for n in range(2)]

    delete_selected_projects([str(i) for i in ids])

    assert [[e['outcome'] for e in _purge_events(i)] for i in ids] == [['removed'], ['removed']]


def test_a_problem_beside_the_delete_is_removed_and_keeps_its_message(
        projects, s3, delete_button):
    s3.error = RuntimeError('S3 is unhappy')
    project_id = projects('-tarless')

    delete_button(project_id)

    event, = _purge_events(project_id)
    assert event['outcome'] == 'removed'
    assert 'Problem deleting tar file from S3' in event['error_message']


def test_a_skipped_history_entry_is_kept_in_the_record(projects, s3, delete_button):
    missing = uuid.uuid4().hex[:24]
    project_id = projects('-dangling', previous_versions=[{'linkid': missing}])

    delete_button(project_id)

    event, = _purge_events(project_id)
    assert event['outcome'] == 'removed'
    assert missing in event['error_message']


def test_a_document_that_did_not_go_is_recorded_as_failed(
        projects, s3, delete_button, monkeypatch):
    """admin_permanent_delete_project() returns a message here, not an exception."""
    from caper import views_admin

    class _Refusing:
        def __getattr__(self, name):
            return getattr(projects.collection, name)

        def delete_one(self, *args, **kwargs):
            raise RuntimeError('Mongo is unhappy')

    monkeypatch.setattr(views_admin, 'collection_handle', _Refusing())
    project_id = projects('-stuck')

    delete_button(project_id)

    event, = _purge_events(project_id)
    assert event['outcome'] == 'failed'
    assert 'Problem deleting Project document' in event['error_message']


def test_a_delete_that_ran_out_of_time_is_recorded_as_failed(
        projects, s3, delete_button, monkeypatch):
    from types import SimpleNamespace
    from caper import views_admin
    monkeypatch.setattr(views_admin, 'delete_payload_within_deadline',
                        lambda *a, **kw: SimpleNamespace(deleted=1, remaining=['x']))
    project_id = projects('-partial')

    delete_button(project_id)

    assert projects.collection.find_one({'_id': project_id}) is not None
    event, = _purge_events(project_id)
    assert event['outcome'] == 'failed'
    assert views_admin.PARTIAL_DELETE_MARKER in event['error_message']


def test_a_refused_delete_is_recorded_as_refused(projects, s3, delete_button):
    from caper.publication_versions import FIELD
    project_id = projects('-tagged', **{FIELD: {'publication': 'Test 2026'}})

    delete_button(project_id)

    assert projects.collection.find_one({'_id': project_id}) is not None
    event, = _purge_events(project_id)
    assert event['outcome'] == 'refused'
    assert event['error_message']


def test_an_unreadable_database_does_not_break_the_page_or_guess(
        projects, s3, monkeypatch):
    """The record is evidence, never authority: not being able to check says so."""
    from caper import views_admin

    class _Unreadable:
        def find_one(self, *args, **kwargs):
            raise RuntimeError('no primary')

    monkeypatch.setattr(views_admin, 'collection_handle_primary', _Unreadable())

    assert views_admin._purge_record(str(projects('-x')), 'x', 'Project x deleted.') == {
        'outcome': 'unknown', 'error_message': None}


# ---------------------------------------------------------------------------
# A delete that fails keeps the document
# ---------------------------------------------------------------------------

@pytest.fixture(params=['delete_payload_within_deadline', 'still_needed_here'])
def payload_cannot_go(request, monkeypatch):
    """The payload delete itself fails, or the check that must come before it does.

    The document is what names the files. Removing it after either of these
    strands them, with nothing left to say whose they were.
    """
    from caper import views_admin

    def fail(*args, **kwargs):
        raise RuntimeError(f'simulated failure in {request.param}')
    monkeypatch.setattr(views_admin, request.param, fail)


def test_a_failed_delete_raises_and_keeps_the_document(projects, payload_cannot_go):
    from caper.views_admin import admin_permanent_delete_project

    project_id = projects('-kept')
    project = projects.collection.find_one({'_id': project_id})

    with pytest.raises(RuntimeError):
        admin_permanent_delete_project(str(project_id), project, project['project_name'])

    assert projects.collection.find_one({'_id': project_id}) is not None


def test_the_batch_reports_a_failed_delete_as_not_deleted(projects, payload_cannot_go):
    """A returned message would read "Permanently deleted 1 of 1" here."""
    from caper.views_admin import delete_selected_projects

    project_id = projects('-kept')

    message = delete_selected_projects([str(project_id)])

    assert 'Permanently deleted 0 of 1' in message
    assert 'Problem deleting' in message
    assert projects.collection.find_one({'_id': project_id}) is not None


def test_a_failure_on_an_older_version_stops_before_the_project(projects, payload_cannot_go):
    from caper.views_admin import permanently_delete_with_history

    older = projects('-older')
    head = projects('-head', previous_versions=[{'linkid': str(older)}])
    project = projects.collection.find_one({'_id': head})

    with pytest.raises(RuntimeError):
        permanently_delete_with_history(str(head), project, project['project_name'])

    assert projects.collection.find_one({'_id': older}) is not None
    assert projects.collection.find_one({'_id': head}) is not None


def test_the_single_delete_view_reports_a_failure_and_records_it(
        projects, payload_cannot_go, request_factory, admin_user):
    from caper import provenance
    from caper.utils import audit_log_handle

    project_id = projects('-kept')
    name = projects.collection.find_one({'_id': project_id})['project_name']

    response = _post(request_factory, admin_user,
                     {'action': 'delete', 'delete': 'on', 'project_id': str(project_id),
                      'project_name': name})

    assert response.status_code == 200
    assert b'Problem deleting project' in response.content
    assert projects.collection.find_one({'_id': project_id}) is not None
    event = audit_log_handle.find_one(
        {'event_type': provenance.PERMANENT_DELETE, 'project_uuid': str(project_id)},
        sort=[('_id', -1)])
    assert event is not None and event.get('outcome') == 'failed'


def test_an_account_deletion_reports_a_failed_delete_as_an_error(projects, payload_cannot_go):
    """It ignores the returned message, so only an exception can reach its report."""
    from caper.account_deletion import dispose_of_projects

    username = f'{projects.marker}-user'
    project_id = projects('-kept', project_members=[username], private='private', delete=False)

    report = dispose_of_projects(username, None, projects_collection=projects.collection)

    assert report['deleted'] == []
    assert report['errors'] == [projects.collection.find_one({'_id': project_id})['project_name']]
