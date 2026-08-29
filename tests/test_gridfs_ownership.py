"""The GridFS ownership survey.

Run against real scratch collections rather than fakes. The survey is almost
entirely queries -- ``$in`` batching, ``distinct``, an aggregation, a count with
``limit`` -- and a fake that answered them by hand would be a second
implementation of the query language, drifting from the one the survey actually
runs against. This repo's recurring defect is exactly that: a second copy of
something, quietly disagreeing with the first.

The property under test throughout is that the buckets **partition**
``fs.files``: every row lands in exactly one, and they sum to the collection
count. Percentages, the residue, and every conclusion drawn from the report rest
on that and on nothing else.
"""

import uuid

import pytest
from bson.objectid import ObjectId

from caper.gridfs_ownership import (
    ORDER, OWNED_LIVE, OWNED_TOMBSTONE, RESIDUE_DOCUMENT_GONE,
    RESIDUE_UNLABELLED, RESIDUE_UNREFERENCED, human, survey,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch(mongo_collection):
    """Two empty collections in the test database, dropped afterwards."""
    database = mongo_collection.database
    suffix = uuid.uuid4().hex[:8]
    projects = database[f'ownership_projects_{suffix}']
    fs_files = database[f'ownership_files_{suffix}']
    yield projects, fs_files
    projects.drop()
    fs_files.drop()


def _file(fs_files, length=100, owner=None):
    """One fs.files row, optionally carrying a backlink."""
    file_id = ObjectId()
    row = {'_id': file_id, 'length': length}
    if owner is not None:
        row['metadata'] = {'project_id': owner}
    fs_files.insert_one(row)
    return file_id


def _project(projects, file_ids, *, tombstone=False, name='P'):
    """A project document naming *file_ids* through a real GridFS slot."""
    project_id = ObjectId()
    # One id per slot: a GridFS key holds a single value, and
    # iter_gridfs_file_ids() yields nothing for a list sitting in one. Building
    # the fixture the way the upload path builds a document is the point --
    # a fixture shape the walker does not recognise tests nothing.
    document = {
        '_id': project_id,
        'project_name': name,
        'runs': {'sample_1': [{'Sample_name': 'sample_1', 'AA directory': file_id}
                              for file_id in file_ids]},
    }
    if tombstone:
        # Both markers, because that is what makes a tombstone -- version
        # removed from history and payload purged. `delete=True` alone is a
        # different state entirely.
        document['version_deleted_from_history'] = True
        document['payload_purged'] = True
    projects.insert_one(document)
    return project_id


def _assert_partitions(result):
    """The buckets sum to the collection. Everything else depends on this."""
    assert sum(result['counts'][label] for label in ORDER) == result['total_files']
    assert result['owned'] + result['residue'] == result['total_files']


def test_a_live_project_owns_its_files(scratch):
    projects, fs_files = scratch
    ids = [_file(fs_files) for _ in range(3)]
    _project(projects, ids)

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_LIVE] == 3
    assert result['bytes'][OWNED_LIVE] == 300
    assert result['residue'] == 0
    _assert_partitions(result)


def test_a_file_no_document_names_is_residue(scratch):
    projects, fs_files = scratch
    _project(projects, [_file(fs_files)])
    _file(fs_files)                      # named by nobody

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_LIVE] == 1
    assert result['residue'] == 1
    _assert_partitions(result)


def test_an_unlabelled_residue_row_is_not_called_an_orphan(scratch):
    """Before the backfill, residue lands in the bucket that claims least."""
    projects, fs_files = scratch
    _file(fs_files)

    result = survey(projects, fs_files)

    assert result['counts'][RESIDUE_UNLABELLED] == 1
    assert result['counts'][RESIDUE_DOCUMENT_GONE] == 0
    assert result['counts'][RESIDUE_UNREFERENCED] == 0
    assert result['labelled_rows'] == 0


def test_a_backlink_to_a_deleted_document_is_residue_document_gone(scratch):
    projects, fs_files = scratch
    _file(fs_files, owner=ObjectId())    # names a project that never existed

    result = survey(projects, fs_files)

    assert result['counts'][RESIDUE_DOCUMENT_GONE] == 1
    assert result['counts'][RESIDUE_UNLABELLED] == 0
    _assert_partitions(result)


def test_a_backlink_the_document_no_longer_names_is_residue_unreferenced(scratch):
    """Residue of a version edit: the document is alive, the file is not in it."""
    projects, fs_files = scratch
    kept = _file(fs_files)
    project_id = _project(projects, [kept])
    _file(fs_files, owner=project_id)    # labelled, but the document dropped it

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_LIVE] == 1
    assert result['counts'][RESIDUE_UNREFERENCED] == 1
    assert result['counts'][RESIDUE_DOCUMENT_GONE] == 0
    _assert_partitions(result)


def test_a_tombstone_still_holding_files_is_reported_as_such(scratch):
    projects, fs_files = scratch
    ids = [_file(fs_files) for _ in range(2)]
    _project(projects, ids, tombstone=True, name='purged')

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_TOMBSTONE] == 2
    assert result['counts'][OWNED_LIVE] == 0
    assert [row['project_name'] for row in result['tombstones_holding']] == ['purged']
    _assert_partitions(result)


def test_two_documents_naming_one_file_do_not_count_it_twice(scratch):
    """The buckets are per row, not per (document, file) pair.

    Nothing shares a file on dev or prod -- measured 2026-08-29, distinct ids
    exactly equalled document-file pairs -- but the local fixtures do, and
    before this was tracked the report claimed 206% of the collection was owned.
    """
    projects, fs_files = scratch
    shared = _file(fs_files)
    _project(projects, [shared], name='first')
    _project(projects, [shared], name='second')

    result = survey(projects, fs_files)

    assert result['total_files'] == 1
    assert result['counts'][OWNED_LIVE] == 1
    assert result['shared_rows'] == 1
    _assert_partitions(result)


def test_a_live_document_keeps_a_file_a_tombstone_also_names(scratch):
    """Sharing has a precedence, and it is not document order.

    A file a live project still names is not deletable, whichever document the
    walk happened to reach first. Counting it as tombstone payload would put a
    live file on the list of deletions that did not finish.
    """
    projects, fs_files = scratch
    shared = _file(fs_files)
    _project(projects, [shared], tombstone=True, name='tombstone')
    _project(projects, [shared], name='alive')

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_LIVE] == 1
    assert result['counts'][OWNED_TOMBSTONE] == 0
    assert result['bytes'][OWNED_TOMBSTONE] == 0
    _assert_partitions(result)


def test_an_id_a_document_names_with_no_row_is_counted_separately(scratch):
    """I12's finding: a document pointing at storage that is gone."""
    projects, fs_files = scratch
    _project(projects, [ObjectId()])

    result = survey(projects, fs_files)

    assert result['named_absent'] == 1
    assert result['counts'][OWNED_LIVE] == 0
    assert result['total_files'] == 0


def test_agreement_between_the_backlink_and_the_document_is_reported(scratch):
    projects, fs_files = scratch
    agreeing = _file(fs_files)
    unlabelled = _file(fs_files)
    project_id = _project(projects, [agreeing, unlabelled])
    fs_files.update_one({'_id': agreeing},
                        {'$set': {'metadata': {'project_id': project_id}}})

    result = survey(projects, fs_files)

    assert result['backlink_agrees'] == 1
    assert result['backlink_missing'] == 1
    assert result['backlink_disagrees'] == 0


def test_a_backlink_naming_the_wrong_document_is_flagged_not_believed(scratch):
    """The document wins. A wrong backlink must not move a file out of owned."""
    projects, fs_files = scratch
    misfiled = _file(fs_files, owner=ObjectId())
    _project(projects, [misfiled], name='real owner')

    result = survey(projects, fs_files)

    assert result['counts'][OWNED_LIVE] == 1
    assert result['backlink_disagrees'] == 1
    assert result['counts'][RESIDUE_DOCUMENT_GONE] == 0
    _assert_partitions(result)


def test_the_survey_batches_ids_rather_than_sending_one_huge_in(scratch):
    """One document can name six figures of files; CHUNK bounds the request."""
    from caper.gridfs_ownership import CHUNK, _chunked

    batches = list(_chunked(range(CHUNK * 2 + 3)))
    assert [len(batch) for batch in batches] == [CHUNK, CHUNK, 3]


def test_bytes_are_reported_in_units_a_reader_can_act_on():
    assert human(0) == '0.0 B'
    assert human(1536) == '1.5 KiB'
    assert human(3 * 1024 ** 3) == '3.0 GiB'
