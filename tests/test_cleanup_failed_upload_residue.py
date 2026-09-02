"""The failed-upload residue cleanup.

Run against real scratch collections, for the reason given in
``test_sweep_gridfs_unreferenced``: a fake answering these queries by hand would
be a second implementation drifting from the one the script runs against.

The property under test throughout is that **a file its document still names is
never deleted**, and its inverse: the wreckage of an interrupted ingestion --
labelled for a document that exists but does not name it -- *is*.

The case that motivates the age guard has its own test: during an upload every
file is legitimately unreferenced, because files are written before the document
that names them.
"""

import datetime
import uuid

import pytest
from bson.objectid import ObjectId

from cleanup_failed_upload_residue import (
    DELETABLE, RETAINED, Ownership, candidates, still_deletable,
)
from caper.gridfs_backlinks import (
    DOCUMENT_GONE, LIVE_FILE, TOMBSTONE_PAYLOAD, UNLABELLED,
    UNREFERENCED_BY_ITS_DOCUMENT,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch(mongo_collection):
    """Empty scratch collections in the test database, dropped afterwards."""
    database = mongo_collection.database
    suffix = uuid.uuid4().hex[:8]
    projects = database[f'cleanup_projects_{suffix}']
    fs_files = database[f'cleanup_files_{suffix}']
    fs_chunks = database[f'cleanup_chunks_{suffix}']
    yield projects, fs_files
    projects.drop()
    fs_files.drop()
    fs_chunks.drop()


class _ScratchDB:
    """Maps the names main() opens onto this test's scratch collections.

    main() asks the database for 'projects', 'fs.files' and 'fs.chunks' by name,
    so a test that inserts into uniquely-named collections is invisible to it --
    which is what the first version of these cap tests actually measured.
    """

    def __init__(self, projects, fs_files):
        database = projects.database
        suffix = projects.name.split('_')[-1]
        self.name = database.name
        self._by_name = {
            'projects': projects,
            'fs.files': fs_files,
            'fs.chunks': database[f'cleanup_chunks_{suffix}'],
        }

    def __getitem__(self, key):
        return self._by_name[key]


def _patched_main(monkeypatch, projects, fs_files, argv):
    from cleanup_failed_upload_residue import main
    shim = _ScratchDB(projects, fs_files)
    monkeypatch.setattr('cleanup_failed_upload_residue.connect', lambda expect: shim)
    return main(argv)


def _file(fs_files, *, owner=None, length=100, age_days=30):
    """One fs.files row, written *age_days* ago, optionally backlinked."""
    file_id = ObjectId()
    row = {'_id': file_id, 'length': length,
           'uploadDate': datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=age_days)}
    if owner is not None:
        row['metadata'] = {'project_id': owner}
    fs_files.insert_one(row)
    return file_id


def _project(projects, file_ids, *, project_id=None, tombstone=False):
    """A project naming *file_ids* through a real GridFS slot."""
    project_id = project_id or ObjectId()
    document = {
        '_id': project_id,
        'project_name': 'P',
        'runs': {'sample_1': [{'Sample_name': 'sample_1', 'AA directory': f}
                              for f in file_ids]},
    }
    if tombstone:
        document['version_deleted_from_history'] = True
        document['payload_purged'] = True
    projects.insert_one(document)
    return project_id


def _survey(projects, fs_files, **kwargs):
    kwargs.setdefault('min_age_hours', 24)
    ownership = Ownership(projects)
    rows, skipped, census, _ = candidates(fs_files, ownership, **kwargs)
    return ownership, rows, skipped, census


def test_the_failed_upload_wreckage_is_deletable(scratch):
    """The TCGA_Sarcoma shape: the document records some files, not all.

    Its upload of 2026-08-14 wrote 3,025 files and the document kept 330; the
    other 2,695 are these. They carry a backlink to a document that exists and
    does not name them.
    """
    projects, fs_files = scratch
    project_id = ObjectId()
    recorded = _file(fs_files, owner=project_id)
    stranded = _file(fs_files, owner=project_id)
    _project(projects, [recorded], project_id=project_id)

    documents, rows, _, census = _survey(projects, fs_files)

    assert [row['_id'] for row, _ in rows] == [stranded]
    assert census[UNREFERENCED_BY_ITS_DOCUMENT] == 1
    assert census[LIVE_FILE] == 1


def test_a_file_its_document_still_names_is_never_deletable(scratch):
    projects, fs_files = scratch
    project_id = ObjectId()
    kept = _file(fs_files, owner=project_id)
    _project(projects, [kept], project_id=project_id)

    _, rows, _, _ = _survey(projects, fs_files)

    assert rows == []


def test_a_backlink_to_a_vanished_document_is_deletable(scratch):
    projects, fs_files = scratch
    orphan = _file(fs_files, owner=ObjectId())     # no such document

    _, rows, _, census = _survey(projects, fs_files)

    assert [row['_id'] for row, _ in rows] == [orphan]
    assert census[DOCUMENT_GONE] == 1


def test_unlabelled_rows_are_left_for_the_other_script(scratch):
    """Residue with no backlink belongs to sweep_gridfs_unreferenced.py.

    Deleting it from here would act on the absence of evidence: before the
    backfill has run, every row is unlabelled and that means nothing.
    """
    projects, fs_files = scratch
    _file(fs_files, owner=None)

    _, rows, _, census = _survey(projects, fs_files)

    assert rows == []
    assert census[UNLABELLED] == 1
    assert UNLABELLED in RETAINED


def test_a_tombstone_payload_is_reported_not_deleted(scratch):
    """A tombstone holding files is an unfinished deletion; hiding it is worse."""
    projects, fs_files = scratch
    project_id = ObjectId()
    held = _file(fs_files, owner=project_id)
    _project(projects, [held], project_id=project_id, tombstone=True)

    _, rows, _, census = _survey(projects, fs_files)

    assert rows == []
    assert census[TOMBSTONE_PAYLOAD] == 1
    assert TOMBSTONE_PAYLOAD not in DELETABLE


def test_an_upload_in_flight_is_protected_by_the_age_guard(scratch):
    """The case that makes this script dangerous without --min-age-hours.

    Files are written to GridFS before the document names them, so every file of
    a running ingestion is correctly labelled unreferenced-by-its-document. Only
    its age separates it from real wreckage.
    """
    projects, fs_files = scratch
    project_id = ObjectId()
    _project(projects, [], project_id=project_id)          # document not yet updated
    in_flight = _file(fs_files, owner=project_id, age_days=0)
    old_wreckage = _file(fs_files, owner=project_id, age_days=10)

    ownership, rows, skipped, _ = _survey(projects, fs_files, min_age_hours=24)

    assert [row['_id'] for row, _ in rows] == [old_wreckage]
    assert skipped == 1
    assert ownership.label(fs_files.find_one({'_id': in_flight})) \
        == UNREFERENCED_BY_ITS_DOCUMENT


def test_a_document_claiming_the_file_stops_the_delete(scratch):
    """The window between the survey and the delete is minutes to hours wide."""
    projects, fs_files = scratch
    project_id = ObjectId()
    file_id = _file(fs_files, owner=project_id)
    _project(projects, [], project_id=project_id)

    ownership = Ownership(projects)
    assert still_deletable(fs_files, ownership, file_id)[0] is True

    projects.update_one({'_id': project_id},
                        {'$set': {'runs.sample_1': [{'AA directory': file_id}]}})
    ownership.refresh()                     # what main() does inside the loop

    ok, why = still_deletable(fs_files, ownership, file_id)
    assert ok is False
    assert why == 'now classified %s' % LIVE_FILE


def test_a_vanished_row_stops_the_delete(scratch):
    projects, fs_files = scratch
    file_id = _file(fs_files, owner=ObjectId())
    ownership = Ownership(projects)
    fs_files.delete_one({'_id': file_id})

    assert still_deletable(fs_files, ownership, file_id) == (False, 'row is gone')


def test_the_underscore_spelling_still_protects_its_file(scratch):
    """The spelling that stranded 116,480 files on prod must count as named."""
    projects, fs_files = scratch
    project_id = ObjectId()
    kept = _file(fs_files, owner=project_id)
    projects.insert_one({'_id': project_id, 'project_name': 'P',
                         'runs': {'s': [{'AA_directory': kept}]}})

    _, rows, _, census = _survey(projects, fs_files)

    assert rows == []
    assert census[LIVE_FILE] == 1


def test_a_string_backlink_resolves_to_its_document(scratch):
    """Backlinks are written as ObjectIds, but a string id must not read as gone."""
    projects, fs_files = scratch
    project_id = ObjectId()
    kept = _file(fs_files, owner=str(project_id))
    _project(projects, [kept], project_id=project_id)

    _, rows, _, census = _survey(projects, fs_files)

    assert rows == []
    assert census[LIVE_FILE] == 1


def test_execute_requires_an_undo_record():
    """A destructive run with no record of what it destroyed is not allowed."""
    from cleanup_failed_upload_residue import main
    with pytest.raises(SystemExit) as excinfo:
        main(['--expect-db', 'anything', '--execute'])
    assert excinfo.value.code != 0


def test_expect_db_does_not_select_the_database(monkeypatch):
    """The guard must be able to fail."""
    from cleanup_failed_upload_residue import connect
    monkeypatch.setenv('DB_URI_SECRET', 'mongodb://localhost:27017')
    monkeypatch.setenv('DB_NAME', 'caper-dev')

    with pytest.raises(SystemExit) as excinfo:
        connect('caper')

    assert 'caper-dev' in str(excinfo.value)


def test_the_report_path_writes_nothing(scratch):
    projects, fs_files = scratch
    _file(fs_files, owner=ObjectId())
    _file(fs_files, owner=ObjectId())
    before = fs_files.count_documents({})

    _survey(projects, fs_files)

    assert fs_files.count_documents({}) == before


def test_undo_entries_are_written_before_the_delete():
    """A kill mid-run must leave the record of what was already destroyed."""
    import cleanup_failed_upload_residue as cleanup
    source = open(cleanup.__file__).read()
    body = source[source.index('    undo = open(args.undo_record'):]
    assert body.index('record({') < body.index('delete_gridfs_file_in_batches('), (
        'the undo entry must be appended before the file is deleted')
    assert 'os.fsync(' in body, 'the undo record must be fsynced, not just flushed'


def test_a_large_eligible_set_refuses_to_delete(scratch, tmp_path, monkeypatch):
    """The cap that makes an unattended run safe to schedule.

    A normal night is a handful of files from one failed upload. A sudden jump
    means something changed -- a mass edit, a bad backfill, a bug in the walk --
    and the right response is to stop and be looked at, not to delete faster.
    """
    projects, fs_files = scratch
    for _ in range(3):
        _file(fs_files, owner=ObjectId())

    code = _patched_main(monkeypatch, projects, fs_files,
                         ['--expect-db', projects.database.name, '--max-delete', '2',
                          '--execute', '--undo-record', str(tmp_path / 'undo.jsonl')])

    assert code == 2, 'an over-cap run must exit non-zero so cron reports it'
    assert fs_files.count_documents({}) == 3, 'nothing may be deleted'
    assert not (tmp_path / 'undo.jsonl').exists()


def test_under_the_cap_it_deletes(scratch, tmp_path, monkeypatch):
    projects, fs_files = scratch
    _file(fs_files, owner=ObjectId())

    code = _patched_main(monkeypatch, projects, fs_files,
                         ['--expect-db', projects.database.name, '--max-delete', '10',
                          '--execute', '--undo-record', str(tmp_path / 'undo.jsonl')])

    assert code == 0
    assert fs_files.count_documents({}) == 0
    assert (tmp_path / 'undo.jsonl').exists()


def test_nothing_to_delete_is_not_an_error(scratch, tmp_path, monkeypatch):
    """The normal scheduled outcome. It must exit 0 and write no undo record."""
    projects, fs_files = scratch
    project_id = ObjectId()
    kept = _file(fs_files, owner=project_id)
    _project(projects, [kept], project_id=project_id)

    code = _patched_main(monkeypatch, projects, fs_files,
                         ['--expect-db', projects.database.name, '--execute',
                          '--undo-record', str(tmp_path / 'undo.jsonl')])

    assert code == 0
    assert fs_files.count_documents({}) == 1
    assert not (tmp_path / 'undo.jsonl').exists()


def test_a_file_another_document_names_is_never_deletable(scratch):
    """The defect that made the first version of this script unsafe.

    A file document A genuinely references, whose metadata points at B. Reading
    the backlink and asking only B produces "unreferenced-by-its-document" and
    deletes A's live payload. `gridfs_backlinks` forbids exactly this: "nothing
    may decide to delete a file because its metadata says it is orphaned."

    Demonstrated against the previous implementation on 2026-09-02, before it
    had deleted anything.
    """
    projects, fs_files = scratch
    a, b = ObjectId(), ObjectId()
    misattributed = _file(fs_files, owner=b)   # the backlink lies
    _project(projects, [misattributed], project_id=a)   # A actually names it
    _project(projects, [], project_id=b)                # B names nothing

    ownership, rows, _, census = _survey(projects, fs_files)

    assert rows == [], 'a file another document names must never be a candidate'
    assert ownership.label(fs_files.find_one({'_id': misattributed})) == LIVE_FILE
    assert census[LIVE_FILE] == 1


def test_the_second_ask_re_walks_the_documents(scratch, tmp_path, monkeypatch):
    """A document claiming a file mid-run must stop its delete, end to end.

    The previous version passed `still_deletable` the survey's frozen snapshot,
    so the second ask could not see the one thing it exists to catch. Its test
    passed only because the test re-walked by hand where main() did not.
    """
    import cleanup_failed_upload_residue as cleanup
    projects, fs_files = scratch
    project_id = ObjectId()
    first = _file(fs_files, owner=project_id)
    rescued = _file(fs_files, owner=project_id)
    _project(projects, [], project_id=project_id)

    monkeypatch.setattr(cleanup, 'REFRESH_EVERY', 1)

    real_delete = cleanup.delete_gridfs_file_in_batches

    def claim_then_delete(files, chunks, file_id):
        # The moment the first file goes, the document claims the second.
        projects.update_one({'_id': project_id},
                            {'$set': {'runs.sample_1': [{'AA directory': rescued}]}})
        return real_delete(files, chunks, file_id)

    monkeypatch.setattr(cleanup, 'delete_gridfs_file_in_batches', claim_then_delete)

    code = _patched_main(monkeypatch, projects, fs_files,
                         ['--expect-db', projects.database.name, '--execute',
                          '--undo-record', str(tmp_path / 'undo.jsonl')])

    assert code == 0
    surviving = {row['_id'] for row in fs_files.find({}, {'_id': 1})}
    assert rescued in surviving, 'the claimed file must survive'
    assert first not in surviving, 'the genuinely orphaned file should still go'


def test_a_tombstone_claim_does_not_read_as_live(scratch):
    """A live claim beats a tombstone claim; a tombstone-only file is reported."""
    projects, fs_files = scratch
    tomb = ObjectId()
    held = _file(fs_files, owner=tomb)
    _project(projects, [held], project_id=tomb, tombstone=True)

    ownership, rows, _, census = _survey(projects, fs_files)

    assert rows == []
    assert ownership.label(fs_files.find_one({'_id': held})) == TOMBSTONE_PAYLOAD
    assert census[TOMBSTONE_PAYLOAD] == 1
