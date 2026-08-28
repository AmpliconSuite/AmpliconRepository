"""Tests for recover_deleted_version.py.

The property that matters here is a refusal, not a repair.  Deleting a version
purges its GridFS payload, and nothing can put those bytes back.  A tool that
clears the tombstone markers anyway produces a version that resolves by URL,
appears in history as a real version, and has no data behind it -- worse than
the tombstone it replaced, and silently so.

The previous version of this script did exactly that: it cleared
``version_deleted_from_history`` and left ``payload_purged`` and
``redirect_to_project`` in place, so the document classified as neither a
tombstone nor a healthy version.
"""

import sys

import pytest
from bson import ObjectId

import recover_deleted_version as recover
from caper.project_status import LIVE, SUPERSEDED, TOMBSTONE, classify

from test_project_version_cleanup import FakeHistoryCollection


class Collection(FakeHistoryCollection):
    """The fake, plus the ``.database.name`` the script asserts on."""

    class _Database:
        name = 'caper-dev'

    database = _Database()


def run(monkeypatch, collection, argv):
    monkeypatch.setattr(recover, 'collection_handle', collection)
    monkeypatch.setattr(sys, 'argv', ['recover_deleted_version.py'] + argv)
    return recover.main()


def chain_of_two(purged=True):
    """A two-version chain whose older version has been deleted."""
    old_id, head_id = ObjectId(), ObjectId()
    chain_id = old_id
    tombstone = {
        '_id': old_id, 'project_name': 'p', 'date': '2026-01-01T00:00:00.000000',
        'delete': True, 'current': True,
        'version_deleted_from_history': True,
        'redirect_to_project': str(head_id),
        'delete_user': 'someone', 'delete_date': '2026-08-01T00:00:00.000000',
        'version_chain_id': chain_id, 'version_ordinal': 1,
        'previous_version_id': None, 'next_version_id': head_id,
        'is_latest': False, 'AA_version': 'AA-1',
    }
    if purged:
        tombstone['payload_purged'] = True
    else:
        tombstone['tarfile'] = ObjectId()
    head = {
        '_id': head_id, 'project_name': 'p', 'date': '2026-02-01T00:00:00.000000',
        'delete': False, 'current': True,
        'version_chain_id': chain_id, 'version_ordinal': 2,
        'previous_version_id': old_id, 'next_version_id': None,
        'is_latest': True, 'previous_versions': [], 'tarfile': ObjectId(),
    }
    return tombstone, head


def test_a_purged_payload_is_refused_and_nothing_is_written(monkeypatch, capsys):
    tombstone, head = chain_of_two(purged=True)
    collection = Collection([tombstone, head])

    with pytest.raises(SystemExit) as exit_info:
        run(monkeypatch, collection, [str(tombstone['_id']), '--apply'])

    assert exit_info.value.code == 2
    assert 'REFUSING' in capsys.readouterr().out
    # Untouched, both documents.
    assert classify(collection.docs[str(tombstone['_id'])]) == TOMBSTONE
    assert collection.docs[str(head['_id'])]['previous_versions'] == []


def test_a_version_whose_payload_survives_is_restored(monkeypatch):
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    run(monkeypatch, collection, [str(tombstone['_id']), '--apply'])

    restored = collection.docs[str(tombstone['_id'])]
    assert classify(restored) == SUPERSEDED
    for marker in recover.TOMBSTONE_MARKERS:
        assert marker not in restored
    assert restored['tarfile'] is not None


def test_every_marker_is_cleared_together_not_a_subset(monkeypatch):
    """The exact defect in the version this replaces.

    Clearing version_deleted_from_history alone left payload_purged and
    redirect_to_project behind, and the document classified as neither one
    thing nor the other.
    """
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    run(monkeypatch, collection, [str(tombstone['_id']), '--apply'])

    restored = collection.docs[str(tombstone['_id'])]
    assert 'redirect_to_project' not in restored
    assert 'version_deleted_from_history' not in restored


def test_the_version_is_re_added_to_the_arrays_the_old_readers_use(monkeypatch):
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    run(monkeypatch, collection, [str(tombstone['_id']), '--apply'])

    entries = collection.docs[str(head['_id'])]['previous_versions']
    assert [e['linkid'] for e in entries] == [str(tombstone['_id'])]
    assert entries[0]['AA_version'] == 'AA-1'


def test_the_current_version_is_read_from_the_chain_when_not_given(monkeypatch, capsys):
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    run(monkeypatch, collection, [str(tombstone['_id'])])

    out = capsys.readouterr().out
    assert 'read from the chain' in out
    assert str(head['_id']) in out
    assert 'DRY-RUN' in out


def test_a_dry_run_writes_nothing(monkeypatch):
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    run(monkeypatch, collection, [str(tombstone['_id'])])

    assert 'version_deleted_from_history' in collection.docs[str(tombstone['_id'])]
    assert collection.docs[str(head['_id'])]['previous_versions'] == []


def test_a_version_that_holds_the_head_comes_back_live(monkeypatch):
    """The terminal deletion, undone: the last version of a project.

    is_latest never moved -- an emptied chain keeps its position-in-time -- so
    restoring it makes the project live again rather than headless.
    """
    tombstone, _head = chain_of_two(purged=False)
    tombstone.update({'is_latest': True, 'next_version_id': None})
    tombstone.pop('redirect_to_project')
    collection = Collection([tombstone])

    run(monkeypatch, collection, [str(tombstone['_id']), '--apply'])

    assert classify(collection.docs[str(tombstone['_id'])]) == LIVE


def test_a_healthy_version_is_left_alone(monkeypatch, capsys):
    _tombstone, head = chain_of_two()
    collection = Collection([head])

    with pytest.raises(SystemExit) as exit_info:
        run(monkeypatch, collection, [str(head['_id']), '--apply'])

    assert exit_info.value.code == 0
    assert 'NOTHING TO DO' in capsys.readouterr().out


def test_the_wrong_database_stops_the_run_before_it_reads_anything(monkeypatch):
    """Prod and dev share one cluster, and dev's name is the local one too."""
    tombstone, head = chain_of_two(purged=False)
    collection = Collection([tombstone, head])

    with pytest.raises(SystemExit) as exit_info:
        run(monkeypatch, collection,
            [str(tombstone['_id']), '--expect-db', 'caper', '--apply'])

    assert 'caper-dev' in str(exit_info.value)
    assert 'version_deleted_from_history' in collection.docs[str(tombstone['_id'])]
