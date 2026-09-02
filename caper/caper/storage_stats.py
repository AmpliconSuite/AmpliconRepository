"""What the databases actually hold, summed across projects, once a day.

Every storage number this project has argued about came from somewhere that
turned out not to mean what it said. ``collStats.unusedStorageSize`` froze
byte-identical across a 4 GiB write on 2026-09-01 and drifted on quiet days;
the cluster's ``VolumeBytesUsed`` did not move at all across 257.80 GiB of
deletion. Neither answers "what is in here and who owns it".

This does, by measuring it: one pass over ``fs.files`` for id and length, then
every project document walked through the application's own
``iter_gridfs_file_ids``. A file's owner is the document that names it --
authority runs documents to files, never the other way round -- so the buckets
below are sums over documents, and anything left over is residue by
subtraction rather than by a second opinion.

It is a snapshot, taken daily and stored, for two reasons. The walk is seconds
of work against hundreds of thousands of rows and has no business running on a
page load. And a single number is not much use: the question that keeps coming
up is which way it is going.
"""

import datetime
import logging

from .project_status import LIVE, SOFT_DELETED, SUPERSEDED, TOMBSTONE, classify
from .project_version_cleanup import iter_gridfs_file_ids
from .utils import db_handle, db_handle_primary, is_project_private


COLLECTION = 'storage_statistics'

# The buckets, in the order they are shown. Every GridFS file lands in exactly
# one of them, so they sum to the total and residue needs no separate walk.
BUCKETS = (
    ('live', 'Current versions'),
    ('superseded', 'Older versions'),
    ('soft_deleted', 'Soft deleted, awaiting purge'),
    ('tombstone', 'Tombstones'),
    ('detached', 'Detached documents'),
    ('residue', 'Owned by no document'),
)

_STATUS_BUCKET = {
    LIVE: 'live',
    SUPERSEDED: 'superseded',
    SOFT_DELETED: 'soft_deleted',
    TOMBSTONE: 'tombstone',
}


def measure(database=None):
    """Walk the database and return one snapshot. Seconds, not milliseconds."""
    database = database if database is not None else db_handle
    lengths = {row['_id']: row.get('length', 0)
               for row in database['fs.files'].find({}, {'length': 1})}

    buckets = {key: {'files': 0, 'bytes': 0} for key, _label in BUCKETS}
    documents = {key: 0 for key in buckets}
    # 'listed' and 'restricted' rather than public/private, because
    # 'hidden_public' is public in the enum and restricted for access control,
    # and because a template reading a key called 'private' trips the guard
    # that exists to stop templates reading the raw visibility field.
    visibility = {'listed': {'files': 0, 'bytes': 0},
                  'restricted': {'files': 0, 'bytes': 0}}
    seen = set()
    duplicated_bytes = 0
    projects = 0

    for project in database['projects'].find({}):
        projects += 1
        bucket = _STATUS_BUCKET.get(classify(project), 'detached')
        documents[bucket] += 1
        # is_project_private() takes the visibility value, not the document --
        # and 'private' is an enum of strings with a legacy boolean spelling,
        # never a plain bool, which is why the value goes through the
        # application's own reader rather than being tested here.
        try:
            private = is_project_private(project.get('private'))
        except Exception:
            # Unreadable visibility counts as private: showing a public total
            # that includes something restricted is the worse error.
            private = True
        for file_id in set(iter_gridfs_file_ids(project)):
            size = lengths.get(file_id)
            if size is None:
                # Named by a document but absent from fs.files: invariant I12's
                # finding, and it has no bytes to attribute.
                continue
            if file_id in seen:
                # A second document naming the same file. Measured 2026-09-02
                # this is zero on both databases, and it stops being zero the
                # day identical content is stored once.
                duplicated_bytes += size
                continue
            seen.add(file_id)
            buckets[bucket]['files'] += 1
            buckets[bucket]['bytes'] += size
            side = 'restricted' if private else 'listed'
            visibility[side]['files'] += 1
            visibility[side]['bytes'] += size

    for file_id, size in lengths.items():
        if file_id not in seen:
            buckets['residue']['files'] += 1
            buckets['residue']['bytes'] += size

    return {
        'taken_at': datetime.datetime.now(datetime.timezone.utc),
        'day': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d'),
        'database': database.name,
        'projects': projects,
        'files': len(lengths),
        'bytes': sum(lengths.values()),
        'chunks': database['fs.chunks'].estimated_document_count(),
        'buckets': buckets,
        'documents': documents,
        'visibility': visibility,
        'shared_bytes': duplicated_bytes,
    }


def record(database=None, force=False):
    """Store today's snapshot. Returns it, or None if today already has one.

    One per day, keyed by the day, so a re-run is not a second row and the
    chart cannot double-count. ``force`` overwrites it, which is what a manual
    regenerate does after something large has been deleted.
    """
    handle = db_handle_primary[COLLECTION]
    day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    name = (database if database is not None else db_handle).name
    if not force and handle.find_one({'day': day, 'database': name}):
        return None
    snapshot = measure(database)
    handle.replace_one({'day': day, 'database': name}, snapshot, upsert=True)
    return snapshot


def latest(database=None):
    name = (database if database is not None else db_handle).name
    return db_handle[COLLECTION].find_one({'database': name},
                                          sort=[('day', -1)])


def history(days=90, database=None):
    """Snapshots oldest-first, for the chart."""
    name = (database if database is not None else db_handle).name
    rows = list(db_handle[COLLECTION].find({'database': name})
                .sort('day', -1).limit(days))
    rows.reverse()
    return rows


def sparkline(rows, key=lambda row: row.get('bytes', 0),
              width=720, height=120):
    """An inline SVG area chart. Returns (points, area, labels) for a template.

    Inline and hand-built because the alternative is a charting library on a
    page that needs one line, and because the y-axis here has to start at zero:
    a chart auto-scaled to its own range turns a 0.3% wobble into a cliff,
    which is exactly the mistake already made once by reading a 70-minute
    square wave in VolumeBytesUsed as the start of reclamation.
    """
    values = [key(row) for row in rows]
    if not values:
        return {'points': '', 'area': '', 'max': 0, 'rows': []}
    top = max(values) or 1
    step = width / max(len(values) - 1, 1)
    coords = [(index * step, height - (value / top) * (height - 8))
              for index, value in enumerate(values)]
    points = ' '.join('%.1f,%.1f' % point for point in coords)
    area = 'M0,%s L%s L%.1f,%s Z' % (height, points.replace(' ', ' L'),
                                     coords[-1][0], height)
    return {'points': points, 'area': area, 'max': top, 'rows': rows}


def record_quietly(database=None):
    """record(), with any failure logged rather than raised.

    Called from a page render, where a storage snapshot failing must not be
    what takes the admin page down.
    """
    try:
        return record(database)
    except Exception:
        logging.exception('Could not record the daily storage snapshot')
        return None
