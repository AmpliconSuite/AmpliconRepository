"""Tests for repair_promoted_tombstone.py.

The script repairs invariant I20's population: a chain whose single is_latest
member is a TOMBSTONE while versions nobody deleted sit beside it.  That state
was produced on dev by a deletion that asked is_tombstone() of documents
fetched under a projection which dropped both markers, so every tombstone read
as a survivor and the head was handed to one.

What is worth testing here is the selection, not the writing: a repair that
touches an emptied project -- where a tombstone head is *correct* -- would turn
a valid state into an invalid one, and it would look like a fix while doing it.
"""

import pytest
from bson import ObjectId

from repair_promoted_tombstone import find_affected, _ordinal
from caper.project_status import LIVE, TOMBSTONE, classify

from test_project_version_cleanup import FakeHistoryCollection


TOMBSTONE_MARKS = {'delete': True, 'current': False,
                   'version_deleted_from_history': True, 'payload_purged': True}


def chain(*specs):
    """A chain of members, oldest first; each spec overrides the defaults."""
    ids = [ObjectId() for _ in specs]
    documents = []
    for index, (doc_id, spec) in enumerate(zip(ids, specs)):
        doc = {
            '_id': doc_id, 'project_name': 'p',
            'version_chain_id': ids[0], 'version_ordinal': index + 1,
            'previous_version_id': ids[index - 1] if index else None,
            'next_version_id': ids[index + 1] if index + 1 < len(ids) else None,
            'is_latest': False, 'delete': True, 'current': False,
            'status': 'SUPERSEDED',
        }
        doc.update(spec)
        documents.append(doc)
    return documents


def test_the_corrupted_shape_is_selected_and_the_right_version_promoted():
    """Ordinal 2 is a tombstone holding the head; ordinal 1 survives."""
    members = chain(
        {},                                                  # ord 1, survivor
        {**TOMBSTONE_MARKS, 'is_latest': True, 'status': 'LIVE'},   # ord 2
        {**TOMBSTONE_MARKS},                                 # ord 3
    )
    affected = find_affected(FakeHistoryCollection(members))
    assert len(affected) == 1
    _chain_id, found, tombstone_head, promote_to = affected[0]
    assert tombstone_head['_id'] == members[1]['_id']
    assert promote_to['_id'] == members[0]['_id'], \
        'the head goes to the newest surviving version'
    assert len(found) == 3


def test_an_emptied_project_is_left_alone():
    """Every member a tombstone: a tombstone head is correct there.

    This is the case the repair must not touch. An emptied project keeps a
    current version -- it is where a restore lands and what the URL resolves
    to -- and I16 says so explicitly. A repair that moved the head here would
    have nowhere to move it to, and 'fixing' it would break T6.
    """
    members = chain({**TOMBSTONE_MARKS},
                    {**TOMBSTONE_MARKS, 'is_latest': True})
    assert find_affected(FakeHistoryCollection(members)) == []


def test_a_healthy_chain_is_left_alone():
    members = chain({}, {'is_latest': True, 'delete': False, 'current': True,
                         'status': 'LIVE'})
    assert find_affected(FakeHistoryCollection(members)) == []


def test_a_live_head_over_tombstoned_ancestors_is_left_alone():
    """The ordinary result of deleting an old version."""
    members = chain({}, {**TOMBSTONE_MARKS},
                    {'is_latest': True, 'delete': False, 'current': True,
                     'status': 'LIVE'})
    assert find_affected(FakeHistoryCollection(members)) == []


def test_a_chain_with_two_heads_is_left_to_i3():
    """Not this script's population, and guessing would make it worse.

    Two heads is an I3 finding with its own causes. Picking one to demote here
    would silently choose which version a project is currently on.
    """
    members = chain({'is_latest': True},
                    {**TOMBSTONE_MARKS, 'is_latest': True})
    assert find_affected(FakeHistoryCollection(members)) == []


def test_the_newest_survivor_wins_not_the_nearest():
    """Promotion is by ordinal, over survivors only.

    Ordinals 1 and 3 survive, 2 and 4 are tombstones and 4 holds the head. The
    answer is 3 -- neither the lowest survivor nor the member adjacent to the
    head.
    """
    members = chain({}, {**TOMBSTONE_MARKS}, {},
                    {**TOMBSTONE_MARKS, 'is_latest': True})
    _chain_id, _found, _head, promote_to = find_affected(
        FakeHistoryCollection(members))[0]
    assert promote_to['version_ordinal'] == 3


def test_the_promoted_version_ends_up_classifying_as_live():
    """The repair writes flags, and classify() has to agree with them.

    The point of moving the head is that the project has a current version
    again; a promotion that left the flags saying SUPERSEDED would move the
    flag without moving the meaning.
    """
    from caper.project_status import status_flags
    promoted = {**chain({})[0], **status_flags(LIVE), 'is_latest': True}
    assert classify(promoted) == LIVE
    assert promoted['status'] == LIVE


def test_ordinal_sort_tolerates_a_missing_ordinal():
    """I4 asserts ordinals exist; this runs on data where it may not hold."""
    assert _ordinal({'version_ordinal': 3}) == 3
    assert _ordinal({}) == 0
    assert _ordinal({'version_ordinal': True}) == 0, \
        'a boolean is not an ordinal, and in Python it would sort as one'
