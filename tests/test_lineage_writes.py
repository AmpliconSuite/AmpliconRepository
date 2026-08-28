"""Tests for the write half of caper.lineage and the paths that call it.

One rule governs all of it, and every test here is a consequence of it:

    Pointers are structure.  is_latest is position.  status is state.

A deletion never rewrites a pointer and never renumbers an ordinal.  The
deleted version stays a node of the chain whose payload is gone -- which is
what keeps its URL resolving, and what lets a history table say "version 2
deleted, version 3 current" instead of quietly rendering two versions where
there were three.

The transition names (T1, T2, T3, T7, T8, T9) are the spec's, kept because the
shapes are easier to talk about with names than with prose.
"""

import pytest
from bson import ObjectId

from caper import lineage, utils, views
from caper.project_status import classify, TOMBSTONE, SUPERSEDED, LIVE
from caper.project_version_cleanup import (
    build_deleted_version_tombstone,
    iter_gridfs_file_ids,
)

from test_project_version_cleanup import FakeGridFS, FakeHistoryCollection


@pytest.fixture
def tombstone_marks():
    """What makes classify() call a document a TOMBSTONE."""
    return {'delete': True, 'current': False,
            'version_deleted_from_history': True, 'payload_purged': True}


def version(chain, ordinal, previous=None, is_latest=False, **extra):
    doc = {'_id': ObjectId(), 'version_chain_id': chain,
           'version_ordinal': ordinal, 'is_latest': is_latest,
           'previous_version_id': previous, 'next_version_id': None,
           'project_name': 'p', 'date': f'2026-0{min(ordinal, 9)}-01T00:00:00.000000'}
    doc.update(extra)
    return doc


def linked(count, **head_extra):
    """A chain of *count* versions with every pointer wired up."""
    members = []
    chain = None
    for ordinal in range(1, count + 1):
        previous = members[-1]['_id'] if members else None
        doc = version(chain, ordinal, previous, is_latest=(ordinal == count))
        if chain is None:
            chain = doc['_id']
            doc['version_chain_id'] = chain
        if members:
            members[-1]['next_version_id'] = doc['_id']
        members.append(doc)
    members[-1].update(head_extra)
    return members


# ---------------------------------------------------------------------------
# T1 / T9 -- a new version joins the chain
# ---------------------------------------------------------------------------

def test_a_project_with_no_history_becomes_a_chain_of_one():
    new_id = ObjectId()
    fields, predecessor = lineage.plan_new_version([], new_id)
    assert predecessor is None
    assert fields == {'version_chain_id': new_id, 'previous_version_id': None,
                      'next_version_id': None, 'version_ordinal': 1,
                      'is_latest': True}


def test_the_chain_is_named_by_its_oldest_member_so_a_rebuild_agrees():
    """Derivable from the data rather than minted.

    The backfill computes the same value from previous_versions[]; if the id
    were random the two would never agree and there would be nothing to check.
    """
    members = linked(3)
    fields, _ = lineage.plan_new_version(members, ObjectId())
    assert fields['version_chain_id'] == members[0]['_id']


def test_a_new_version_is_appended_after_the_highest_ordinal():
    members = linked(3)
    new_id = ObjectId()
    fields, (predecessor_id, predecessor_fields) = \
        lineage.plan_new_version(members, new_id)

    assert fields['version_ordinal'] == 4
    assert fields['previous_version_id'] == members[-1]['_id']
    assert fields['is_latest'] is True
    assert predecessor_id == members[-1]['_id']
    assert predecessor_fields == {'next_version_id': new_id, 'is_latest': False}


def test_ordinals_count_from_the_high_water_mark_not_the_member_count():
    """A chain that has had a version deleted still contains it.

    Ordinals are identity, so they are never reused. Counting from len(members)
    would hand the new version an ordinal some other version already holds the
    moment anything is ever removed from the collection.
    """
    members = linked(3)
    members.pop(1)                       # as if ordinal 2 were gone entirely
    fields, _ = lineage.plan_new_version(members, ObjectId())
    assert fields['version_ordinal'] == 4


def test_the_predecessor_may_be_a_tombstone(tombstone_marks):
    """T9: re-populating an emptied project.

    A deleted version is a legitimate previous_version_id. The history then
    reads "version 1 deleted, version 2 current" -- true, and the thing the
    array encoding cannot express at all.
    """
    members = linked(1)
    members[0].update(tombstone_marks)
    fields, (predecessor_id, _) = lineage.plan_new_version(members, ObjectId())
    assert predecessor_id == members[0]['_id']
    assert fields['version_ordinal'] == 2


# ---------------------------------------------------------------------------
# Finding the chain to extend -- and refusing to guess
# ---------------------------------------------------------------------------

def test_no_predecessors_means_a_new_chain():
    assert lineage.predecessor_chain(FakeHistoryCollection([]), []) == []
    assert lineage.predecessor_chain(FakeHistoryCollection([]), None) == []


def test_an_unpointered_predecessor_leaves_the_new_version_unpointered():
    """The safety property the read switch was measured against.

    Pointering the new version alone would strand its predecessors: it would
    read its history from a chain of one and render a project with no history,
    while the array it inherited names every version it has. Leaving both on
    the array path keeps them together, and the backfill is what gives such a
    chain pointers. 26 documents on dev are in this state, 0 on prod.
    """
    old = {'_id': ObjectId(), 'project_name': 'p'}
    collection = FakeHistoryCollection([old])
    assert lineage.predecessor_chain(
        collection, [{'linkid': str(old['_id'])}]) is None


def test_references_spanning_two_chains_are_refused_rather_than_picked_from():
    first, second = linked(1), linked(1)
    collection = FakeHistoryCollection(first + second)
    assert lineage.predecessor_chain(
        collection, [{'linkid': str(first[0]['_id'])},
                     {'linkid': str(second[0]['_id'])}]) is None


def test_the_chain_is_read_from_the_pointers_not_from_the_array():
    """The array names one member; the chain read back has all of them.

    This is what makes "nothing orders previous_versions[]" stop mattering:
    the predecessor is the highest ordinal in the chain, however the array that
    led there happened to be sorted.
    """
    members = linked(3)
    collection = FakeHistoryCollection(members)
    found = lineage.predecessor_chain(
        collection, [{'linkid': str(members[0]['_id'])}])
    assert [d['_id'] for d in found] == [d['_id'] for d in members]


def test_link_new_version_writes_both_ends():
    members = linked(2)
    new_id = ObjectId()
    collection = FakeHistoryCollection(members + [{'_id': new_id, 'project_name': 'p'}])

    written = lineage.link_new_version(
        collection, new_id, [{'linkid': str(members[0]['_id'])}])

    assert written['version_ordinal'] == 3
    new_doc = collection.docs[str(new_id)]
    assert new_doc['previous_version_id'] == members[-1]['_id']
    assert new_doc['is_latest'] is True
    old_head = collection.docs[str(members[-1]['_id'])]
    assert old_head['is_latest'] is False
    assert old_head['next_version_id'] == new_id


def test_link_new_version_leaves_a_refused_chain_completely_alone():
    old = {'_id': ObjectId(), 'project_name': 'p'}
    new_id = ObjectId()
    collection = FakeHistoryCollection([old, {'_id': new_id, 'project_name': 'p'}])

    assert lineage.link_new_version(
        collection, new_id, [{'linkid': str(old['_id'])}]) == {}
    assert 'version_chain_id' not in collection.docs[str(new_id)]
    assert 'is_latest' not in collection.docs[str(old['_id'])]


def test_unlink_restores_the_predecessor_as_head():
    """The rollback path: aggregation failed, the old version comes back.

    Without this the chain keeps a head the user was told had failed.
    """
    members = linked(2)
    new_id = ObjectId()
    collection = FakeHistoryCollection(members + [{'_id': new_id, 'project_name': 'p'}])
    lineage.link_new_version(collection, new_id, [{'linkid': str(members[0]['_id'])}])

    lineage.unlink_new_version(collection, new_id)

    assert 'version_chain_id' not in collection.docs[str(new_id)]
    assert 'version_ordinal' not in collection.docs[str(new_id)]
    restored = collection.docs[str(members[-1]['_id'])]
    assert restored['is_latest'] is True
    assert restored['next_version_id'] is None


def test_unlink_is_a_no_op_on_a_document_that_was_never_linked():
    doc = {'_id': ObjectId(), 'project_name': 'p'}
    collection = FakeHistoryCollection([doc])
    assert lineage.unlink_new_version(collection, doc['_id']) == {}


# ---------------------------------------------------------------------------
# T2 / T3 / T7 / T8 -- planning a deletion
# ---------------------------------------------------------------------------

def test_deleting_a_middle_version_touches_nothing_else():
    """T2. The head is untouched, the neighbours are untouched, and the victim
    keeps the ordinal it has always had -- renumbering would move every
    downstream version and invalidate every audit event naming the old one."""
    members = linked(3)
    plan = lineage.plan_deletion(members, members[1]['_id'])
    assert plan.promoted_id is None
    assert plan.victim_keeps_head is False
    assert plan.chain_emptied is False


def test_deleting_the_head_promotes_the_highest_surviving_ordinal():
    members = linked(3)
    plan = lineage.plan_deletion(members, members[2]['_id'])
    assert plan.promoted_id == members[1]['_id']
    assert plan.victim_keeps_head is False


def test_promotion_skips_versions_that_are_already_tombstones(tombstone_marks):
    """T7 in its non-terminal form: the version below the head is gone."""
    members = linked(3)
    members[1].update(tombstone_marks)
    plan = lineage.plan_deletion(members, members[2]['_id'])
    assert plan.promoted_id == members[0]['_id']


def test_promotion_ignores_the_order_the_array_happened_to_be_in():
    """The hazard this replaces: promotion used to be previous_versions[-1].

    Nothing orders that array, and production has same-day version pairs where
    a date sort ties as well. Here the chain arrives with its members shuffled
    and the answer is still the highest ordinal.
    """
    members = linked(3)
    shuffled = [members[1], members[2], members[0]]
    plan = lineage.plan_deletion(shuffled, members[2]['_id'])
    assert plan.promoted_id == members[1]['_id']


def test_deleting_the_last_survivor_leaves_it_holding_the_head(tombstone_marks):
    """T7. An empty project still has a current version to render and restore
    into; it just has no payload behind it."""
    members = linked(2)
    members[0].update(tombstone_marks)
    plan = lineage.plan_deletion(members, members[1]['_id'])
    assert plan.promoted_id is None
    assert plan.victim_keeps_head is True
    assert plan.chain_emptied is True


def test_deleting_the_only_version_is_the_same_shape():
    """T8, the degenerate case of T7 -- and the one the old code got wrong."""
    members = linked(1)
    plan = lineage.plan_deletion(members, members[0]['_id'])
    assert plan.promoted_id is None
    assert plan.victim_keeps_head is True
    assert plan.chain_emptied is True


def test_there_is_no_plan_without_pointers():
    """The signal to the caller that it should keep doing what it did before."""
    assert lineage.plan_deletion(None, ObjectId()) is None
    assert lineage.plan_deletion([], ObjectId()) is None
    assert lineage.plan_deletion(linked(2), ObjectId()) is None


def test_pointer_fields_carries_every_one_of_them():
    """A tombstone is written with replace_one, so anything not named is lost.

    Dropping these is exactly why both tombstones on production sit in chains
    of their own, invisible to every pointer read.
    """
    members = linked(2)
    fields = lineage.pointer_fields(members[1], is_latest=False)
    assert set(fields) == set(lineage.POINTER_FIELDS)
    assert fields['version_ordinal'] == 2
    assert fields['previous_version_id'] == members[0]['_id']
    assert fields['is_latest'] is False
    assert lineage.pointer_fields({'_id': ObjectId()}) == {}


# ---------------------------------------------------------------------------
# Through delete_project_version() -- the write paths themselves
# ---------------------------------------------------------------------------

def payload(doc):
    doc['tarfile'] = ObjectId()
    return doc


def chain_project(count, member_username):
    """*count* linked versions with payloads and the array kept in step."""
    members = linked(count)
    for ordinal, member in enumerate(members, start=1):
        member.update({'private': 'private', 'project_members': [member_username],
                       'delete': ordinal != count, 'current': ordinal == count,
                       'runs': {}, 'AA_version': f'AA-{ordinal}'})
        payload(member)
    members[-1]['previous_versions'] = [
        {'date': m['date'], 'linkid': str(m['_id']), 'AA_version': m['AA_version']}
        for m in members[:-1]]
    return members


def run_delete(monkeypatch, request_factory, user, members, victim):
    collection = FakeHistoryCollection(members)
    fs = FakeGridFS()
    monkeypatch.setattr(utils, 'collection_handle', collection)
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'delete_gridfs_file', fs.delete)

    head_id = str(members[-1]['_id'])
    request = request_factory.post(f'/project/{head_id}/delete_version/{victim}')
    request.user = user
    response = views.delete_project_version(request, head_id, str(victim))
    return response, collection, fs


def test_deleting_the_only_version_purges_the_payload_and_writes_a_tombstone(
        monkeypatch, request_factory, test_user):
    """T8, and the reason invariant I18 exists.

    This path used to spell the tombstone marker by hand and skip everything
    else: no GridFS purge, no payload_purged, no redirect. The document it left
    classified as SUPERSEDED and stayed resolvable with its entire payload
    still stored and still billed, while the log line said the project was
    fully removed. Measured on prod 2026-08-27: 0 documents in that state, so
    the path was unexercised rather than damaging -- but it was not correct.
    """
    members = chain_project(1, test_user.username)
    tarfile_id = members[0]['tarfile']

    response, collection, fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[0]['_id'])

    assert response.status_code == 200
    tombstone = collection.docs[str(members[0]['_id'])]
    assert classify(tombstone) == TOMBSTONE
    assert tombstone['payload_purged'] is True
    assert str(tarfile_id) in fs.deleted
    # Nowhere to redirect to: the URL resolves to the deleted version rather
    # than forwarding, which is the difference between an empty project and an
    # absent one.
    assert 'redirect_to_project' not in tombstone
    # And it is still the chain's position-in-time, so the project can be
    # restored into and re-populated.
    assert tombstone['is_latest'] is True
    assert tombstone['version_chain_id'] == members[0]['version_chain_id']
    assert tombstone['version_ordinal'] == 1


def test_the_sole_version_path_no_longer_leaves_a_resolvable_superseded_document(
        monkeypatch, request_factory, test_user):
    """The precise defect, stated as the state it used to produce.

    Also invariants I8 and I14: a tombstone names no GridFS file. The document
    it left behind was SUPERSEDED, reachable by URL, and still named every file
    it had ever uploaded -- which is what made "project fully removed" false in
    both directions at once.
    """
    members = chain_project(1, test_user.username)
    _response, collection, _fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[0]['_id'])

    left = collection.docs[str(members[0]['_id'])]
    assert classify(left) != SUPERSEDED
    assert list(iter_gridfs_file_ids(left)) == []


def test_deleting_the_head_promotes_by_ordinal_and_moves_only_is_latest(
        monkeypatch, request_factory, test_user):
    """T3. The tombstone keeps its ordinal and its neighbours keep pointing at
    it, so the promoted version ends up is_latest=True *with* a
    next_version_id. That is the shape, not a violation."""
    members = chain_project(2, test_user.username)
    head, older = members[1], members[0]

    response, collection, fs = run_delete(
        monkeypatch, request_factory, test_user, members, head['_id'])

    assert response.status_code == 200
    promoted = collection.docs[str(older['_id'])]
    tombstone = collection.docs[str(head['_id'])]

    assert classify(promoted) == LIVE
    assert promoted['is_latest'] is True
    assert promoted['next_version_id'] == head['_id']      # unchanged
    assert promoted['version_ordinal'] == 1                # never renumbered

    assert classify(tombstone) == TOMBSTONE
    assert tombstone['is_latest'] is False
    assert tombstone['version_ordinal'] == 2
    assert tombstone['previous_version_id'] == older['_id']
    assert tombstone['version_chain_id'] == older['version_chain_id']
    assert str(head['tarfile']) in fs.deleted


def test_a_deleted_head_still_appears_in_the_history_it_was_deleted_from(
        monkeypatch, request_factory, test_user):
    """The point of keeping the tombstone in the chain.

    The array cannot say "this version was deleted", so before the pointers the
    only way to find a deleted version was a separate lookup by
    redirect_to_project. Now it is simply a member.
    """
    members = chain_project(2, test_user.username)
    _response, collection, _fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[1]['_id'])

    promoted = collection.docs[str(members[0]['_id'])]
    entries, _msg = utils.previous_versions(promoted)

    rows = {e['linkid']: e for e in entries}
    assert set(rows) == {str(members[0]['_id']), str(members[1]['_id'])}
    assert rows[str(members[1]['_id'])]['version_deleted_from_history'] is True


def test_deleting_a_middle_version_leaves_the_head_alone(
        monkeypatch, request_factory, test_user):
    """T2 through the view: the head keeps its flag, its ordinal and its
    pointers, and only the victim's document changes."""
    members = chain_project(3, test_user.username)
    head, victim = members[2], members[1]

    response, collection, fs = run_delete(
        monkeypatch, request_factory, test_user, members, victim['_id'])

    assert response.status_code == 200
    unchanged = collection.docs[str(head['_id'])]
    assert unchanged['is_latest'] is True
    assert unchanged['version_ordinal'] == 3
    assert unchanged['previous_version_id'] == victim['_id']

    tombstone = collection.docs[str(victim['_id'])]
    assert classify(tombstone) == TOMBSTONE
    assert tombstone['version_ordinal'] == 2
    assert tombstone['next_version_id'] == head['_id']
    assert str(victim['tarfile']) in fs.deleted


def test_an_unpointered_project_still_deletes_the_way_it_always_did(
        monkeypatch, request_factory, test_user):
    """No pointers, no plan: the array decides, exactly as before.

    26 documents on dev are unpointered. Their delete button must keep working.
    """
    members = chain_project(2, test_user.username)
    for member in members:
        for field in lineage.POINTER_FIELDS:
            member.pop(field, None)

    response, collection, fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[1]['_id'])

    assert response.status_code == 200
    promoted = collection.docs[str(members[0]['_id'])]
    assert classify(promoted) == LIVE
    assert classify(collection.docs[str(members[1]['_id'])]) == TOMBSTONE


def test_an_emptied_project_still_resolves_by_url(
        monkeypatch, request_factory, test_user):
    """Deleting every version leaves an empty project, not an absent one.

    The tombstone has no redirect_to_project -- there is nowhere to forward to
    -- so get_one_project() has to return the tombstone itself rather than
    None. If it returned None the URL would 404 and the project would be gone
    in the only sense a visitor can observe, which is exactly the distinction
    the terminal deletion is written to preserve.
    """
    members = chain_project(1, test_user.username)
    _response, collection, _fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[0]['_id'])

    resolved = utils.get_one_project(str(members[0]['_id']))

    assert resolved is not None, 'the emptied project 404s'
    assert resolved['_id'] == members[0]['_id']
    assert classify(resolved) == TOMBSTONE


def test_an_emptied_project_reads_as_empty_rather_than_as_broken(
        monkeypatch, request_factory, test_user):
    """The project page's own emptiness test, applied to what deletion leaves.

    A tombstone carries no 'runs', which is what makes is_empty_project true
    and what routes the page around the metadata and charting work rather than
    into it with nothing to work on.
    """
    members = chain_project(1, test_user.username)
    _response, collection, _fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[0]['_id'])
    tombstone = collection.docs[str(members[0]['_id'])]

    # The expression from project_page(), applied to the document it would get.
    is_empty_project = (
        ('EMPTY?' in tombstone and tombstone['EMPTY?'] is True)
        or (not tombstone.get('runs'))
        or (len(tombstone.get('runs', {})) == 0))
    assert is_empty_project

    # Nothing on the document sends the page down another branch first.
    assert 'FINISHED?' not in tombstone
    assert tombstone.get('aggregation_failed') is None
    assert 'redirect_to_project' not in tombstone


def test_the_emptied_projects_history_says_the_version_was_deleted(
        monkeypatch, request_factory, test_user):
    """One row, marked deleted, and nothing offered for promotion.

    The history table is the only thing left that says what happened to this
    project, so it has to say it.
    """
    members = chain_project(1, test_user.username)
    _response, collection, _fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[0]['_id'])
    tombstone = collection.docs[str(members[0]['_id'])]

    entries, msg = utils.previous_versions(tombstone)

    assert msg is None, 'there is no newer version to point at'
    assert [e['linkid'] for e in entries] == [str(members[0]['_id'])]
    assert entries[0]['version_deleted_from_history'] is True

    # project_page builds the promote control from the rows that are not
    # deleted, and there are none.
    active = [e for e in entries if not e.get('version_deleted_from_history')]
    assert active == []


def test_an_unreadable_history_stops_the_delete_rather_than_purging(
        monkeypatch, request_factory, test_user):
    """The one case where refusing is the safe answer.

    An unpointered document whose array holds only the pre-April-2024 encoding
    -- the whole entry serialised into the linkid -- names a predecessor that
    cannot be resolved. Treating that as "no previous versions" would take the
    terminal branch and purge the payload of a project whose own history says
    it has a predecessor.
    """
    members = chain_project(2, test_user.username)
    for member in members:
        for field in lineage.POINTER_FIELDS:
            member.pop(field, None)
    members[-1]['previous_versions'] = ['[{"date": "2024-01-01T00:00:00"}]']

    response, collection, fs = run_delete(
        monkeypatch, request_factory, test_user, members, members[1]['_id'])

    assert response.status_code == 409
    assert fs.deleted == []
    assert classify(collection.docs[str(members[1]['_id'])]) != TOMBSTONE


# ---------------------------------------------------------------------------
# The one tombstone-creation routine
# ---------------------------------------------------------------------------

def test_the_tombstone_routine_carries_the_pointers_across_the_replace():
    members = linked(2)
    tombstone = build_deleted_version_tombstone(
        members[1], members[0], 'someone', '2026-08-27T00:00:00.000000',
        is_latest=False)
    for field in lineage.POINTER_FIELDS:
        assert field in tombstone
    assert tombstone['version_ordinal'] == 2
    assert tombstone['is_latest'] is False


def test_the_tombstone_routine_takes_no_successor_for_a_terminal_delete():
    members = linked(1)
    members[0].update({'private': 'public', 'project_members': ['someone']})
    tombstone = build_deleted_version_tombstone(
        members[0], None, 'someone', '2026-08-27T00:00:00.000000', is_latest=True)

    assert classify(tombstone) == TOMBSTONE
    assert 'redirect_to_project' not in tombstone
    # Membership and visibility come from the deleted document itself, because
    # there is no surviving version to inherit them from.
    assert tombstone['private'] == 'public'
    assert tombstone['project_members'] == ['someone']


# ---------------------------------------------------------------------------
# The status a half-write computes has to come from the document as it is now
# ---------------------------------------------------------------------------

class OneWriteBehind:
    """A collection whose reads lag, the way a DocumentDB secondary does.

    find_one() serves the value from before the most recent write.  This is not
    an exotic failure mode to simulate: reads through collection_handle are
    SECONDARY_PREFERRED, and against caper-dev on 2026-08-28 a read issued
    straight after a write returned the previous value 34 times out of 40.
    """

    def __init__(self, document):
        self.committed = dict(document)
        self.pending = dict(document)

    def find_one(self, query, projection=None):
        served = dict(self.committed)
        self.committed = dict(self.pending)     # catches up, one read late
        if projection:
            return {key: served[key] for key in projection if key in served}
        return served

    def update_one(self, query, update):
        self.pending.update(update.get('$set', {}))


def test_a_half_write_reads_its_flags_from_the_primary(monkeypatch):
    """project_delete() must not compute status from a stale replica.

    An edit calls project_update() and then project_delete(). The first clears
    'current'; the second sets 'delete' and recomputes the status from what it
    reads. Reading a replica that is one write behind, it sees current=True and
    writes SOFT_DELETED onto a document whose flags now say SUPERSEDED -- which
    is exactly what one of two re-aggregations produced on dev on 2026-08-28,
    the same code decided by replica lag.
    """
    project_id = ObjectId()
    # As project_update() has just left it: superseded, not soft-deleted.
    primary = OneWriteBehind({'_id': project_id, 'delete': False, 'current': False})
    monkeypatch.setattr(views, 'collection_handle_primary', primary)

    # What the view was handed: the pre-update read, one write out of date.
    stale = {'_id': project_id, 'delete': False, 'current': True}

    assert views.status_after(stale, delete=True) == 'SOFT_DELETED', \
        'the stale document is what produces the wrong answer'
    assert views.status_after(views.current_flags(stale), delete=True) == SUPERSEDED, \
        'reading the flags from the primary produces the right one'


def test_reading_the_flags_keeps_the_rest_of_the_document(monkeypatch):
    """Only the two flags come from the primary; the caller's document stands.

    The view still needs the fields it already read -- members, visibility,
    name -- and re-fetching the whole document to get two booleans would cost
    a full project read on every delete.
    """
    project_id = ObjectId()
    monkeypatch.setattr(views, 'collection_handle_primary',
                        OneWriteBehind({'_id': project_id, 'delete': True,
                                        'current': False}))
    project = {'_id': project_id, 'delete': False, 'current': True,
               'project_name': 'p', 'project_members': ['someone']}

    flags = views.current_flags(project)
    assert flags['project_members'] == ['someone']
    assert flags['project_name'] == 'p'
    assert (flags['delete'], flags['current']) == (True, False)


def test_an_unreadable_primary_falls_back_to_the_document_in_hand(monkeypatch):
    """A missing row must not crash the delete.

    find_one() returning None means the document is gone from under us. There
    is nothing better to compute from than what the caller already read, and
    raising here would turn a stale status into a failed request.
    """
    class Missing:
        def find_one(self, query, projection=None):
            return None

    monkeypatch.setattr(views, 'collection_handle_primary', Missing())
    project = {'_id': ObjectId(), 'delete': False, 'current': True}
    assert views.current_flags(project) == project


# ---------------------------------------------------------------------------
# Deleting from a chain that already holds a tombstone
# ---------------------------------------------------------------------------
#
# Every transition test above starts from a clean chain, and on a clean chain
# "the highest surviving ordinal" and "the highest ordinal" are the same
# document. They stop being the same the moment one version has already been
# deleted, which is the ordinary case after any project has been tidied once.
# Deleting the head then promoted the tombstone on dev, 2026-08-28.

def test_promotion_skips_a_tombstone_and_takes_the_live_version(tombstone_marks):
    """Ordinal 2 is deleted, then ordinal 3. Ordinal 1 is what survives."""
    members = linked(3)
    members[1].update(tombstone_marks)
    plan = lineage.plan_deletion(members, members[2]['_id'])
    assert plan.promoted_id == members[0]['_id'], \
        'a deleted version cannot be promoted back into being the current one'
    assert plan.chain_emptied is False


def test_promotion_reads_the_markers_through_the_projection(tombstone_marks):
    """The predicate has to survive the projection the caller actually uses.

    plan_deletion() is handed whatever chain_members() fetched, and
    chain_members() fetches POINTER_PROJECTION. If a field is_tombstone() reads
    is not in that projection, every tombstone reads as a survivor -- and the
    planner is perfectly correct in isolation while being wrong in the only
    place it is called from.
    """
    members = linked(3)
    members[1].update(tombstone_marks)
    collection = FakeHistoryCollection(members)

    projected = list(collection.find({'version_chain_id': members[0]['_id']},
                                     lineage.POINTER_PROJECTION))
    plan = lineage.plan_deletion(projected, members[2]['_id'])
    assert plan.promoted_id == members[0]['_id'], \
        'POINTER_PROJECTION dropped a field is_tombstone() needs'


def test_deleting_the_head_of_an_all_tombstone_chain_empties_it(tombstone_marks):
    """Nothing survives, so nothing is promoted and the chain is emptied."""
    members = linked(3)
    members[0].update(tombstone_marks)
    members[1].update(tombstone_marks)
    plan = lineage.plan_deletion(members, members[2]['_id'])
    assert plan.promoted_id is None
    assert plan.victim_keeps_head is True
    assert plan.chain_emptied is True


def test_deleting_a_middle_version_beside_a_tombstone_promotes_nothing(tombstone_marks):
    """A non-head deletion never promotes, tombstones in the chain or not."""
    members = linked(3)
    members[0].update(tombstone_marks)
    plan = lineage.plan_deletion(members, members[1]['_id'])
    assert plan.promoted_id is None
    assert plan.victim_keeps_head is False
    assert plan.chain_emptied is False


def test_every_field_the_planner_reads_is_in_the_projection(tombstone_marks):
    """The general form, so the next field added does not repeat this.

    Runs the planner over a chain projected down to POINTER_PROJECTION and over
    the same chain in full, and requires the same answer. A field the planner
    starts reading without adding it here makes the two disagree.
    """
    members = linked(4)
    members[1].update(tombstone_marks)
    members[2].update(tombstone_marks)
    collection = FakeHistoryCollection(members)
    projected = list(collection.find({'version_chain_id': members[0]['_id']},
                                     lineage.POINTER_PROJECTION))

    for victim in members:
        assert (lineage.plan_deletion(projected, victim['_id'])
                == lineage.plan_deletion(members, victim['_id'])), \
            f'the projection changes the plan for {victim["version_ordinal"]}'
