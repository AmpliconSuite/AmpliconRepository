"""Removing what a failed upload stored, without removing anything else.

Written as refutations. The rule this code bends -- a backlink is provenance
and never authority -- was broken once already, in a tool whose green tests
never exercised the production path. So each test here is an attempt to make
the cleanup delete something it must not.
"""

from bson import ObjectId

from caper.upload_cleanup import discard_failed_upload_payload


class _Files:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, query, projection=None):
        want = query.get('metadata.project_id')
        for row in self.rows:
            if (row.get('metadata') or {}).get('project_id') == want:
                yield {'_id': row['_id']}


class _Projects:
    def __init__(self, docs):
        self.docs = {d['_id']: d for d in docs}

    def find_one(self, query, projection=None):
        return self.docs.get(query.get('_id'))


def _file(project_id):
    return {'_id': ObjectId(), 'metadata': {'project_id': str(project_id)}}


def test_it_deletes_what_the_failed_upload_stored():
    pid = ObjectId()
    rows = [_file(pid) for _ in range(4)]
    deleted = []

    n = discard_failed_upload_payload(
        _Projects([{'_id': pid, 'aggregation_failed': True}]),
        _Files(rows), deleted.append, pid)

    assert n == 4
    assert set(deleted) == {r['_id'] for r in rows}


def test_a_file_the_placeholder_names_is_never_deleted():
    """The loop may have got far enough to record some files. Those are the
    project's, and a retry or a partial result depends on them."""
    pid = ObjectId()
    rows = [_file(pid) for _ in range(3)]
    named = rows[0]['_id']
    deleted = []

    n = discard_failed_upload_payload(
        _Projects([{'_id': pid, 'aggregation_failed': True,
                    'runs': {'s0': [{'AA PNG file': named}]}}]),
        _Files(rows), deleted.append, pid)

    assert n == 2
    assert named not in deleted


def test_the_rollback_target_never_loses_payload():
    """An edit that fails restores the previous version. If that version's
    files were swept as part of cleaning up the failed one, the rollback would
    restore a project with no data -- worse than the failure it recovers from."""
    failed, old = ObjectId(), ObjectId()
    shared = _file(failed)
    rows = [shared] + [_file(failed) for _ in range(2)]
    deleted = []

    n = discard_failed_upload_payload(
        _Projects([{'_id': failed, 'aggregation_failed': True},
                   {'_id': old, 'tarfile': shared['_id']}]),
        _Files(rows), deleted.append, failed, rollback_project_id=old)

    assert n == 2
    assert shared['_id'] not in deleted


def test_another_uploads_files_are_never_touched():
    mine, theirs = ObjectId(), ObjectId()
    rows = [_file(mine), _file(theirs), _file(theirs)]
    deleted = []

    discard_failed_upload_payload(
        _Projects([{'_id': mine}]), _Files(rows), deleted.append, mine)

    assert deleted == [rows[0]['_id']]


def test_an_unlabelled_file_is_never_touched():
    """Files written before put_with_backlink carry no metadata. They are not
    this upload's, and the query must not reach them."""
    pid = ObjectId()
    rows = [{'_id': ObjectId()}, {'_id': ObjectId(), 'metadata': {}}]
    deleted = []

    assert discard_failed_upload_payload(
        _Projects([{'_id': pid}]), _Files(rows), deleted.append, pid) == 0
    assert deleted == []


def test_a_delete_that_raises_costs_one_file_not_the_handler():
    """This runs inside a failure handler. An exception here would replace the
    error the user needs to see with one about cleaning up."""
    pid = ObjectId()
    rows = [_file(pid) for _ in range(3)]
    deleted = []

    def flaky(file_id):
        if file_id == rows[1]['_id']:
            raise RuntimeError('socket timeout')
        deleted.append(file_id)

    n = discard_failed_upload_payload(
        _Projects([{'_id': pid}]), _Files(rows), flaky, pid)

    assert n == 2
    assert len(deleted) == 2


def test_a_broken_projects_handle_never_raises_out():
    pid = ObjectId()

    class _Exploding:
        def find_one(self, *a, **k):
            raise RuntimeError('database is gone')

    class _ExplodingFiles:
        def find(self, *a, **k):
            raise RuntimeError('database is gone')

    assert discard_failed_upload_payload(
        _Exploding(), _ExplodingFiles(), lambda _i: None, pid) == 0


# ---------------------------------------------------------------------------
# The wiring, asserted against views.py itself
# ---------------------------------------------------------------------------

def _views_source():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, 'caper', 'caper', 'views.py')) as handle:
        return handle.read().split('\n')


def test_every_failure_site_discards_the_payload():
    """A seventh failure path added later must fail this, not leak silently.

    Unit tests above prove the function is right. They say nothing about
    whether the upload's failure paths reach it, and that distinction is not
    theoretical here: a green suite that rebuilt the production path by hand is
    exactly what hid an authority inversion in the residue cleanup on
    2026-09-02.
    """
    lines = _views_source()
    sites = [n for n, line in enumerate(lines)
             if "'aggregation_failed': True" in line]
    assert sites, 'no failure sites found -- has the marker been renamed?'

    unguarded = []
    for n in sites:
        # Proximity, not scoping: the discard call follows the update that
        # marks the placeholder, and the widest gap today is the rollback path,
        # where the call comes after the enclosing try/except closes. The window
        # errs toward passing on purpose -- its job is to catch a new failure
        # path that discards nothing anywhere near it, not to police layout.
        window = '\n'.join(lines[n:n + 30])
        if '_discard_failed_upload(' not in window:
            unguarded.append((n + 1, lines[n].strip()))

    assert not unguarded, (
        'these failure sites mark the upload failed and leave its payload '
        'behind:\n' + '\n'.join('  views.py:%d: %s' % row for row in unguarded))


def test_the_discard_helper_uses_the_primary_and_the_batched_deleter():
    """Two things a rewrite could quietly drop.

    The read has to be from the primary -- the cluster serves secondary reads,
    and a placeholder written moments ago may not be on a replica yet, which
    would make the cleanup think the document names nothing and delete
    everything. And the deleter has to be the batched one: a project tarfile
    runs to gigabytes and one delete_many over its chunks exceeds the driver's
    socket timeout.
    """
    lines = _views_source()
    start = next(n for n, line in enumerate(lines)
                 if line.startswith('def _discard_failed_upload('))
    body = '\n'.join(lines[start:start + 14])

    assert 'collection_handle_primary' in body, 'reads must come from the primary'
    assert 'delete_gridfs_file' in body, 'must use the batched deleter'
