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

from caper.project_status import (
    DETACHED, LIVE as LIVE_LABEL, SOFT_DELETED, SUPERSEDED, classify)
from backfill_project_status import (
    apply_current,
    apply_lineage,
    apply_pointers,
    apply_status,
    plan_current,
    plan_lineage,
    plan_pointers,
    plan_status,
    take,
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


# ---------------------------------------------------------------------------
# The undo record
# ---------------------------------------------------------------------------

def test_replaying_the_undo_record_restores_every_document(projects, tmp_path):
    seed(projects)
    before = {doc['_id']: doc for doc in projects.find({})}

    path = tmp_path / 'undo.jsonl'
    with open(path, 'x') as rollback:
        apply_current(projects, plan_current(projects), execute=True, rollback=rollback)
        apply_lineage(projects, plan_lineage(projects), execute=True, rollback=rollback)

    assert projects.find_one({'_id': ORPHAN})['current'] is True, 'nothing was written'

    # Replaying is a for-loop over the file, which is the claim the record
    # makes: no tool is needed to get back, only the file.
    for line in open(path):
        undo = json.loads(line)
        projects.update_one({'_id': ObjectId(undo['_id'])},
                            {undo['op']: undo['fields']})

    assert {doc['_id']: doc for doc in projects.find({})} == before


# ---------------------------------------------------------------------------
# --limit, which is how this runs against prod: a few documents, checked, then
# the rest
# ---------------------------------------------------------------------------

def test_a_limited_run_covers_the_rest_on_the_next_run_with_no_overlap(projects):
    """The property that makes staging safe.

    Splitting a backfill into a small first batch is only worth doing if the
    second run resumes exactly where the first stopped.  It does, but not
    because anything remembers: a document that has been written no longer
    matches the query that selects it, so the plan itself is what shrinks.
    The sort is what stops the two runs from picking documents in different
    orders and leaving one behind.
    """
    seed(projects)
    everything = [doc['_id'] for doc, _target in plan_current(projects)]
    assert len(everything) > 1, 'need at least two eligible documents to split'

    first = take(plan_current(projects), 1)
    assert [doc['_id'] for doc, _target in first] == everything[:1]
    apply_current(projects, first, execute=True)

    rest = plan_current(projects)
    assert [doc['_id'] for doc, _target in rest] == everything[1:], \
        'the second run must pick up exactly what the first left'

    apply_current(projects, rest, execute=True)
    assert plan_current(projects) == []


def test_take_without_a_limit_is_the_whole_plan(projects):
    seed(projects)
    plan = plan_current(projects)

    assert take(plan, None) == plan
    assert take(plan, len(plan) + 5) == plan

    # 0 means zero documents, not "no limit". Someone typing --limit 0 to make
    # a run do nothing must not get all 70 instead.
    assert take(plan, 0) == []


# ---------------------------------------------------------------------------
# Pass 3 -- lineage pointers
# ---------------------------------------------------------------------------

def test_pointers_are_read_off_the_heads_list(projects):
    """Ordinals, neighbours and is_latest all come from one place.

    The head's previous_versions[] is the order the application wrote -- each
    new version is created with the old list plus the version it replaces -- so
    no date is consulted. Dates are missing on some documents and tied on
    others; the list is not.
    """
    v1, v2, head = ObjectId(), ObjectId(), ObjectId()
    projects.insert_many([
        {'_id': v1, 'project_name': 'v1', 'delete': True, 'current': False},
        {'_id': v2, 'project_name': 'v2', 'delete': True, 'current': False,
         'previous_versions': [{'linkid': str(v1)}]},
        {'_id': head, 'project_name': 'head', 'delete': False, 'current': True,
         'previous_versions': [{'linkid': str(v1)}, {'linkid': str(v2)}]},
    ])

    plan, refused = plan_pointers(projects)
    assert refused == []
    apply_pointers(projects, plan, execute=True)

    ordered = [get(projects, oid) for oid in (v1, v2, head)]
    assert [d['version_ordinal'] for d in ordered] == [1, 2, 3]
    assert [d['is_latest'] for d in ordered] == [False, False, True]
    assert [d['previous_version_id'] for d in ordered] == [None, v1, v2]
    assert [d['next_version_id'] for d in ordered] == [v2, head, None]

    # The oldest version names the chain: derivable, so a re-run computes the
    # same value, and stable, because new versions are appended at the far end.
    assert {d['version_chain_id'] for d in ordered} == {v1}


def test_a_document_with_no_lineage_is_its_own_chain(projects):
    alone = ObjectId()
    projects.insert_one({'_id': alone, 'project_name': 'alone',
                         'delete': False, 'current': True})

    plan, refused = plan_pointers(projects)
    apply_pointers(projects, plan, execute=True)

    doc = get(projects, alone)
    assert doc['version_chain_id'] == alone
    assert doc['version_ordinal'] == 1
    assert doc['is_latest'] is True
    assert doc['previous_version_id'] is None
    assert doc['next_version_id'] is None
    assert refused == []


def test_a_chain_with_two_possible_heads_is_refused_whole(projects):
    """Two documents nobody names is a fork, and a fork is a finding.

    Dev has four of these. Guessing which branch is the real history would
    write a lineage nobody can check afterwards, so the whole chain is left
    alone -- not just the ambiguous part, because the ordinals of the members
    below the fork depend on which branch wins.
    """
    shared, branch_a, branch_b = ObjectId(), ObjectId(), ObjectId()
    projects.insert_many([
        {'_id': shared, 'project_name': 'shared ancestor',
         'delete': True, 'current': False},
        {'_id': branch_a, 'project_name': 'branch a', 'delete': False,
         'current': True, 'previous_versions': [{'linkid': str(shared)}]},
        {'_id': branch_b, 'project_name': 'branch b', 'delete': False,
         'current': True, 'previous_versions': [{'linkid': str(shared)}]},
    ])

    plan, refused = plan_pointers(projects)

    assert plan == []
    assert len(refused) == 1
    members, reason = refused[0]
    assert set(members) == {str(shared), str(branch_a), str(branch_b)}
    assert '2 possible heads' in reason

    apply_pointers(projects, plan, execute=True)
    for oid in (shared, branch_a, branch_b):
        assert 'version_chain_id' not in get(projects, oid)


def test_a_chain_the_head_does_not_fully_list_is_refused(projects):
    """Reached through a longer path than the head's own list.

    The members are connected, so they are one chain, but the head's list is
    not a complete ordering of it and nothing else in the data is either.
    """
    v1, v2, head = ObjectId(), ObjectId(), ObjectId()
    projects.insert_many([
        {'_id': v1, 'project_name': 'v1', 'delete': True, 'current': False},
        {'_id': v2, 'project_name': 'v2', 'delete': True, 'current': False,
         'previous_versions': [{'linkid': str(v1)}]},
        # Names v2 but not v1, so v1 is in the chain by way of v2 alone.
        {'_id': head, 'project_name': 'head', 'delete': False, 'current': True,
         'previous_versions': [{'linkid': str(v2)}]},
    ])

    plan, refused = plan_pointers(projects)

    assert plan == []
    assert len(refused) == 1
    assert 'longer path' in refused[0][1]


def test_pointers_are_idempotent(projects):
    seed(projects)
    plan, _refused = plan_pointers(projects)
    apply_pointers(projects, plan, execute=True)

    again, _refused = plan_pointers(projects)
    assert again == [], 'the pointer pass is not idempotent'


def test_exactly_one_is_latest_per_chain(projects):
    """Invariant I3, asserted over whatever the seed population produces."""
    seed(projects)
    plan, _refused = plan_pointers(projects)
    apply_pointers(projects, plan, execute=True)

    heads = {}
    for doc in projects.find({'version_chain_id': {'$exists': True}}):
        heads.setdefault(doc['version_chain_id'], []).append(doc['is_latest'])
    assert heads, 'nothing was written'
    for chain_id, flags in heads.items():
        assert flags.count(True) == 1, 'chain %s has %d heads' % (
            chain_id, flags.count(True))


def test_ordinals_are_contiguous_from_one_within_a_chain(projects):
    """Invariant I4."""
    seed(projects)
    plan, _refused = plan_pointers(projects)
    apply_pointers(projects, plan, execute=True)

    chains = {}
    for doc in projects.find({'version_chain_id': {'$exists': True}}):
        chains.setdefault(doc['version_chain_id'], []).append(doc['version_ordinal'])
    for chain_id, ordinals in chains.items():
        assert sorted(ordinals) == list(range(1, len(ordinals) + 1)), \
            'chain %s has ordinals %s' % (chain_id, sorted(ordinals))


def test_the_pointer_undo_record_removes_every_field_it_wrote(projects):
    seed(projects)
    plan, _refused = plan_pointers(projects)
    before = {str(doc['_id']): get(projects, doc['_id']) for doc, _f in plan}

    import io
    undo = io.StringIO()
    apply_pointers(projects, plan, execute=True, rollback=undo)

    for line in undo.getvalue().splitlines():
        entry = json.loads(line)
        assert entry['op'] == '$unset'
        projects.update_one({'_id': ObjectId(entry['_id'])},
                            {'$unset': entry['fields']})

    for doc_id, original in before.items():
        assert get(projects, ObjectId(doc_id)) == original


# ---------------------------------------------------------------------------
# Pass 4 -- the stored status field
# ---------------------------------------------------------------------------

def test_status_is_written_from_classify(projects):
    seed(projects)
    plan = plan_status(projects)
    apply_status(projects, plan, execute=True)

    for doc in projects.find({}):
        assert doc['status'] == classify(doc)


def test_status_is_not_rewritten_without_refresh(projects):
    """The default pass fills absences only.

    Overwriting a stored value is a different risk class from filling a gap, so
    it takes a flag -- and without one, a re-run finds nothing to do.
    """
    seed(projects)
    apply_status(projects, plan_status(projects), execute=True)
    stale = ObjectId()
    projects.insert_one({'_id': stale, 'project_name': 'stale',
                         'delete': False, 'current': True, 'status': 'SUPERSEDED'})

    assert [doc['_id'] for doc, _s, _c in plan_status(projects)] == [stale] or \
        plan_status(projects) == []
    # The stale document is not in the default plan; only genuinely absent ones.
    assert all(stored is None for _doc, stored, _c in plan_status(projects))
    assert get(projects, stale)['status'] == 'SUPERSEDED'


def test_refresh_corrects_a_status_that_went_stale(projects):
    """The window this exists to close.

    A deployment running code from before status_after() writes 'delete' on a
    soft delete and leaves 'status' behind. The backfill must be able to repair
    a disagreement it can itself create by running ahead of the deploy.
    """
    stale = ObjectId()
    projects.insert_one({'_id': stale, 'project_name': 'went stale',
                         'delete': True, 'current': True, 'status': LIVE_LABEL})

    plan = plan_status(projects, refresh=True)
    assert [(doc['_id'], stored, computed) for doc, stored, computed in plan] == \
        [(stale, LIVE_LABEL, SOFT_DELETED)]

    apply_status(projects, plan, execute=True)
    assert get(projects, stale)['status'] == SOFT_DELETED


def test_the_refresh_undo_puts_the_stale_value_back(projects):
    """The inverse of a correction is the value that was there, not an unset."""
    import io
    stale = ObjectId()
    projects.insert_one({'_id': stale, 'project_name': 'went stale',
                         'delete': True, 'current': True, 'status': LIVE_LABEL})

    undo = io.StringIO()
    apply_status(projects, plan_status(projects, refresh=True),
                 execute=True, rollback=undo)

    entry = json.loads(undo.getvalue().strip())
    assert entry == {'_id': str(stale), 'op': '$set', 'fields': {'status': LIVE_LABEL}}
    projects.update_one({'_id': stale}, {'$set': entry['fields']})
    assert get(projects, stale)['status'] == LIVE_LABEL
