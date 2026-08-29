"""The derived chain view: what it derives, and that only one thing writes it.

The view is a materialised copy of lineage that already lives on the project
documents. It exists so a chain is addressable and so there is a second copy to
check the documents against. The rule that makes a second copy safe rather than
a second source of truth is that authority runs one way -- documents to view,
never back -- and the spec asks for that to be enforced by a test rather than by
intent, which is what test_only_the_rebuild_command_writes_the_view does.
"""

import pathlib
import re

import pytest
from bson.objectid import ObjectId

from caper.project_status import LIVE, SUPERSEDED, TOMBSTONE, status_flags
from caper.version_chains import (
    COLLECTION,
    PAYLOAD_PRESENT,
    PAYLOAD_PURGED,
    build_chain_document,
    default_canonical_name,
    group_into_chains,
    head_of,
    is_empty_chain,
    order_members,
    source_digest,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


def _member(ordinal, status=SUPERSEDED, is_latest=False, name='P', purged=False):
    doc = {'_id': ObjectId(), 'version_ordinal': ordinal, 'project_name': name,
           'is_latest': is_latest, **status_flags(status)}
    if purged:
        doc['payload_purged'] = True
    return doc


def test_members_are_ordered_by_ordinal_not_insertion():
    members = [_member(3), _member(1), _member(2)]
    assert [d['version_ordinal'] for d in order_members(members)] == [1, 2, 3]


def test_a_true_ordinal_does_not_masquerade_as_one():
    """isinstance(True, int) is True in Python, so a boolean would sort as 1."""
    doc = {'_id': ObjectId(), 'version_ordinal': True, **status_flags(LIVE)}
    assert build_chain_document(ObjectId(), [doc])['versions'][0]['ordinal'] == 0


def test_the_digest_covers_shape_and_ignores_display_fields():
    """(id, ordinal, status) decide the digest; date must not make it churn."""
    members = [_member(1), _member(2, LIVE, is_latest=True)]
    before = source_digest(members)

    members[0]['date'] = '2026-08-29'
    assert source_digest(members) == before, \
        'date changed the digest, so every rebuild would report drift'

    members[0].update(status_flags(TOMBSTONE))
    assert source_digest(members) != before, \
        'a member changing status must change the digest; that is the point'


def test_the_digest_is_order_independent_of_input():
    a = _member(1)
    b = _member(2, LIVE, is_latest=True)
    assert source_digest([a, b]) == source_digest([b, a])


def test_adjacent_ordinals_cannot_collide():
    """Without separators, (1, 11) and (11, 1) could produce the same bytes."""
    one = ObjectId()
    two = ObjectId()
    first = [{'_id': one, 'version_ordinal': 1, **status_flags(LIVE)},
             {'_id': two, 'version_ordinal': 11, **status_flags(LIVE)}]
    second = [{'_id': one, 'version_ordinal': 11, **status_flags(LIVE)},
              {'_id': two, 'version_ordinal': 1, **status_flags(LIVE)}]
    assert source_digest(first) != source_digest(second)


def test_head_is_position_not_status():
    """An emptied chain's head is a tombstone, and that is correct."""
    members = [_member(1, TOMBSTONE, purged=True),
               _member(2, TOMBSTONE, is_latest=True, purged=True)]
    head = head_of(order_members(members))
    assert head is not None and head['version_ordinal'] == 2


def test_no_head_is_recorded_rather_than_invented():
    """Two heads or none is a document defect; I3 and I16 own it.

    The view must not paper over it by picking one, or the validator would be
    comparing against a repair rather than against the documents.
    """
    assert head_of([_member(1), _member(2)]) is None
    assert head_of([_member(1, is_latest=True), _member(2, LIVE, is_latest=True)]) is None
    assert build_chain_document(ObjectId(), [_member(1)])['head_project_id'] is None


def test_empty_is_every_member_a_tombstone():
    assert is_empty_chain([_member(1, TOMBSTONE, purged=True),
                           _member(2, TOMBSTONE, is_latest=True, purged=True)])
    assert not is_empty_chain([_member(1, TOMBSTONE, purged=True),
                               _member(2, LIVE, is_latest=True)])
    assert not is_empty_chain([]), 'a chain with no members is not an empty chain'


def test_emptiness_is_not_written_into_the_chain_document():
    """I15's rule, at the point where it would be tempting to break it."""
    members = [_member(1, TOMBSTONE, purged=True),
               _member(2, TOMBSTONE, is_latest=True, purged=True)]
    doc = build_chain_document(ObjectId(), members)
    assert is_empty_chain(members)
    for key in ('chain_empty', 'is_empty', 'empty_chain', 'EMPTY?'):
        assert key not in doc


def test_payload_state_follows_the_purge_marker():
    members = [_member(1, TOMBSTONE, purged=True), _member(2, LIVE, is_latest=True)]
    entries = build_chain_document(ObjectId(), members)['versions']
    assert entries[0]['payload_state'] == PAYLOAD_PURGED
    assert entries[1]['payload_state'] == PAYLOAD_PRESENT


def test_canonical_name_prefers_the_head_but_survives_an_emptied_chain():
    live = [_member(1, name='old'), _member(2, LIVE, is_latest=True, name='current')]
    assert default_canonical_name(live) == 'current'

    # The case the field exists for: every version deleted, name still needed.
    emptied = [_member(1, TOMBSTONE, purged=True, name='gone'),
               _member(2, TOMBSTONE, is_latest=True, purged=True, name='alsogone')]
    assert default_canonical_name(emptied) == 'alsogone'


def test_documents_without_a_chain_are_skipped_not_invented():
    """They are I1's population; a chain of one here would hide them."""
    with_chain = _member(1, LIVE, is_latest=True)
    with_chain['version_chain_id'] = with_chain['_id']
    without = _member(1, LIVE, is_latest=True)
    grouped = group_into_chains([with_chain, without])
    assert list(grouped) == [with_chain['_id']]


def test_derived_document_carries_only_derived_fields():
    """So a caller cannot clobber canonical_name by spreading it."""
    doc = build_chain_document(ObjectId(), [_member(1, LIVE, is_latest=True)])
    for field in ('canonical_name', 'retired'):
        assert field not in doc


def test_only_the_rebuild_command_writes_the_view():
    """One direction of authority, enforced rather than intended.

    A view anything may write is a second source of truth, which is the failure
    this whole area is being dug out of. Feature code reads the collection; only
    rebuild_version_chains.py writes it.
    """
    writers = re.compile(
        r'\.(insert_one|insert_many|update_one|update_many|replace_one|'
        r'delete_one|delete_many|bulk_write|find_one_and_update|'
        r'find_one_and_replace|find_one_and_delete)\b')

    offenders = []
    for path in sorted((REPO / 'caper').rglob('*.py')):
        if 'migrations' in path.parts:
            continue
        source = path.read_text()
        if COLLECTION not in source:
            continue
        for number, line in enumerate(source.splitlines(), 1):
            if writers.search(line):
                offenders.append(f'{path.relative_to(REPO)}:{number}: {line.strip()}')

    assert not offenders, (
        'application code writes the derived chain view; only '
        'rebuild_version_chains.py may:\n  ' + '\n  '.join(offenders))


def test_the_rebuild_command_preserves_the_authoritative_fields():
    """canonical_name and retired live on the chain document and nowhere else.

    They must survive a rebuild, because they must survive the deletion of every
    version -- that is the test for whether a field is chain-level at all.
    """
    source = (REPO / 'rebuild_version_chains.py').read_text()
    assert '$setOnInsert' in source, \
        'the rebuild must seed the authoritative fields without overwriting them'

    setter = source.split("update = {'$set':")[1].split('\n')[0]
    for field in ('canonical_name', 'retired'):
        assert field not in setter, \
            f'{field} is authoritative on the chain document; a rebuild must ' \
            f'not overwrite it'
