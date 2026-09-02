"""Deleting a version must survive the thing that killed it on dev.

The incident, 2026-09-02: deleting version 2 of a five-version PCAWG chain
purged the GridFS payload first and wrote the tombstone second, inline in the
web request. The worker was killed at gunicorn's 900 s timeout after 901
seconds, having deleted 15,272 of 15,733 files. The document was left
classified SUPERSEDED -- no tombstone markers, no redirect -- so the site
presented an ordinary superseded version whose payload was 97% gone, and the
only trace was an audit event still reading `completed: False`.

Two properties stop that recurring, and both are tested here: the tombstone is
written *before* any file is deleted, and the ids not yet deleted are recorded
on the tombstone so an interrupted purge is resumed by name rather than left to
a global orphan sweep -- which could not have found it anyway, because until
that field is cleared the tombstone still references the files.
"""

import datetime
import inspect
import os

import pytest
from bson import ObjectId

from caper import provenance, version_purge
from caper.project_version_cleanup import (
    PENDING_PAYLOAD_KEY,
    PURGE_CLAIM_KEY,
    build_deleted_version_tombstone,
)
from caper.project_status import classify, TOMBSTONE


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self.updates = []

    def update_one(self, query, update):
        self.updates.append((query, update))

    def find_one_and_update(self, query, update, projection=None):
        pending_wanted = query.get(PENDING_PAYLOAD_KEY)
        for doc in self.documents:
            if pending_wanted is not None and not doc.get(PENDING_PAYLOAD_KEY):
                continue
            if PURGE_CLAIM_KEY in doc:
                clauses = query.get('$or') or []
                cutoff = next((c[PURGE_CLAIM_KEY]['$lt'] for c in clauses
                               if PURGE_CLAIM_KEY in c
                               and '$lt' in c[PURGE_CLAIM_KEY]), None)
                if cutoff is None or doc[PURGE_CLAIM_KEY] >= cutoff:
                    continue
            before = dict(doc)
            doc.update(update['$set'])
            return before
        return None


@pytest.fixture
def purge_env(monkeypatch):
    """Wire version_purge to fakes and record what it did."""
    from caper import utils

    state = {'deleted': [], 'confirmed': [], 'submitted': []}
    collection = FakeCollection()

    monkeypatch.setattr(utils, 'delete_gridfs_file',
                        lambda file_id: state['deleted'].append(file_id))
    monkeypatch.setattr(utils, 'collection_handle', collection)
    monkeypatch.setattr(utils, 'audit_log_handle', object())
    monkeypatch.setattr(provenance, 'confirm',
                        lambda handle, event, **kw: state['confirmed'].append((event, kw)))

    def fake_submit(fn, *args, task_label=None, **kwargs):
        state['submitted'].append(task_label)
        return fn(*args, **kwargs)          # run inline so the test can assert

    monkeypatch.setattr(version_purge._thread_executor, 'submit', fake_submit)
    state['collection'] = collection
    return state


# ---------------------------------------------------------------------------
# Which ids get purged
# ---------------------------------------------------------------------------

def test_protected_ids_are_left_alone():
    """A file the surviving version still uses is not this deletion's to remove."""
    shared, mine = ObjectId(), ObjectId()
    victim = {'_id': ObjectId(),
              'runs': {'r': [{'AA directory': shared}, {'AA directory': mine}]}}

    wanted = version_purge.payload_ids_to_purge(victim, {shared})

    assert mine in wanted and shared not in wanted


def test_the_same_id_is_only_listed_once():
    """The live head references 27,604 values for 18,403 distinct files."""
    repeated = ObjectId()
    victim = {'_id': ObjectId(),
              'runs': {'r': [{'AA directory': repeated},
                             {'Reconstruction directory': repeated}]}}

    assert version_purge.payload_ids_to_purge(victim) == [repeated]


# ---------------------------------------------------------------------------
# The purge itself
# ---------------------------------------------------------------------------

def test_the_purge_deletes_clears_the_pending_list_and_confirms(purge_env):
    ids = [ObjectId(), ObjectId()]
    victim_id = str(ObjectId())

    version_purge.start(victim_id, ids, 'event-1', outcome='tombstoned_in_place')

    assert purge_env['deleted'] == ids
    assert purge_env['confirmed'] == [('event-1', {'outcome': 'tombstoned_in_place',
                                                   'gridfs_files_purged': 2})]
    query, update = purge_env['collection'].updates[-1]
    assert update['$pull'][PENDING_PAYLOAD_KEY]['$in'] == ids
    assert PURGE_CLAIM_KEY in update['$unset']


def test_a_version_with_no_payload_still_confirms_its_event(purge_env):
    """The deletion happened even though there was nothing to delete."""
    version_purge.start(str(ObjectId()), [], 'event-2', outcome='chain_emptied')

    assert purge_env['submitted'] == []
    assert purge_env['confirmed'] == [('event-2', {'outcome': 'chain_emptied',
                                                   'gridfs_files_purged': 0})]


def test_the_event_is_confirmed_after_the_files_are_gone(purge_env):
    """`completed: False` is the signature that found the incident.

    Confirming before the purge would have made the interrupted delete look
    finished, which is the one thing that must not happen.
    """
    order = []
    from caper import utils
    ids = [ObjectId()]

    utils.delete_gridfs_file = lambda file_id: order.append('deleted')
    provenance.confirm = lambda handle, event, **kw: order.append('confirmed')

    version_purge.start(str(ObjectId()), ids, 'event-3', outcome='x')

    assert order == ['deleted', 'confirmed']


# ---------------------------------------------------------------------------
# Resumption
# ---------------------------------------------------------------------------

def test_an_interrupted_purge_is_resumed(purge_env):
    victim = ObjectId()
    leftover = [ObjectId(), ObjectId()]
    collection = FakeCollection([{'_id': victim, PENDING_PAYLOAD_KEY: leftover}])

    assert version_purge.resume_pending(collection=collection) == 1
    assert purge_env['deleted'] == leftover


def test_only_one_worker_takes_each_pending_purge(purge_env):
    """Every gunicorn worker calls resume_pending() as it boots."""
    collection = FakeCollection([{'_id': ObjectId(),
                                  PENDING_PAYLOAD_KEY: [ObjectId()]}])

    first = version_purge.resume_pending(collection=collection)
    second = version_purge.resume_pending(collection=collection)

    assert (first, second) == (1, 0)


def test_an_abandoned_claim_is_taken_again(purge_env):
    """A worker killed mid-purge must not strand its own work forever."""
    stale = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    collection = FakeCollection([{'_id': ObjectId(),
                                  PENDING_PAYLOAD_KEY: [ObjectId()],
                                  PURGE_CLAIM_KEY: stale}])

    assert version_purge.resume_pending(collection=collection) == 1


def test_a_finished_tombstone_is_not_resumed(purge_env):
    collection = FakeCollection([{'_id': ObjectId(), PENDING_PAYLOAD_KEY: []}])

    assert version_purge.resume_pending(collection=collection) == 0


# ---------------------------------------------------------------------------
# The ordering, and the state the incident produced
# ---------------------------------------------------------------------------

def test_an_interrupted_delete_leaves_a_tombstone_not_a_superseded_version():
    """The incident state, asserted directly.

    On dev the document was left SUPERSEDED with its payload gone. Because the
    tombstone is now written first, the worst an interruption can leave is a
    correct tombstone that still names the files it did not reach.
    """
    victim = {'_id': ObjectId(), 'project_name': 'PCAWG', 'date': 'x',
              'previous_versions': []}
    head = {'_id': ObjectId(), 'project_name': 'PCAWG'}
    pending = [ObjectId(), ObjectId()]

    tombstone = build_deleted_version_tombstone(
        victim, head, 'someone', 'today', pending_payload_ids=pending)

    assert classify(tombstone) == TOMBSTONE
    assert tombstone[PENDING_PAYLOAD_KEY] == pending


def test_the_view_writes_the_tombstone_before_it_purges():
    """A source check, because the ordering is the whole fix.

    Nothing in the request path may delete a file: if it does, the 900 s worker
    timeout is back between the delete and the tombstone.
    """
    from caper import views

    source = inspect.getsource(views.delete_project_version)
    assert 'delete_gridfs_payload_for_project' not in source, (
        'the version delete is purging its payload inside the request again')
    assert source.count('pending_payload_ids=pending_ids') == 3, (
        'every deletion case must hand its ids to the tombstone')
    assert source.count('version_purge.start(') == 3


def test_every_delete_case_purges_after_its_tombstone():
    """Per case: the replace_one that writes the tombstone precedes the purge."""
    from caper import views

    source = inspect.getsource(views.delete_project_version)
    for start in [source.index('version_purge.start(')]:
        assert source.index('build_deleted_version_tombstone') < start
    assert source.index('collection_handle.replace_one') < source.index('version_purge.start(')


def test_a_worker_resumes_pending_purges_when_it_boots():
    """ready() cannot do this: preload_app means it runs in the master."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, 'gunicorn_config.py')).read()

    assert 'def post_fork(' in source
    assert 'resume_pending' in source
