"""How much of GridFS is still owned by a project, counted from the documents.

``gridfs_backlinks`` makes "is *this* file orphaned?" answerable from the file.
This module answers the collection-wide question -- "how much of the storage is
still owned, and how much is residue?" -- and it deliberately does **not**
depend on the backlinks being there. The authority is the same in both
directions: a file is owned because a retained document names it.

That matters for two reasons. A survey that only worked after a migration could
not measure the database the migration is about to run on. And a survey computed
the same way the backlinks were written could only ever agree with them; taking
the long way round -- walking the documents -- is what makes the agreement
between the two worth reporting.

The accounting is a partition of ``fs.files``. Every row lands in exactly one
bucket, and the buckets sum to the collection count:

  owned-by-a-live-document   a retained document names it. Never deletable.
  owned-by-a-tombstone       a TOMBSTONE names it: its payload should have been
                             purged, so each of these is a deletion that did not
                             finish.
  residue                    no document names it. Everything recoverable is
                             recovered from here, and nothing else is.

``residue`` is the number worth watching, and on its own it says nothing about
what to do. Backlinks split it into cases that differ in what they mean:

  residue/document-gone            a backlink names a document that no longer
                                   exists: residue of a purge.
  residue/unreferenced            a backlink names a document that still exists
                                   but no longer names the file: residue of a
                                   version edit.
  residue/unlabelled              no backlink at all. Before the backfill has
                                   run every row is here, and that is not a
                                   finding -- it is the question not yet asked.

**Nothing here deletes, and no caller may delete from these counts alone.** The
two production incidents behind this work were both traversal bugs inside a
count exactly like this one: 84 of 345 documents called orphaned when 77 still
held a payload, and 80,170 live files made to look like garbage by a key list
8 spellings behind. A count is evidence for asking the next question.

Named ids that have no ``fs.files`` row at all are counted separately
(``named_absent``): those are documents pointing at storage that is gone, which
is I12's finding rather than a property of the files.
"""

import datetime

from .gridfs_backlinks import METADATA_FIELD, PROJECT_ID, iter_backlinks
from .project_status import TOMBSTONE, classify

OWNED_LIVE = 'owned-by-a-live-document'
OWNED_TOMBSTONE = 'owned-by-a-tombstone'
RESIDUE_DOCUMENT_GONE = 'residue/document-gone'
RESIDUE_UNREFERENCED = 'residue/unreferenced'
RESIDUE_UNLABELLED = 'residue/unlabelled'

#: Report order: what must be kept first, then residue in decreasing certainty
#: about what it is.
ORDER = (OWNED_LIVE, OWNED_TOMBSTONE, RESIDUE_DOCUMENT_GONE,
         RESIDUE_UNREFERENCED, RESIDUE_UNLABELLED)

BACKLINK_KEY = f'{METADATA_FIELD}.{PROJECT_ID}'

#: ``$in`` batch size when asking which named ids exist. One document can name
#: six figures of files, and a single ``$in`` that large is a request no server
#: should be asked to plan.
CHUNK = 2000


def _chunked(items, size=CHUNK):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def survey(projects, fs_files, *, with_bytes=True, progress=None):
    """Walk every project document and account for every ``fs.files`` row.

    *projects* and *fs_files* are collections. *progress*, if given, is called
    with ``(documents_done, files_seen)`` every 25 documents so a long run can
    say where it is.

    Returns a plain dict, so that a caller can store the result as a snapshot
    and render it later without this module being involved.
    """
    counts = dict.fromkeys(ORDER, 0)
    size = dict.fromkeys(ORDER, 0)
    per_project = []
    tombstones_holding = []

    named_absent = 0             # ids a document names that have no row
    shared_rows = 0              # rows a second document also names
    backlink_agrees = 0          # named row whose backlink names that document
    backlink_disagrees = 0       # named row whose backlink names another
    backlink_missing = 0         # named row carrying no backlink at all
    documents = 0

    # A row must land in exactly one bucket however many documents name it, or
    # the buckets stop summing to the collection and every percentage below is
    # nonsense -- the local fixtures, which deliberately share ids across
    # documents, reported 206% owned before this was tracked. ``counted`` is
    # therefore per row, not per (document, row) pair.
    #
    # Sharing also has a precedence: a file named by a live document is not
    # deletable, whatever else names it. ``held_by_tombstone`` is the small set
    # that lets a live claim take a row back from a tombstone in a single pass;
    # it stays small because a tombstone holding files is the bug being hunted,
    # not the normal case.
    counted = set()
    held_by_tombstone = {}

    for document in projects.find({}):
        documents += 1
        doc_id = document['_id']
        is_tombstone = classify(document) == TOMBSTONE
        label = OWNED_TOMBSTONE if is_tombstone else OWNED_LIVE

        named = {file_id for file_id, _, _ in iter_backlinks(document)}
        present = 0
        project_bytes = 0
        for batch in _chunked(named):
            projection = {'length': 1, BACKLINK_KEY: 1}
            for row in fs_files.find({'_id': {'$in': batch}}, projection):
                file_id = row['_id']
                length = row.get('length') or 0
                present += 1
                project_bytes += length

                if file_id not in counted:
                    counted.add(file_id)
                    counts[label] += 1
                    size[label] += length
                    if is_tombstone:
                        held_by_tombstone[file_id] = length
                else:
                    shared_rows += 1
                    if not is_tombstone and file_id in held_by_tombstone:
                        held = held_by_tombstone.pop(file_id)
                        counts[OWNED_TOMBSTONE] -= 1
                        size[OWNED_TOMBSTONE] -= held
                        counts[OWNED_LIVE] += 1
                        size[OWNED_LIVE] += held

                linked = (row.get(METADATA_FIELD) or {}).get(PROJECT_ID)
                if linked is None:
                    backlink_missing += 1
                elif linked == doc_id:
                    backlink_agrees += 1
                else:
                    backlink_disagrees += 1

        named_absent += len(named) - present

        if named or is_tombstone:
            per_project.append({
                'project_id': str(doc_id),
                'project_name': document.get('project_name'),
                'tombstone': is_tombstone,
                'named': len(named),
                'present': present,
                'bytes': project_bytes,
            })
        if is_tombstone and present:
            tombstones_holding.append(per_project[-1])

        if progress and documents % 25 == 0:
            progress(documents, len(counted))

    owned = len(counted)
    total_files, total_bytes = _collection_size(fs_files, with_bytes)
    residue = max(total_files - owned, 0)
    residue_bytes = max(total_bytes - sum(size.values()), 0) if with_bytes else 0

    # Split the residue with the backlinks, where there are any. Every row
    # counted here is by construction one no document names: the ids that are
    # named were all just seen above.
    gone, unreferenced = _split_residue(projects, fs_files, counted, with_bytes)
    counts[RESIDUE_DOCUMENT_GONE], size[RESIDUE_DOCUMENT_GONE] = gone
    counts[RESIDUE_UNREFERENCED], size[RESIDUE_UNREFERENCED] = unreferenced

    labelled_residue = counts[RESIDUE_DOCUMENT_GONE] + counts[RESIDUE_UNREFERENCED]
    counts[RESIDUE_UNLABELLED] = max(residue - labelled_residue, 0)
    size[RESIDUE_UNLABELLED] = max(
        residue_bytes - size[RESIDUE_DOCUMENT_GONE] - size[RESIDUE_UNREFERENCED], 0)

    labelled_rows = fs_files.count_documents({BACKLINK_KEY: {'$exists': True}})

    per_project.sort(key=lambda row: row['bytes'], reverse=True)
    return {
        'measured_at': datetime.datetime.now(datetime.timezone.utc),
        'documents': documents,
        'total_files': total_files,
        'total_bytes': total_bytes,
        'counts': counts,
        'bytes': size,
        'owned': owned,
        'residue': residue,
        'residue_bytes': residue_bytes,
        'named_absent': named_absent,
        'shared_rows': shared_rows,
        'labelled_rows': labelled_rows,
        'backlink_agrees': backlink_agrees,
        'backlink_disagrees': backlink_disagrees,
        'backlink_missing': backlink_missing,
        'tombstones_holding': tombstones_holding,
        'per_project': per_project,
        'with_bytes': with_bytes,
    }


def _collection_size(fs_files, with_bytes):
    """``(rows, bytes)`` for the whole collection.

    ``estimated_document_count`` reads collection metadata rather than counting,
    which is the wrong trade here: the residue is a subtraction, so a count that
    is merely close makes every bucket below it merely close too.
    """
    if not with_bytes:
        return fs_files.count_documents({}), 0
    cursor = fs_files.aggregate(
        [{'$group': {'_id': None, 'n': {'$sum': 1}, 'b': {'$sum': '$length'}}}],
        allowDiskUse=True)
    for row in cursor:
        return row['n'], row['b'] or 0
    return 0, 0


def _split_residue(projects, fs_files, named_ids, with_bytes):
    """``((gone_n, gone_b), (unreferenced_n, unreferenced_b))`` from the backlinks.

    A labelled row is residue when no document named it, so this walks the
    labelled rows of each project and skips the ones already accounted for.
    With no backlinks written yet, ``distinct`` returns nothing and both
    buckets are zero -- which is the truthful answer, not a clean bill.
    """
    gone_n = gone_b = unref_n = unref_b = 0
    for project_id in fs_files.distinct(BACKLINK_KEY):
        if project_id is None:
            continue
        exists = projects.count_documents({'_id': project_id}, limit=1) > 0
        for row in fs_files.find({BACKLINK_KEY: project_id}, {'length': 1}):
            if row['_id'] in named_ids:
                continue
            length = (row.get('length') or 0) if with_bytes else 0
            if exists:
                unref_n += 1
                unref_b += length
            else:
                gone_n += 1
                gone_b += length
    return (gone_n, gone_b), (unref_n, unref_b)


def human(num_bytes):
    """Byte count as a short string. Storage is the reason anyone reads this."""
    value = float(num_bytes or 0)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024
