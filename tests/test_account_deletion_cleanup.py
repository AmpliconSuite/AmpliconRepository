"""
What survives an account deletion, and what does not.

Projects live in MongoDB and name their members by string -- sometimes a
username, sometimes an email address, because the members box accepts either.
Deleting the Django user used to leave every one of those strings behind, along
with the subscriber list and the notification-preferences document.

The line these tests draw:

  * active records -- the live project's member and subscriber lists, and the
    user's preferences -- lose the account's identifiers;
  * historical records -- superseded project versions and the audit log -- keep
    them, because they exist to say what was true at the time. An account
    closing is not a reason to rewrite the provenance of a dataset.
"""

import uuid

import pytest

from caper.account_signals import purge_account_references


# ---------------------------------------------------------------------------
# A minimal in-memory stand-in for the two collections
# ---------------------------------------------------------------------------

class FakeCollection:
    """Enough of the pymongo surface for the queries purge_account_references makes.

    Supports the exact-match, ``$in`` and ``$pull``-with-``$in`` forms used
    there. A real MongoDB would work too, but the point of these tests is the
    decision about which documents to touch, and a fake makes the fixtures
    readable and the assertions exact.
    """

    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, query):
        for key, condition in query.items():
            value = doc.get(key)
            if isinstance(condition, dict) and '$in' in condition:
                candidates = value if isinstance(value, list) else [value]
                if not set(candidates) & set(condition['$in']):
                    return False
            elif isinstance(value, list):
                if condition not in value:
                    return False
            elif value != condition:
                return False
        return True

    def update_many(self, query, update):
        modified = 0
        for doc in self.docs:
            if not self._matches(doc, query):
                continue
            changed = False
            for field, condition in update.get('$pull', {}).items():
                current = doc.get(field, [])
                remaining = [v for v in current if v not in condition['$in']]
                if remaining != current:
                    doc[field] = remaining
                    changed = True
            if changed:
                modified += 1
        return type('Result', (), {'modified_count': modified})()

    def delete_many(self, query):
        keep = [d for d in self.docs if not self._matches(d, query)]
        deleted = len(self.docs) - len(keep)
        self.docs = keep
        return type('Result', (), {'deleted_count': deleted})()


USERNAME = 'departing_user'
EMAIL = 'departing_user@example.com'


@pytest.fixture
def projects():
    return FakeCollection([
        # Live project, member recorded by username.
        {'_id': 1, 'current': True, 'project_name': 'by-username',
         'project_members': [USERNAME, 'someone_else'],
         'subscribers': ['someone_else@example.com']},
        # Live project, same person recorded by email address instead.
        {'_id': 2, 'current': True, 'project_name': 'by-email',
         'project_members': ['owner', EMAIL],
         'subscribers': [EMAIL, 'other@example.com']},
        # Live project they merely subscribed to.
        {'_id': 3, 'current': True, 'project_name': 'subscribed-only',
         'project_members': ['owner'],
         'subscribers': [EMAIL]},
        # A superseded version of project 1. Provenance -- must not be touched.
        {'_id': 4, 'current': False, 'project_name': 'by-username',
         'project_members': [USERNAME, 'someone_else'],
         'subscribers': [EMAIL]},
        # Someone else's project, to catch an over-broad query.
        {'_id': 5, 'current': True, 'project_name': 'unrelated',
         'project_members': ['owner'],
         'subscribers': ['other@example.com']},
    ])


@pytest.fixture
def preferences():
    return FakeCollection([
        {'_id': 1, 'email': EMAIL, 'onProjectUpdate': True},
        {'_id': 2, 'email': 'other@example.com', 'onProjectUpdate': True},
    ])


def _purge(projects, preferences, username=USERNAME, email=EMAIL):
    return purge_account_references(
        username, email,
        projects_collection=projects,
        preferences_collection=preferences)


def _doc(collection, _id):
    return next(d for d in collection.docs if d['_id'] == _id)


# ---------------------------------------------------------------------------
# Active records are cleaned up
# ---------------------------------------------------------------------------

def test_username_membership_is_removed(projects, preferences):
    _purge(projects, preferences)

    assert _doc(projects, 1)['project_members'] == ['someone_else']


def test_email_membership_is_removed(projects, preferences):
    """The half that the username-only cleanup used to miss entirely."""
    _purge(projects, preferences)

    assert _doc(projects, 2)['project_members'] == ['owner']


def test_subscriber_references_are_removed(projects, preferences):
    _purge(projects, preferences)

    assert _doc(projects, 2)['subscribers'] == ['other@example.com']
    assert _doc(projects, 3)['subscribers'] == []


def test_user_preferences_are_deleted(projects, preferences):
    _purge(projects, preferences)

    assert [d['email'] for d in preferences.docs] == ['other@example.com']


def test_other_users_are_left_alone(projects, preferences):
    _purge(projects, preferences)

    assert _doc(projects, 5)['project_members'] == ['owner']
    assert _doc(projects, 5)['subscribers'] == ['other@example.com']
    assert _doc(projects, 1)['subscribers'] == ['someone_else@example.com']


# ---------------------------------------------------------------------------
# Historical records are preserved
# ---------------------------------------------------------------------------

def test_superseded_project_versions_keep_their_membership(projects, preferences):
    """The provenance case, and the reason the queries filter on current: True.

    A previous version of a project records who its members were when that
    version was published. Deleting an account does not make that untrue.
    """
    _purge(projects, preferences)

    historical = _doc(projects, 4)
    assert historical['project_members'] == [USERNAME, 'someone_else']
    assert historical['subscribers'] == [EMAIL]


def test_purge_is_idempotent(projects, preferences):
    first = _purge(projects, preferences)
    second = _purge(projects, preferences)

    assert first['projects_updated'] > 0
    assert second['projects_updated'] == 0
    assert second['preferences_deleted'] == 0


def test_purge_without_identifiers_does_nothing(projects, preferences):
    before = [dict(d) for d in projects.docs]

    counts = purge_account_references(
        None, None,
        projects_collection=projects,
        preferences_collection=preferences)

    assert counts == {'projects_updated': 0, 'preferences_deleted': 0}
    assert projects.docs == before
    assert len(preferences.docs) == 2


# ---------------------------------------------------------------------------
# Wiring: the ORM side, and the signal that ties the two together
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_api_token_is_revoked_with_the_account():
    """Handled by the model relationship rather than by our own code.

    Token.user is a OneToOneField with on_delete=CASCADE, so this asserts the
    relationship still has that shape -- if it ever changed, deleted accounts
    would leave working API credentials behind.
    """
    from django.contrib.auth import get_user_model
    from rest_framework.authtoken.models import Token

    user = get_user_model().objects.create_user(
        username=f'token_cleanup_{uuid.uuid4().hex[:8]}',
        email=f'token_cleanup_{uuid.uuid4().hex[:8]}@example.com')
    token_key = Token.objects.create(user=user).key
    assert Token.objects.filter(key=token_key).exists()

    user.delete()

    assert not Token.objects.filter(key=token_key).exists()


@pytest.mark.integration
def test_deleting_a_user_triggers_the_purge(monkeypatch):
    """The cleanup hangs off post_delete, so /admin/ and the shell are covered too."""
    from django.contrib.auth import get_user_model
    from caper import account_signals

    calls = []
    monkeypatch.setattr(account_signals, 'purge_account_references',
                        lambda username, email, **kw: calls.append((username, email))
                        or {'projects_updated': 0, 'preferences_deleted': 0})

    suffix = uuid.uuid4().hex[:8]
    user = get_user_model().objects.create_user(
        username=f'signal_test_{suffix}',
        email=f'signal_test_{suffix}@example.com')
    user.delete()

    assert calls == [(f'signal_test_{suffix}', f'signal_test_{suffix}@example.com')]


@pytest.mark.integration
def test_end_to_end_against_a_real_mongo(request):
    """The same decisions, but with pymongo doing the work rather than the fake.

    The tests above use FakeCollection so the fixtures stay readable, which
    leaves the fake's ``$pull``/``$in`` semantics unverified against the real
    thing. This one deletes an actual Django user and checks an actual pair of
    MongoDB documents, so a divergence between the two shows up here.
    """
    from django.contrib.auth import get_user_model
    from caper.utils import (
        collection_handle_primary, db_handle_primary, get_collection_handle)

    prefs = get_collection_handle(db_handle_primary, 'user_preferences')

    suffix = uuid.uuid4().hex[:12]
    username = f'purge_e2e_{suffix}'
    email = f'purge_e2e_{suffix}@example.invalid'
    marker = f'purge-e2e-{suffix}'

    live = collection_handle_primary.insert_one({
        'project_name': marker, 'current': True, 'delete': True,
        'project_members': [username, 'coworker'],
        'subscribers': [email, 'coworker@example.invalid'],
    }).inserted_id
    historical = collection_handle_primary.insert_one({
        'project_name': marker, 'current': False, 'delete': True,
        'project_members': [username, 'coworker'],
        'subscribers': [email],
    }).inserted_id
    prefs.insert_one({'email': email, 'onProjectUpdate': True})

    def _cleanup():
        collection_handle_primary.delete_many({'project_name': marker})
        prefs.delete_many({'email': email})
    request.addfinalizer(_cleanup)

    get_user_model().objects.create_user(username=username, email=email).delete()

    live_doc = collection_handle_primary.find_one({'_id': live})
    assert live_doc['project_members'] == ['coworker']
    assert live_doc['subscribers'] == ['coworker@example.invalid']

    historical_doc = collection_handle_primary.find_one({'_id': historical})
    assert historical_doc['project_members'] == [username, 'coworker']
    assert historical_doc['subscribers'] == [email]

    assert prefs.find_one({'email': email}) is None


@pytest.mark.integration
def test_a_mongo_failure_does_not_break_the_deletion(monkeypatch):
    """The account is already gone by then; an outage must not surface as a 500."""
    from django.contrib.auth import get_user_model
    from caper import account_signals

    def _boom(*args, **kwargs):
        raise RuntimeError('mongo is down')

    monkeypatch.setattr(account_signals, 'purge_account_references', _boom)

    suffix = uuid.uuid4().hex[:8]
    user = get_user_model().objects.create_user(
        username=f'failure_test_{suffix}',
        email=f'failure_test_{suffix}@example.com')
    user_id = user.pk

    user.delete()  # must not raise

    assert not get_user_model().objects.filter(pk=user_id).exists()
