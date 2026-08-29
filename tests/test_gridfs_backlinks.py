"""Backlinks from GridFS files to the documents that name them.

"Is this one file orphaned?" costs a traversal of every project document today
-- on prod, 345 documents and 232 MiB to build a set of 948,515 ids and diff it
against 1,065,019 fs.files rows. Both production incidents behind this work
happened inside that traversal and both were traversal bugs, one worth 80,170
files. A backlink makes the question a find_one.

Authority still runs documents -> files. Nothing here may decide a file is
deletable on the strength of its metadata alone.
"""

import pathlib
import re

import pytest
from bson.objectid import ObjectId

from caper.gridfs_backlinks import (
    DOCUMENT_GONE,
    LIVE_FILE,
    METADATA_FIELD,
    PROJECT_ID,
    TOMBSTONE_PAYLOAD,
    UNLABELLED,
    UNREFERENCED_BY_ITS_DOCUMENT,
    as_object_id,
    build_metadata,
    classify_file,
    put_with_backlink,
)
from caper.project_status import LIVE, TOMBSTONE, status_flags

REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeGridFS:
    """Records what put() was called with. Returns a fresh id each time."""

    def __init__(self, explode=False):
        self.calls = []
        self.explode = explode

    def put(self, fileobj, **kwargs):
        if self.explode:
            raise RuntimeError('gridfs is down')
        self.calls.append((fileobj, kwargs))
        return ObjectId()


# --------------------------------------------------------------------------
# id coercion -- a field that is sometimes str and sometimes ObjectId makes
# every later query wrong for half the rows
# --------------------------------------------------------------------------

def test_ids_are_normalised_to_objectid():
    oid = ObjectId()
    assert as_object_id(oid) == oid
    assert as_object_id(str(oid)) == oid


@pytest.mark.parametrize('value', [
    None, '', 'not-an-id', 'deadbeef', True, False,
    '0123456789abcdef0123456789abcdef',      # a uuid4().hex, 32 chars not 24
])
def test_things_that_are_not_ids_become_none(value):
    assert as_object_id(value) is None


def test_a_uuid_project_id_yields_no_backlink_rather_than_a_bad_one():
    """create_project_helper passes tmp_id, which is a uuid on a direct create.

    Storing that as project_id would produce a backlink pointing at nothing.
    An absent one is honest and the backfill completes it.
    """
    assert build_metadata('0123456789abcdef0123456789abcdef') == {}


# --------------------------------------------------------------------------
# metadata construction
# --------------------------------------------------------------------------

def test_unknown_fields_are_omitted_not_stored_as_none():
    """So {'metadata.project_id': {'$exists': True}} means what it says."""
    pid = ObjectId()
    meta = build_metadata(pid, sample_name='GBM39', feature_key='AA PNG file')
    assert meta[PROJECT_ID] == pid
    assert meta['sample_name'] == 'GBM39'
    assert meta['feature_key'] == 'AA PNG file'
    assert 'version_chain_id' not in meta
    assert 'written_by_event' not in meta
    assert 'written_at' in meta


def test_no_backlink_at_all_produces_an_empty_subdocument():
    assert build_metadata(None) == {}
    assert 'written_at' not in build_metadata(None)


# --------------------------------------------------------------------------
# put_with_backlink
# --------------------------------------------------------------------------

def test_the_backlink_travels_with_the_file():
    fs = FakeGridFS()
    pid = ObjectId()
    put_with_backlink(fs, b'x', project_id=pid, sample_name='GBM39',
                      feature_key='AA PNG file')
    _, kwargs = fs.calls[0]
    assert kwargs[METADATA_FIELD][PROJECT_ID] == pid
    assert kwargs[METADATA_FIELD]['feature_key'] == 'AA PNG file'


def test_filename_is_still_passed_through():
    fs = FakeGridFS()
    put_with_backlink(fs, b'x', project_id=ObjectId(), filename='AA.tar.gz')
    assert fs.calls[0][1]['filename'] == 'AA.tar.gz'


def test_no_metadata_key_when_there_is_nothing_to_say():
    """An empty metadata subdocument would be noise on every legacy row."""
    fs = FakeGridFS()
    put_with_backlink(fs, b'x', project_id=None)
    assert METADATA_FIELD not in fs.calls[0][1]


def test_a_backlink_is_never_worth_a_file(monkeypatch):
    """If metadata cannot be built, store the file anyway.

    An unlabelled row is something the backfill completes; a failed upload is
    data the user loses.
    """
    import caper.gridfs_backlinks as module

    def explode(*args, **kwargs):
        raise ValueError('nope')

    monkeypatch.setattr(module, 'build_metadata', explode)
    fs = FakeGridFS()
    file_id = put_with_backlink(fs, b'x', project_id=ObjectId())
    assert file_id is not None
    assert METADATA_FIELD not in fs.calls[0][1]


def test_a_real_gridfs_failure_still_raises():
    """Only the metadata is best-effort. A failed put must not look like success."""
    with pytest.raises(RuntimeError):
        put_with_backlink(FakeGridFS(explode=True), b'x', project_id=ObjectId())


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def _doc(status=LIVE):
    return {'_id': ObjectId(), **status_flags(status)}


def test_a_file_its_document_still_names_is_live():
    fid, doc = ObjectId(), _doc()
    assert classify_file(fid, {PROJECT_ID: doc['_id']}, doc, {fid}) == LIVE_FILE


def test_a_file_its_document_no_longer_names_is_residue():
    """The case a re-aggregation produces: a full set of new ids, none of them
    the old ones. This is the distinction it would be easy to get wrong by
    asking whether the document names *any* files."""
    fid, doc = ObjectId(), _doc()
    other = {ObjectId(), ObjectId()}
    assert classify_file(fid, {PROJECT_ID: doc['_id']}, doc, other) == \
        UNREFERENCED_BY_ITS_DOCUMENT


def test_membership_survives_a_string_id():
    """Documents store these ids in both encodings; the comparison must not care."""
    fid, doc = ObjectId(), _doc()
    assert classify_file(fid, {PROJECT_ID: doc['_id']}, doc, {str(fid)}) == LIVE_FILE


def test_a_file_whose_document_is_gone():
    fid = ObjectId()
    assert classify_file(fid, {PROJECT_ID: ObjectId()}, None, set()) == DOCUMENT_GONE


def test_a_tombstone_still_holding_files_is_flagged_separately():
    """Deletable, and a bug: a tombstone's payload was supposed to be purged."""
    fid = ObjectId()
    doc = {'_id': ObjectId(), **status_flags(TOMBSTONE)}
    assert classify_file(fid, {PROJECT_ID: doc['_id']}, doc, {fid}) == \
        TOMBSTONE_PAYLOAD


def test_no_backlink_is_unlabelled_not_orphaned():
    """Before the backfill runs, absence means nothing at all.

    Reading it as "orphaned" is how you delete 80,170 live files.
    """
    fid = ObjectId()
    assert classify_file(fid, None, None, set()) == UNLABELLED
    assert classify_file(fid, {}, None, set()) == UNLABELLED
    assert classify_file(fid, {'sample_name': 'GBM39'}, None, set()) == UNLABELLED


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------

def test_every_gridfs_write_carries_a_backlink():
    """A new bare fs.put() is a new source of unlabelled files.

    The backfill can only complete rows for documents that still exist, so a
    write site that skips the backlink quietly re-grows the population this
    phase exists to bound.
    """
    offenders = []
    for path in sorted((REPO / 'caper').rglob('*.py')):
        if path.name == 'gridfs_backlinks.py' or 'migrations' in path.parts:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r'\bfs(_handle)?\.put\s*\(', line):
                offenders.append(f'{path.relative_to(REPO)}:{number}: {line.strip()}')

    assert not offenders, (
        'GridFS write that does not carry a backlink; use put_with_backlink():'
        '\n  ' + '\n  '.join(offenders))


@pytest.mark.slow
@pytest.mark.integration
def test_a_real_upload_labels_the_files_it_stores(
        request_factory, test_user, mongo_collection):
    """The wiring, not the helper.

    The unit tests above prove put_with_backlink() builds the right metadata.
    They say nothing about whether the four call sites in views.py actually use
    it, which is what decides whether the unlabelled population stops growing.
    """
    from conftest import (
        _build_create_request, _cleanup_project, _poll_until_finished,
        _project_id_from_redirect, DATASET_SMALL_TAR, DATASET_SMALL_XLSX,
    )
    from caper.utils import db_handle
    from caper.views import create_project
    from caper.project_version_cleanup import iter_gridfs_file_ids

    project_id = None
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'BacklinkWiring',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        project_id = _project_id_from_redirect(response)
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"create failed: {doc.get('error_message') if doc else 'timed out'}"

        named = {oid for oid in iter_gridfs_file_ids(doc)}
        assert named, 'the fixture project stored no GridFS files to check'

        rows = list(db_handle['fs.files'].find(
            {'_id': {'$in': sorted(named)}}, {'metadata': 1}))
        assert len(rows) == len(named), 'some named files have no fs.files row'

        unlabelled = [r['_id'] for r in rows
                      if (r.get('metadata') or {}).get(PROJECT_ID) is None]
        assert not unlabelled, (
            f'{len(unlabelled)} of {len(rows)} files stored by this upload '
            f'carry no backlink; a write site is still calling fs.put directly')

        # And the backlink has to name the right document, not merely exist.
        wrong = [r['_id'] for r in rows
                 if r['metadata'][PROJECT_ID] != as_object_id(project_id)]
        assert not wrong, f'{len(wrong)} file(s) name a different project'

    finally:
        if project_id:
            _cleanup_project(mongo_collection, project_id)


# --------------------------------------------------------------------------
# iter_backlinks -- must see exactly what the deletion path sees
# --------------------------------------------------------------------------

def _project_with_files():
    """A document shaped like a real one, including the awkward bits."""
    return {
        '_id': ObjectId(),
        'tarfile': ObjectId(),
        'runs': {
            'sample_1': [{
                'Sample_name': 'GBM39',
                'AA PNG file': ObjectId(),
                'AA directory': ObjectId(),
                'cnvkit directory': ObjectId(),   # holds ids on prod, 62,651 of them
                'Feature ID': 'amplicon1',        # not a file key
            }],
            # A row with no Sample_name: the runs key is the only name available.
            'sample_2': [{'AA PNG file': ObjectId()}],
        },
    }


def test_iter_backlinks_sees_exactly_what_the_deletion_path_sees():
    """The anti-drift guard.

    A second key list that falls behind is the defect this codebase produces
    most often -- one was 8 keys behind and made 80,170 live files look like
    garbage. iter_backlinks() must never see fewer files than the deletion
    path, or the backfill would leave rows unlabelled and they would later read
    as stranded.
    """
    from caper.gridfs_backlinks import iter_backlinks
    from caper.project_version_cleanup import iter_gridfs_file_ids

    doc = _project_with_files()
    assert {fid for fid, _, _ in iter_backlinks(doc)} == set(iter_gridfs_file_ids(doc))


def test_backlinks_carry_the_sample_name_not_the_runs_key():
    from caper.gridfs_backlinks import iter_backlinks

    doc = _project_with_files()
    by_id = {fid: (sample, key) for fid, sample, key in iter_backlinks(doc)}
    png = doc['runs']['sample_1'][0]['AA PNG file']
    assert by_id[png] == ('GBM39', 'AA PNG file')


def test_a_row_without_a_sample_name_falls_back_to_the_runs_key():
    from caper.gridfs_backlinks import iter_backlinks

    doc = _project_with_files()
    by_id = {fid: sample for fid, sample, _ in iter_backlinks(doc)}
    assert by_id[doc['runs']['sample_2'][0]['AA PNG file']] == 'sample_2'


def test_the_tarball_is_labelled_too():
    from caper.gridfs_backlinks import iter_backlinks

    doc = _project_with_files()
    by_id = {fid: key for fid, _, key in iter_backlinks(doc)}
    assert by_id[doc['tarfile']] == 'tarfile'
