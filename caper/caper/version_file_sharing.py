"""A new version reuses its predecessor's identical files instead of storing them again.

**Why.** Every edit of a project -- replace, append, re-aggregate, remove or
rename samples -- writes a whole new version through ``extract_project_files``,
and until this change every file that version names was a fresh GridFS copy.
Since aggregator 8.3.1 and the site's deterministic directory packing (#712),
a re-aggregated version's files are byte-identical to its predecessor's:
measured 2026-11-02 on dev, a 17-sample project re-aggregated twice gave 296 of
296 directory tarballs identical, and every other per-file slot identical as
well. Storing them once is issue #624.

**Scope: one version chain, and only from the immediate predecessor.** That
was decided with the issue: pointers between unrelated projects are too easy to
make stale. A file is therefore only ever named by versions of one project, and
the only version a new one takes files from is the one it was made from.

**Matching.** A file is reused when the predecessor names a file with the same
SHA-256, length and GridFS filename, in the same sample and field. So a reused
file is the same file in the same place, never an identical one from another
sample. The filename is part of the match because the per-sample download names
a directory tarball after it.

The sample and field come from the predecessor *document*, the place it names
the file, and never from the row's metadata. The rows disagree with each other:
measured read-only on prod 2026-11-03, the 228,153 per-sample files the current
versions name carry either the document's underscore spelling of the field
(``Feature_BED_file``, from the August backlink backfill) or the run key
(``sample_1``) where the sample name belongs (from uploads since). Matching on
the metadata would have matched none of them.

The hash is recorded in ``metadata.sha256`` when a file is stored.
A one-time backfill (since retired) added it to files stored before; a file
without one is never matched.

**Provenance stays the writer's.** ``metadata.project_id`` keeps naming the
version that stored the bytes. A version that reuses the file is added to
``metadata.reused_by`` *before* its document names the file. Both are an index
of who might name a file, never the authority on it (see gridfs_backlinks): the
deletion check below reads the documents they point at and decides from those.
Keeping ``project_id`` scalar also keeps ``upload_cleanup`` correct unchanged.
It selects a failed upload's files by that field, and a file the upload reused
names an earlier version there.

**Deletion.** Every application path that deletes a version's payload asks
``files_still_needed`` first, and keeps what it returns:

  * ``delete_project_version`` (all three cases), when it builds the tombstone's
    pending list, and ``version_purge`` again before each batch it deletes;
  * ``admin_permanent_delete_project``, which the account deletion also uses.

The standalone sweeps (``cleanup_failed_upload_residue.py``,
``sweep_gridfs_unreferenced.py``, ``cleanup_orphaned_projects.py``) were
already safe. Each builds its protected set by walking every document, so a
file one survivor names is kept however many others also named it.
"""

import hashlib
import logging

from .gridfs_backlinks import METADATA_FIELD, PROJECT_ID, REUSED_BY, as_object_id, iter_backlinks
from .project_status import TOMBSTONE, classify
from .project_version_cleanup import iter_gridfs_file_ids

#: Where a stored file's content hash lives on its fs.files row.
SHA256 = 'sha256'

#: ``$in`` batch size for fs.files reads; one version can name six figures of files.
CHUNK = 2000

_READ_BLOCK = 1 << 20


def file_digest(fileobj):
    """``(sha256 hex, length)`` of *fileobj*'s contents, leaving it at offset 0."""
    fileobj.seek(0)
    digest, length = hashlib.sha256(), 0
    while True:
        block = fileobj.read(_READ_BLOCK)
        if not block:
            break
        digest.update(block)
        length += len(block)
    fileobj.seek(0)
    return digest.hexdigest(), length


#: Aggregator <= 6 names for what 7.0 and later call by the name on the right.
#: Measured on dev 2026-11-03, re-aggregating a 6.1.0 project with 8.3.1: each
#: pair held identical bytes in 58 of 58 features. The site already names the
#: new file under both (``AA PNG file`` is set to the ``Graph PNG file`` id).
LEGACY_FIELDS = {
    'AA PDF file': 'Graph PDF file',
    'AA PNG file': 'Graph PNG file',
    'AA graph file': 'Graph file',
    'AA cycles file': 'Cycles file',
    'AA directory': 'Reconstruction directory',
}


def field_name(key):
    """The field *key* names, for matching: run.json's spelling, today's name.

    The documents store ``Feature_BED_file`` where run.json says
    ``Feature BED file``, and older aggregators used the names in LEGACY_FIELDS.
    """
    key = str(key).replace('_', ' ')
    return LEGACY_FIELDS.get(key, key)


def _chunked(items, size=CHUNK):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


class PredecessorFiles:
    """The files a new version may take from the version it was made from.

    Built once per upload, before the files are stored. *projects* and
    *fs_files* must read the primary: the new document's predecessor pointer
    was written moments ago by ``lineage.link_new_version``.
    """

    def __init__(self, projects, fs_files, new_project_id):
        self._fs_files = fs_files
        self.new_id = as_object_id(new_project_id)
        self.predecessor_id = None
        self._index = {}
        try:
            self._load(projects)
        except Exception:
            # Sharing is an optimisation. If it cannot be set up, the upload
            # stores every file, exactly as it did before this module.
            logging.exception('Could not read the predecessor of %s; this '
                              'version will store all of its files', new_project_id)
            self._index = {}

    def _load(self, projects):
        if self.new_id is None:
            return
        new_doc = projects.find_one({'_id': self.new_id}, {'previous_version_id': 1})
        predecessor_id = as_object_id((new_doc or {}).get('previous_version_id'))
        if predecessor_id is None:
            return
        predecessor = projects.find_one({'_id': predecessor_id})
        if predecessor is None or classify(predecessor) == TOMBSTONE:
            return
        self.predecessor_id = predecessor_id
        self._index_files(predecessor)

    @classmethod
    def of_version(cls, fs_files, version):
        """The files a version made from *version* could take, with no new version yet.

        For predicting: ``would_reuse`` works on it and writes nothing.
        """
        self = cls.__new__(cls)
        self._fs_files, self.new_id, self.predecessor_id = fs_files, None, version['_id']
        self._index = {}
        self._index_files(version)
        return self

    def would_reuse(self, sha256, length, filename, sample_name, feature_key):
        """The id ``claim`` would return for this file, without recording anything."""
        return self._index.get((sha256, length, filename, sample_name, field_name(feature_key)))

    def _index_files(self, predecessor):
        # Every (sample, field) the predecessor names each file under. One file
        # can sit in two fields: a directory is named as both
        # Reconstruction_directory and its legacy alias AA_directory.
        slots = {}
        for file_id, sample_name, key in iter_backlinks(predecessor):
            slots.setdefault(file_id, set()).add((sample_name, field_name(key)))
        projection = {'length': 1, 'filename': 1, f'{METADATA_FIELD}.{SHA256}': 1}
        for batch in _chunked(slots):
            for row in self._fs_files.find({'_id': {'$in': batch}}, projection):
                sha256 = (row.get(METADATA_FIELD) or {}).get(SHA256)
                if not sha256:
                    continue
                for sample_name, field in slots[row['_id']]:
                    key = (sha256, row.get('length'), row.get('filename'), sample_name, field)
                    self._index.setdefault(key, row['_id'])

    def __len__(self):
        return len(self._index)

    def claim(self, sha256, length, filename, sample_name, feature_key):
        """The id of the predecessor's identical file, now recorded as reused; or None.

        The claim is written before the new document names the file, so that a
        deletion running meanwhile sees this version as a possible owner. A row
        that has gone since the index was built matches nothing and is not used.
        *sample_name* is the feature's ``Sample name``, which the saved document
        keeps as ``Sample_name``; not the run key.
        """
        key = (sha256, length, filename, sample_name, field_name(feature_key))
        file_id = self._index.get(key)
        if file_id is None:
            return None
        try:
            result = self._fs_files.update_one(
                {'_id': file_id},
                {'$addToSet': {f'{METADATA_FIELD}.{REUSED_BY}': self.new_id}})
        except Exception:
            logging.exception('Could not record %s as reused by %s; storing a copy',
                              file_id, self.new_id)
            return None
        if not result.matched_count:
            self._index.pop(key, None)
            return None
        return file_id

    def missing(self, file_ids):
        """Those of *file_ids* that no longer have an fs.files row."""
        wanted = {as_object_id(f) for f in file_ids} - {None}
        present = set()
        for batch in _chunked(wanted):
            present |= {row['_id'] for row in
                        self._fs_files.find({'_id': {'$in': batch}}, {'_id': 1})}
        return wanted - present


def _in_progress(document):
    """True for a version still being written: it may name a file it has claimed."""
    return (document.get('FINISHED?') is False
            and not document.get('aggregation_failed')
            and classify(document) != TOMBSTONE)


def files_still_needed(projects, fs_files, victim, candidate_ids, also_going=()):
    """Which of *candidate_ids* another document still needs. Never deletes.

    *victim* is the document whose payload is being removed (or its tombstone).
    A file is kept when any **other** document names it, or when it is claimed
    by a version still being written. The documents asked are every member of
    the victim's chain, every version its ``previous_versions`` names, and every
    document the candidates' backlinks name (writer and reusers). The backlinks
    only add documents to read; a file is kept or released by what those
    documents contain.

    *also_going* names documents to leave out as well, for a question about a
    delete of several documents at once. The deletes themselves go one document
    at a time and do not pass it.

    Read from the primary: a version that has just claimed a file must not be
    missed because a replica has not seen it yet.
    """
    candidates = {as_object_id(f) for f in candidate_ids} - {None}
    if not candidates:
        return set()
    victim_id = as_object_id((victim or {}).get('_id'))

    to_read = set()
    chain_id = (victim or {}).get('version_chain_id')
    if chain_id is not None:
        to_read |= {doc['_id'] for doc in
                    projects.find({'version_chain_id': chain_id}, {'_id': 1})}
    for entry in (victim or {}).get('previous_versions') or []:
        linkid = entry.get('linkid') if isinstance(entry, dict) else None
        if as_object_id(linkid) is not None:
            to_read.add(as_object_id(linkid))

    claimed_by = {}                        # reuser id -> candidate ids it claimed
    projection = {f'{METADATA_FIELD}.{PROJECT_ID}': 1, f'{METADATA_FIELD}.{REUSED_BY}': 1}
    for batch in _chunked(candidates):
        for row in fs_files.find({'_id': {'$in': batch}}, projection):
            metadata = row.get(METADATA_FIELD) or {}
            writer = as_object_id(metadata.get(PROJECT_ID))
            if writer is not None:
                to_read.add(writer)
            for reuser in metadata.get(REUSED_BY) or []:
                reuser = as_object_id(reuser)
                if reuser is not None:
                    to_read.add(reuser)
                    claimed_by.setdefault(reuser, set()).add(row['_id'])
    to_read.discard(victim_id)
    to_read -= {as_object_id(doc_id) for doc_id in also_going}

    needed = set()
    for doc_id in to_read:
        document = projects.find_one({'_id': doc_id})
        if document is None:
            continue
        needed |= candidates & set(iter_gridfs_file_ids(document))
        if doc_id in claimed_by and _in_progress(document):
            needed |= claimed_by[doc_id]
    return needed


def still_needed_here(victim, candidate_ids, also_going=()):
    """``files_still_needed`` against this site's database, read from the primary."""
    from .utils import collection_handle_primary, db_handle_primary
    return files_still_needed(collection_handle_primary, db_handle_primary['fs.files'],
                              victim, candidate_ids, also_going)
