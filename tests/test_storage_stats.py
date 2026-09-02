"""The storage breakdown shown on the admin statistics page.

The property that matters is that the buckets partition the files: every
GridFS file lands in exactly one, so the rows sum to the total and residue is
what is left rather than a second opinion. A breakdown whose parts do not sum
to the whole is worse than none, because it invites arithmetic that is wrong.
"""

from bson import ObjectId

from caper import storage_stats


class _Collection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query=None, projection=None):
        for doc in self.docs:
            yield dict(doc)

    def estimated_document_count(self):
        return len(self.docs)


class _Database:
    def __init__(self, name, projects, files, chunks=0):
        self.name = name
        self._c = {'projects': _Collection(projects),
                   'fs.files': _Collection(files),
                   'fs.chunks': _Collection([{} for _ in range(chunks)])}

    def __getitem__(self, key):
        return self._c[key]


def _file(size):
    return {'_id': ObjectId(), 'length': size}


def test_the_buckets_partition_every_file():
    live_file, old_file, orphan = _file(100), _file(200), _file(400)
    db = _Database('caper-test', [
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'public', 'tarfile': live_file['_id']},
        {'_id': ObjectId(), 'delete': True, 'current': False,
         'private': 'public', 'tarfile': old_file['_id']},
    ], [live_file, old_file, orphan])

    snapshot = storage_stats.measure(db)

    assert snapshot['bytes'] == 700
    assert snapshot['files'] == 3
    assert sum(b['bytes'] for b in snapshot['buckets'].values()) == 700
    assert sum(b['files'] for b in snapshot['buckets'].values()) == 3
    assert snapshot['buckets']['live']['bytes'] == 100
    assert snapshot['buckets']['superseded']['bytes'] == 200
    assert snapshot['buckets']['residue']['bytes'] == 400
    assert snapshot['buckets']['residue']['files'] == 1


def test_a_file_two_documents_name_is_counted_once():
    """Zero on both databases today, and the whole point of #624 is to change
    that. The total must not double when it does."""
    shared = _file(500)
    db = _Database('caper-test', [
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'public', 'tarfile': shared['_id']},
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'public', 'tarfile': shared['_id']},
    ], [shared])

    snapshot = storage_stats.measure(db)

    assert snapshot['bytes'] == 500
    assert sum(b['bytes'] for b in snapshot['buckets'].values()) == 500
    assert snapshot['shared_bytes'] == 500, 'the second naming is reported'
    assert snapshot['buckets']['residue']['files'] == 0


def test_a_named_file_with_no_fs_files_row_contributes_nothing():
    """Invariant I12's finding. It has no bytes, so it cannot have a share."""
    real = _file(100)
    db = _Database('caper-test', [
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'public', 'tarfile': real['_id'],
         'Run metadata JSON': ObjectId()},
    ], [real])

    snapshot = storage_stats.measure(db)

    assert snapshot['bytes'] == 100
    assert sum(b['bytes'] for b in snapshot['buckets'].values()) == 100


def test_visibility_follows_the_owning_document():
    public_file, private_file = _file(10), _file(20)
    db = _Database('caper-test', [
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'public', 'tarfile': public_file['_id']},
        {'_id': ObjectId(), 'delete': False, 'current': True,
         'private': 'private', 'tarfile': private_file['_id']},
    ], [public_file, private_file])

    snapshot = storage_stats.measure(db)

    assert snapshot['visibility']['listed']['bytes'] == 10
    assert snapshot['visibility']['restricted']['bytes'] == 20


def test_the_chart_scales_from_zero():
    """A chart auto-scaled to its own range turns a 0.3% wobble into a cliff,
    which is the reading error already made once on VolumeBytesUsed."""
    rows = [{'bytes': 1000}, {'bytes': 1003}, {'bytes': 1001}]
    chart = storage_stats.sparkline(rows, height=100)

    ys = [float(point.split(',')[1]) for point in chart['points'].split()]
    assert max(ys) - min(ys) < 1.0, (
        'a 0.3%% change should be a flat line, not a cliff: %s' % chart['points'])
    assert chart['max'] == 1003


def test_the_chart_is_empty_rather_than_broken_with_no_history():
    chart = storage_stats.sparkline([])
    assert chart['points'] == ''
    assert chart['rows'] == []
