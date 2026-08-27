"""Tests for backfill_project_status.py.

Against a real MongoDB, in a throwaway database, rather than against a fake
collection.  Both corrections turn on query semantics a stand-in gets wrong:
``{'current': {'$exists': False}}`` distinguishing an absent field from a false
one, and the write preconditions comparing a stored array for exact equality.
Those semantics are the thing under test, so the database evaluates them.

CI provides mongo:4 at DB_URI_SECRET; locally it is the docker mongo.
"""

import json
import os
import uuid

import pytest
from bson import ObjectId
from pymongo import MongoClient

from caper.project_status import DETACHED, SOFT_DELETED, SUPERSEDED, classify
from backfill_project_status import (
    apply_current,
    apply_lineage,
    plan_current,
    plan_lineage,
)


@pytest.fixture
def projects():
    uri = os.getenv('DB_URI_SECRET')
    if not uri:
        pytest.skip('DB_URI_SECRET is not set')
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    name = 'caper-backfill-test-%s' % uuid.uuid4().hex[:8]
    try:
        client.admin.command('ping')
    except Exception as exc:
        pytest.skip('no MongoDB available: %s' % exc)
    try:
        yield client[name]['projects']
    finally:
        client.drop_database(name)


# ---------------------------------------------------------------------------
# The population, one document per shape that occurs in the collection
# ---------------------------------------------------------------------------

ORPHAN = ObjectId()          # delete=True, no 'current', nothing names it
MEMBER = ObjectId()          # delete=True, no 'current', named by HEAD
HEAD = ObjectId()            # live, names MEMBER in the current encoding
SUPERSEDED_DOC = ObjectId()  # delete=True, current=False -- already correct
REACHABLE = ObjectId()       # delete=False, current=False -- the group to leave
LIVE_DOC = ObjectId()
TOMBSTONE_DOC = ObjectId()   # markers decide it, whatever 'current' says
LEGACY_WRAPPED = ObjectId()  # previous_versions holding a JSON *string*
LEGACY_BARE = ObjectId()     # an unwrapped legacy object keyed 'link'
TARGET = ObjectId()          # what the two legacy entries point at


def seed(projects):
    projects.insert_many([
        {'_id': ORPHAN, 'project_name': 'orphan', 'delete': True},
        {'_id': MEMBER, 'project_name': 'member', 'delete': True},
        {'_id': HEAD, 'project_name': 'head', 'delete': False, 'current': True,
         'previous_versions': [{'date': '2024-05-01', 'linkid': str(MEMBER)}]},
        {'_id': SUPERSEDED_DOC, 'project_name': 'superseded',
         'delete': True, 'current': False},
        {'_id': REACHABLE, 'project_name': 'reachable',
         'delete': False, 'current': False},
        {'_id': LIVE_DOC, 'project_name': 'live', 'delete': False, 'current': True},
        {'_id': TOMBSTONE_DOC, 'project_name': 'tombstone', 'delete': True,
         'version_deleted_from_history': True, 'payload_purged': True},
        {'_id': LEGACY_WRAPPED, 'project_name': 'legacy wrapped', 'delete': False,
         'current': True,
         'previous_versions': [json.dumps([{'date': '2024-03-01',
                                           'link': str(TARGET)}])]},
        {'_id': LEGACY_BARE, 'project_name': 'legacy bare', 'delete': False,
         'current': True,
         'previous_versions': [{'date': '2024-03-02', 'link': str(TARGET)}]},
        {'_id': TARGET, 'project_name': 'target', 'delete': True, 'current': False},
    ])


def get(projects, oid):
    return projects.find_one({'_id': oid})


# ---------------------------------------------------------------------------
# Pass 1 -- the missing 'current' flag
# ---------------------------------------------------------------------------

def test_current_is_inferred_from_whether_anything_names_the_document(projects):
    seed(projects)
    plan = plan_current(projects)

    assert {doc['_id']: target for doc, target in plan} == {
        ORPHAN: SOFT_DELETED,
        MEMBER: SUPERSEDED,
    }, 'only documents with no current field at all are eligible'

    apply_current(projects, plan, execute=True)

    # Nothing names the orphan, so it was the head of its own chain when it was
    # deleted: recoverable from the admin page, which is the whole point.
    assert get(projects, ORPHAN)['current'] is True
    assert classify(get(projects, ORPHAN)) == SOFT_DELETED

    # HEAD names MEMBER, so MEMBER is a past version, not a deleted project.
    assert get(projects, MEMBER)['current'] is False
    assert classify(get(projects, MEMBER)) == SUPERSEDED


def test_the_reachable_and_already_correct_documents_are_left_alone(projects):
    seed(projects)
    before = {oid: get(projects, oid)
              for oid in (SUPERSEDED_DOC, REACHABLE, LIVE_DOC, TOMBSTONE_DOC)}

    apply_current(projects, plan_current(projects), execute=True)

    for oid, doc in before.items():
        assert get(projects, oid) == doc, '%s was modified' % oid

    # The delete=False, current=False group stays DETACHED on purpose: it is
    # reachable today and holds more than one kind of document.
    assert classify(get(projects, REACHABLE)) == DETACHED


def test_running_twice_writes_nothing_the_second_time(projects):
    seed(projects)
    apply_current(projects, plan_current(projects), execute=True)

    assert plan_current(projects) == [], 'the backfill is not idempotent'


def test_report_mode_writes_nothing(projects):
    seed(projects)
    plan = plan_current(projects)
    before = {doc['_id']: get(projects, doc['_id']) for doc, _target in plan}

    written, skipped = apply_current(projects, plan, execute=False)

    assert (written, skipped) == (0, 0)
    for oid, doc in before.items():
        assert get(projects, oid) == doc


def test_a_document_changed_since_the_plan_is_skipped_not_clobbered(projects):
    seed(projects)
    plan = plan_current(projects)

    # Someone deletes and restores the orphan between the report and the run,
    # which writes the flag the backfill was about to infer -- and writes the
    # opposite value, so a clobber is visible rather than coincidental.
    projects.update_one({'_id': ORPHAN}, {'$set': {'current': False}})

    written, skipped = apply_current(projects, plan, execute=True)

    assert get(projects, ORPHAN)['current'] is False, 'the concurrent write was lost'
    assert skipped == 1
    assert written == 1, 'the other document should still have been written'


# ---------------------------------------------------------------------------
# Pass 2 -- lineage entries in the encoding nothing reads
# ---------------------------------------------------------------------------

def test_both_legacy_encodings_are_rewritten_into_the_one_the_reader_uses(projects):
    seed(projects)
    plan = plan_lineage(projects)

    assert {doc['_id'] for doc, _rewritten in plan} == {LEGACY_WRAPPED, LEGACY_BARE}

    apply_lineage(projects, plan, execute=True)

    assert get(projects, LEGACY_WRAPPED)['previous_versions'] == [
        {'date': '2024-03-01', 'linkid': str(TARGET)}]
    assert get(projects, LEGACY_BARE)['previous_versions'] == [
        {'date': '2024-03-02', 'linkid': str(TARGET)}]


def test_the_rewrite_preserves_the_reference_and_the_fields_around_it(projects):
    projects.insert_one({
        '_id': LEGACY_BARE, 'project_name': 'carries tool versions',
        'delete': False, 'current': True,
        'previous_versions': [{'date': '2024-03-02', 'link': str(TARGET),
                               'AA_version': '1.3.r6', 'AC_version': '0.5.1'}],
    })

    apply_lineage(projects, plan_lineage(projects), execute=True)

    assert get(projects, LEGACY_BARE)['previous_versions'] == [{
        'date': '2024-03-02', 'linkid': str(TARGET),
        'AA_version': '1.3.r6', 'AC_version': '0.5.1',
    }], 'the rewrite dropped fields the history table renders'


def test_entries_already_in_the_current_encoding_are_not_rewritten(projects):
    seed(projects)
    before = get(projects, HEAD)

    apply_lineage(projects, plan_lineage(projects), execute=True)

    assert get(projects, HEAD) == before


def test_an_unreadable_entry_is_left_alone_rather_than_half_rewritten(projects):
    projects.insert_one({
        '_id': LEGACY_BARE, 'project_name': 'mixed', 'delete': False,
        'current': True,
        'previous_versions': [
            {'date': '2024-03-02', 'link': str(TARGET)},
            'not json and not an id',
        ],
    })

    plan = plan_lineage(projects)

    assert plan == [], (
        'rewriting the array would preserve the readable entry and cement the '
        'unreadable one as a linkid holding prose')


def test_lineage_rewrite_is_idempotent(projects):
    seed(projects)
    apply_lineage(projects, plan_lineage(projects), execute=True)

    assert plan_lineage(projects) == []
