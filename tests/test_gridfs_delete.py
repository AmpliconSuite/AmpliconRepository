"""
Deleting a GridFS file without tripping the driver's socket timeout.

gridfs.GridFS.delete() removes a file's chunks in a single delete_many.  On the
admin delete page that command ran past the 120 second socket timeout for the
largest project on dev: the driver raised NetworkTimeout while DocumentDB went
on deleting, so the operator was told the delete had failed for work that in
fact succeeded, and the project document survived with its payload destroyed.
Measured on dev 2026-08-27, the two biggest rows on that page are 9,270 and
7,233 chunks; the one that failed left 0 chunks behind.

delete_gridfs_file() removes the chunks in bounded batches instead, so no
single command approaches the timeout and an interrupted delete resumes rather
than restarting.
"""

import pytest


@pytest.fixture
def gridfs_file(request):
    """A GridFS file of *n* chunks, removed again whatever the test does."""
    from bson import ObjectId
    from caper.utils import fs_handle, gridfs_chunks_handle, gridfs_files_handle

    written = []

    def _cleanup():
        for file_id in written:
            gridfs_files_handle.delete_one({'_id': file_id})
            gridfs_chunks_handle.delete_many({'files_id': file_id})
    request.addfinalizer(_cleanup)

    def make(chunks=5, chunk_size=16):
        file_id = fs_handle.put(b'x' * (chunk_size * chunks),
                                chunkSize=chunk_size,
                                filename='test-delete-gridfs')
        written.append(ObjectId(file_id))
        return file_id

    make.files = gridfs_files_handle
    make.chunks = gridfs_chunks_handle
    return make


def _counts(make, file_id):
    from bson import ObjectId
    oid = ObjectId(str(file_id))
    return (make.files.count_documents({'_id': oid}),
            make.chunks.count_documents({'files_id': oid}))


def test_every_chunk_goes_even_when_there_are_more_than_one_batch(gridfs_file):
    from caper.utils import delete_gridfs_file

    file_id = gridfs_file(chunks=10)
    assert _counts(gridfs_file, file_id) == (1, 10)

    removed = delete_gridfs_file(file_id, batch_size=3)

    assert removed == 10
    assert _counts(gridfs_file, file_id) == (0, 0)


def test_a_string_id_is_accepted(gridfs_file):
    """Callers hold ids as strings -- project['tarfile'] is one."""
    from caper.utils import delete_gridfs_file

    file_id = gridfs_file(chunks=3)

    assert delete_gridfs_file(str(file_id), batch_size=2) == 3
    assert _counts(gridfs_file, file_id) == (0, 0)


def test_an_interrupted_delete_resumes_where_it_stopped(gridfs_file, monkeypatch):
    """
    The point of batching: a failure loses only the batch in flight.  Calling
    again finishes the file rather than starting it over, which is what makes
    the admin page's retry cheap after a timeout.
    """
    from caper import utils
    from caper.utils import delete_gridfs_file

    file_id = gridfs_file(chunks=10)
    real_delete_many = utils.gridfs_chunks_handle.delete_many
    calls = {'n': 0}

    def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 3:
            raise RuntimeError('socket timed out')
        return real_delete_many(*args, **kwargs)

    monkeypatch.setattr(utils.gridfs_chunks_handle, 'delete_many', flaky)

    with pytest.raises(RuntimeError):
        delete_gridfs_file(file_id, batch_size=2)

    monkeypatch.undo()
    _files, remaining = _counts(gridfs_file, file_id)
    assert 0 < remaining < 10, 'the interrupted call deleted all or nothing'

    assert delete_gridfs_file(file_id, batch_size=2) == remaining
    assert _counts(gridfs_file, file_id) == (0, 0)


def test_deleting_a_file_that_is_already_gone_is_not_an_error(gridfs_file):
    """
    The retry path after a timeout lands here: the file document was deleted
    before the chunks, so the second attempt finds nothing to remove.  gridfs
    treats that as success and so does this.
    """
    from caper.utils import delete_gridfs_file

    file_id = gridfs_file(chunks=2)
    delete_gridfs_file(file_id)

    assert delete_gridfs_file(file_id) == 0


def test_the_file_document_goes_before_its_chunks(gridfs_file, monkeypatch):
    """
    A half-deleted file must not be readable as a whole one, so the document
    that names the chunks is removed first -- the order gridfs itself uses.
    """
    from caper import utils
    from caper.utils import delete_gridfs_file

    file_id = gridfs_file(chunks=6)
    order = []

    files_delete_one = utils.gridfs_files_handle.delete_one
    chunks_delete_many = utils.gridfs_chunks_handle.delete_many

    def note_files(*args, **kwargs):
        order.append('files')
        return files_delete_one(*args, **kwargs)

    def note_chunks(*args, **kwargs):
        order.append('chunks')
        return chunks_delete_many(*args, **kwargs)

    monkeypatch.setattr(utils.gridfs_files_handle, 'delete_one', note_files)
    monkeypatch.setattr(utils.gridfs_chunks_handle, 'delete_many', note_chunks)

    delete_gridfs_file(file_id, batch_size=2)

    assert order[0] == 'files'
    assert 'chunks' in order
