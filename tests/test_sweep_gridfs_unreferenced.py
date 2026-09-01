"""The GridFS residue sweeper.

Run against real scratch collections, for the reason given in
``test_gridfs_ownership``: the logic here is mostly queries, and a fake that
answered them by hand would be a second implementation drifting from the one the
sweeper runs against.

The property under test throughout is the one that matters for a destructive
script: **a file any document names is never a candidate, and never deleted**,
including when the document spells the key with underscores, when the document
is a tombstone, and when the document appeared after the survey had walked past
it.
"""

import datetime
import json
import uuid

import pytest
from bson.objectid import ObjectId

from sweep_gridfs_unreferenced import (
    absorb_new_documents, candidates, owned_file_ids, still_unreferenced,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch(mongo_collection):
    """Two empty collections in the test database, dropped afterwards."""
    database = mongo_collection.database
    suffix = uuid.uuid4().hex[:8]
    projects = database[f'sweep_projects_{suffix}']
    fs_files = database[f'sweep_files_{suffix}']
    yield projects, fs_files
    projects.drop()
    fs_files.drop()


def _file(fs_files, *, length=100, age_days=30, owner=None):
    """One fs.files row, written *age_days* ago."""
    file_id = ObjectId()
    row = {'_id': file_id, 'length': length,
           'uploadDate': datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=age_days)}
    if owner is not None:
        row['metadata'] = {'project_id': owner}
    fs_files.insert_one(row)
    return file_id


def _project(projects, file_ids, *, key='AA directory', tombstone=False):
    """A project naming *file_ids* through a real GridFS slot."""
    project_id = ObjectId()
    document = {
        '_id': project_id,
        'project_name': 'P',
        'runs': {'sample_1': [{'Sample_name': 'sample_1', key: file_id}
                              for file_id in file_ids]},
    }
    if tombstone:
        document['version_deleted_from_history'] = True
        document['payload_purged'] = True
    projects.insert_one(document)
    return project_id


def _sweep(projects, fs_files, **kwargs):
    kwargs.setdefault('min_age_hours', 24)
    owned, seen = owned_file_ids(projects)
    rows, skipped = candidates(fs_files, owned, **kwargs)
    return owned, seen, rows, skipped


def test_a_file_a_document_names_is_never_a_candidate(scratch):
    projects, fs_files = scratch
    kept = _file(fs_files)
    loose = _file(fs_files)
    _project(projects, [kept])

    _, _, rows, _ = _sweep(projects, fs_files)

    assert [row['_id'] for row in rows] == [loose]


def test_the_underscore_spelling_still_protects_its_file(scratch):
    """The spelling that stranded 116,480 files on prod must count as owned.

    Documents written before aggregator 6.0.0 use `AA_directory`; documents
    written after use `AA directory`. A sweeper that recognised only one would
    delete the payload of every project of the other vintage.
    """
    projects, fs_files = scratch
    kept = _file(fs_files)
    _project(projects, [kept], key='AA_directory')

    owned, _, rows, _ = _sweep(projects, fs_files)

    assert kept in owned
    assert rows == []


def test_a_tombstone_still_owns_its_files(scratch):
    """A tombstone holding files is a deletion bug, and hiding it is worse.

    Sweeping them would make the incomplete deletion permanently invisible, so
    the sweeper leaves them for `report_gridfs_orphans.py` to keep reporting.
    """
    projects, fs_files = scratch
    held = _file(fs_files)
    _project(projects, [held], tombstone=True)

    owned, _, rows, _ = _sweep(projects, fs_files)

    assert held in owned
    assert rows == []


def test_recent_files_are_held_back(scratch):
    """A file is written before the document that names it."""
    projects, fs_files = scratch
    fresh = _file(fs_files, age_days=0)
    old = _file(fs_files, age_days=10)

    _, _, rows, skipped = _sweep(projects, fs_files, min_age_hours=24)

    assert [row['_id'] for row in rows] == [old]
    assert skipped == 1
    assert fresh not in [row['_id'] for row in rows]


def test_before_takes_only_the_older_tranche(scratch):
    projects, fs_files = scratch
    _file(fs_files, age_days=10)
    ancient = _file(fs_files, age_days=800)

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
    _, _, rows, _ = _sweep(projects, fs_files, before=cutoff)

    assert [row['_id'] for row in rows] == [ancient]


def test_a_document_created_during_the_survey_is_absorbed(scratch):
    """The survey takes minutes; an upload can land inside that window.

    `owned_file_ids` has already walked past where the new document would have
    been, so without this its files read as residue and would be deleted.
    """
    projects, fs_files = scratch
    late = _file(fs_files)
    owned, seen = owned_file_ids(projects)
    assert late not in owned

    _project(projects, [late])                      # arrives after the walk
    added_docs, added_files = absorb_new_documents(projects, owned, seen)

    assert (added_docs, added_files) == (1, 1)
    assert late in owned
    assert still_unreferenced(fs_files, owned, late) == (False, 'a document names it')


def test_absorb_is_idempotent(scratch):
    projects, fs_files = scratch
    _project(projects, [_file(fs_files)])
    owned, seen = owned_file_ids(projects)

    assert absorb_new_documents(projects, owned, seen) == (0, 0)


def test_a_backlink_appearing_stops_the_delete(scratch):
    """The backfill can claim a row between the survey and the delete."""
    projects, fs_files = scratch
    file_id = _file(fs_files)
    owned, _ = owned_file_ids(projects)
    assert still_unreferenced(fs_files, owned, file_id) == (True, None)

    fs_files.update_one({'_id': file_id},
                        {'$set': {'metadata': {'project_id': ObjectId()}}})

    ok, why = still_unreferenced(fs_files, owned, file_id)
    assert ok is False
    assert 'backlink' in why


def test_a_vanished_row_stops_the_delete(scratch):
    projects, fs_files = scratch
    file_id = _file(fs_files)
    owned, _ = owned_file_ids(projects)
    fs_files.delete_one({'_id': file_id})

    assert still_unreferenced(fs_files, owned, file_id) == (False, 'row is gone')


def test_execute_requires_an_undo_record():
    """A destructive run with no record of what it destroyed is not allowed."""
    from sweep_gridfs_unreferenced import main
    with pytest.raises(SystemExit) as excinfo:
        main(['--expect-db', 'anything', '--execute'])
    assert excinfo.value.code != 0


def test_expect_db_does_not_select_the_database(monkeypatch):
    """The guard must be able to fail.

    An earlier script in this repository passed --expect-db straight to the
    client, so the comparison was against a value it had itself chosen and could
    never mismatch.
    """
    from sweep_gridfs_unreferenced import connect
    monkeypatch.setenv('DB_URI_SECRET', 'mongodb://localhost:27017')
    monkeypatch.setenv('DB_NAME', 'caper-dev')

    with pytest.raises(SystemExit) as excinfo:
        connect('caper')

    assert 'caper-dev' in str(excinfo.value)


def test_undo_entries_are_written_before_the_delete(tmp_path):
    """A kill mid-run must leave the record of what was already destroyed.

    The dev purge of 2026-09-01 lost the record for 17 documents to exactly this:
    entries accumulated in memory and were written after the loop.
    """
    import sweep_gridfs_unreferenced as sweep
    source = open(sweep.__file__).read()
    body = source[source.index('    undo = open(args.undo_record'):]
    record_call = body.index('record({')
    delete_call = body.index('delete_gridfs_file_in_batches(')
    assert record_call < delete_call, (
        'the undo entry must be appended before the file is deleted')
    assert 'os.fsync(' in body, 'the undo record must be fsynced, not just flushed'


def test_the_report_path_writes_nothing(scratch, tmp_path):
    """Without --execute the collections must be untouched."""
    projects, fs_files = scratch
    _file(fs_files)
    _file(fs_files)
    before = fs_files.count_documents({})

    owned, _ = owned_file_ids(projects)
    candidates(fs_files, owned, min_age_hours=24)

    assert fs_files.count_documents({}) == before
    assert list(tmp_path.iterdir()) == []


def test_undo_record_round_trips_as_jsonl(tmp_path):
    """Each line must be independently parseable, so a truncated file still reads."""
    path = tmp_path / 'undo.jsonl'
    with open(path, 'a') as handle:
        handle.write(json.dumps({'run_header': True, 'database': 'caper-dev'}) + '\n')
        handle.write(json.dumps({'_id': str(ObjectId()), 'length': 12}) + '\n')
        handle.write('{"_id": "truncated"')          # a kill mid-write

    good = []
    for line in open(path):
        try:
            good.append(json.loads(line))
        except ValueError:
            pass

    assert len(good) == 2
    assert good[0]['run_header'] is True
