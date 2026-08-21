"""
What happens to the projects nobody else is a member of.

Removing the account from a shared project is obvious. The interesting case is a
project whose only member is leaving, where there are three possible answers and
picking the wrong one either destroys data or leaves a project no one owns:

  * private -> delete it. Nobody else could see it, and keeping private data
    after its only owner has asked to leave is the thing account deletion is
    meant to avoid.
  * public or hidden_public -> hand it to a caretaker. Someone may have linked to
    it in print. "Unlisted" is not "unreachable".
  * shared -> not this module's problem; purge_account_references pulls the
    member out.

These tests exist mostly to pin down the visibility check, which is where this
went wrong before: the field holds either a legacy boolean or one of three
strings, and ``if project['private']`` is true for the *string* ``'public'``.
That is a public project taking the delete branch.
"""

import uuid

import pytest

from caper.account_deletion import (
    DELETE, REASSIGN, RELEASE, dispose_of_projects, plan_account_deletion,
)

from test_account_deletion_cleanup import FakeCollection


USERNAME = 'departing_user'
EMAIL = 'departing_user@example.com'


class RecordingDeleter:
    """Stands in for admin_permanent_delete_project, which touches S3 and GridFS."""

    def __init__(self, fail_on=None):
        self.deleted = []
        self.fail_on = fail_on or set()

    def __call__(self, project_id, project, project_name):
        if project_name in self.fail_on:
            raise RuntimeError(f'could not delete {project_name}')
        self.deleted.append(project_name)
        return ''


def _project(_id, name, *, private, members, current=True):
    return {'_id': _id, 'project_name': name, 'private': private,
            'current': current, 'project_members': list(members)}


@pytest.fixture
def projects():
    return FakeCollection([
        _project(1, 'solo-private', private='private', members=[USERNAME]),
        _project(2, 'solo-public', private='public', members=[USERNAME]),
        _project(3, 'solo-hidden', private='hidden_public', members=[USERNAME]),
        _project(4, 'shared', private='private', members=[USERNAME, 'colleague']),
        _project(5, 'solo-by-email', private='private', members=[EMAIL]),
        _project(6, 'someone-elses', private='private', members=['colleague']),
        _project(7, 'solo-private', private='private', members=[USERNAME],
                 current=False),
    ])


@pytest.fixture
def caretaker(monkeypatch):
    """Pin the caretaker so the tests do not depend on who exists in the auth DB."""
    from caper import account_deletion
    monkeypatch.setattr(account_deletion, 'caretaker_username', lambda: 'curator')
    return 'curator'


def _dispose(projects, deleter=None):
    deleter = deleter or RecordingDeleter()
    report = dispose_of_projects(USERNAME, EMAIL,
                                 projects_collection=projects,
                                 delete_project=deleter)
    return report, deleter


def _doc(projects, _id):
    return next(d for d in projects.docs if d['_id'] == _id)


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------

def test_solo_private_project_is_deleted(projects, caretaker):
    report, deleter = _dispose(projects)

    assert 'solo-private' in deleter.deleted
    assert 'solo-private' in report['deleted']


def test_solo_public_project_is_reassigned_not_deleted(projects, caretaker):
    report, deleter = _dispose(projects)

    assert 'solo-public' not in deleter.deleted
    assert ('solo-public', caretaker) in report['reassigned']
    assert _doc(projects, 2)['project_members'] == [caretaker]


def test_solo_hidden_public_project_is_reassigned_not_deleted(projects, caretaker):
    """Unlisted still means someone may hold a working link to it.

    is_project_private() groups hidden_public with private, which is right for
    access control and wrong here -- deleting it would break a link that was
    handed out deliberately, to a reviewer or in a manuscript.
    """
    report, deleter = _dispose(projects)

    assert 'solo-hidden' not in deleter.deleted
    assert ('solo-hidden', caretaker) in report['reassigned']
    assert _doc(projects, 3)['project_members'] == [caretaker]


def test_shared_project_is_left_for_the_purge(projects, caretaker):
    report, deleter = _dispose(projects)

    assert 'shared' not in deleter.deleted
    assert report['released'] == ['shared']
    # Untouched here -- purge_account_references does the $pull.
    assert _doc(projects, 4)['project_members'] == [USERNAME, 'colleague']


def test_membership_recorded_by_email_still_counts_as_solo(projects, caretaker):
    """The members box takes either form, so matching on username alone under-counts.

    Miss this and the project looks like it has no members at all rather than
    one departing member, escapes the decision entirely, and is left orphaned.
    """
    _, deleter = _dispose(projects)

    assert 'solo-by-email' in deleter.deleted


def test_other_peoples_projects_are_untouched(projects, caretaker):
    report, deleter = _dispose(projects)

    assert 'someone-elses' not in deleter.deleted
    assert _doc(projects, 6)['project_members'] == ['colleague']


def test_superseded_versions_are_not_disposed_of(projects, caretaker):
    """Provenance again: an old version of a project is a record, not a project."""
    report, deleter = _dispose(projects)

    assert deleter.deleted.count('solo-private') == 1
    assert _doc(projects, 7)['project_members'] == [USERNAME]


# ---------------------------------------------------------------------------
# The visibility field, which has had three representations over time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('private_value, expected', [
    (True, DELETE),
    ('private', DELETE),
    (False, REASSIGN),
    ('public', REASSIGN),
    ('hidden_public', REASSIGN),
])
def test_visibility_is_normalized_before_deciding(private_value, expected, caretaker):
    """`if project['private']` is true for the string 'public'.

    That is not a hypothetical: the field was a boolean before it was a string,
    both forms are in the database, and the truthy-string reading sends a public
    project down the delete branch.
    """
    collection = FakeCollection([
        _project(1, 'p', private=private_value, members=[USERNAME]),
    ])

    plan = plan_account_deletion(USERNAME, EMAIL, projects_collection=collection)

    assert [action for _, action, _ in plan] == [expected]


def test_a_missing_visibility_field_is_treated_as_private(caretaker):
    """Deleting is the cautious answer when the record does not say."""
    collection = FakeCollection([
        {'_id': 1, 'project_name': 'p', 'current': True,
         'project_members': [USERNAME]},
    ])

    plan = plan_account_deletion(USERNAME, EMAIL, projects_collection=collection)

    assert [action for _, action, _ in plan] == [DELETE]


# ---------------------------------------------------------------------------
# The promise the confirmation screens make
# ---------------------------------------------------------------------------

def test_the_plan_matches_what_disposal_does(projects, caretaker):
    """Both confirmation screens render plan_account_deletion.

    If the plan and the disposal could disagree, the page would be asking for
    consent to something other than what happens -- so they walk the same list.
    """
    plan = plan_account_deletion(USERNAME, EMAIL, projects_collection=projects)
    promised = {
        'delete': sorted(p['project_name'] for p, a, _ in plan if a == DELETE),
        'reassign': sorted(p['project_name'] for p, a, _ in plan if a == REASSIGN),
        'release': sorted(p['project_name'] for p, a, _ in plan if a == RELEASE),
    }

    report, deleter = _dispose(projects)

    assert promised['delete'] == sorted(report['deleted']) == sorted(deleter.deleted)
    assert promised['reassign'] == sorted(name for name, _ in report['reassigned'])
    assert promised['release'] == sorted(report['released'])


def test_plan_is_read_only(projects):
    before = [dict(d) for d in projects.docs]

    plan_account_deletion(USERNAME, EMAIL, projects_collection=projects)

    assert projects.docs == before


def test_an_account_with_no_identifiers_plans_nothing(projects):
    assert plan_account_deletion(None, None, projects_collection=projects) == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_one_failed_project_does_not_abandon_the_rest(projects, caretaker):
    """The account row is already gone; stopping half way just leaves more mess."""
    deleter = RecordingDeleter(fail_on={'solo-private'})

    report, _ = _dispose(projects, deleter)

    assert report['errors'] == ['solo-private']
    assert 'solo-by-email' in report['deleted']
    assert ('solo-public', caretaker) in report['reassigned']


# ---------------------------------------------------------------------------
# Choosing the caretaker
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_caretaker_falls_back_when_the_named_account_is_absent(monkeypatch):
    """A fresh deployment will not have the configured caretaker in it.

    Reassigning to an account that does not exist leaves the project as
    ownerless as doing nothing, so an existing staff account is used instead.
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from caper.account_deletion import caretaker_username

    monkeypatch.setattr(
        settings, 'ORPHANED_PROJECT_OWNER',
        f'definitely_not_a_user_{uuid.uuid4().hex[:8]}', raising=False)

    chosen = caretaker_username()

    User = get_user_model()
    if User.objects.filter(is_staff=True).exists():
        assert User.objects.filter(username=chosen, is_staff=True).exists()
    else:
        assert chosen == 'admin'


# ---------------------------------------------------------------------------
# End to end, against MongoDB rather than the fake
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_real_deletion_reassigns_a_real_public_project(request, monkeypatch):
    """Deleting the Django user must reassign, through the post_delete receiver.

    Only the reassignment path is exercised against real data: the delete path
    calls admin_permanent_delete_project, which reaches into GridFS, the local
    filesystem and S3, and is covered by the injected deleter above instead.
    """
    from django.contrib.auth import get_user_model
    from caper import account_deletion
    from caper.utils import collection_handle_primary

    monkeypatch.setattr(account_deletion, 'caretaker_username', lambda: 'curator')

    suffix = uuid.uuid4().hex[:12]
    username = f'dispose_e2e_{suffix}'
    email = f'dispose_e2e_{suffix}@example.invalid'
    marker = f'dispose-e2e-{suffix}'

    solo_public = collection_handle_primary.insert_one({
        'project_name': marker, 'current': True, 'delete': True,
        'private': 'public', 'project_members': [username],
    }).inserted_id
    shared = collection_handle_primary.insert_one({
        'project_name': marker, 'current': True, 'delete': True,
        'private': 'private', 'project_members': [username, 'colleague'],
    }).inserted_id

    request.addfinalizer(
        lambda: collection_handle_primary.delete_many({'project_name': marker}))

    get_user_model().objects.create_user(username=username, email=email).delete()

    assert collection_handle_primary.find_one(
        {'_id': solo_public})['project_members'] == ['curator']
    assert collection_handle_primary.find_one(
        {'_id': shared})['project_members'] == ['colleague']
