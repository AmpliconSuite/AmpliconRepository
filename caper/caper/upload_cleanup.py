"""Remove what a failed upload already stored, at the moment it fails.

An upload writes its files into GridFS one at a time and only afterwards
updates the document to name them. Until 2026-09-02 a failure in between left
every file already written with nothing pointing at it: the placeholder was
marked ``aggregation_failed`` and kept, the temp directory was removed, and the
payload stayed. TCGA_Sarcoma's failed upload of 2026-08-14 left 2,695 files
that way, all written inside a 90-second window.

**This closes one of the two ways an upload dies, and only one.** An exception
inside the aggregation thread runs the failure path, and that is what calls
this. A worker that is killed -- the daily 07:12 restart, an OOM, a deploy --
runs no failure path at all, and nothing in this process can help there. The
weekly ``cleanup_failed_upload_residue.py`` sweep is the backstop for that
case, and it is the right architecture for it rather than a gap: the only
thing that can collect after a crash is something that runs later. What this
changes is the window for the case it does cover, from up to a week down to
seconds.

**Why it is safe to select files by their metadata here, when nothing else may.**
The standing rule is that a backlink is provenance and never authority: a file
is retained because a retained document names it, and no tool may delete a file
because its own metadata says it is orphaned. That rule protects against stale
or wrong metadata on files that have existed for months. It does not bind here,
and the reason is narrow enough to state exactly:

  * Every candidate id was minted by *this* upload, seconds ago.
  * A document written before the upload cannot name an id that did not exist
    when it was written.
  * The only documents that could name one are the ones this upload touched:
    its own placeholder, and -- on an edit -- the version being rolled back to.

Both of those are read fresh and their ids are excluded. Anything the metadata
claims but those two documents do not name has no other possible owner. The
measured backdrop is the same one the rule rests on: no GridFS id on either
database is named by more than one document, 0 of 1.55 million on 2026-08-31.
"""

import logging

from bson import ObjectId

from .gridfs_backlinks import METADATA_FIELD, PROJECT_ID, as_object_id
from .project_version_cleanup import iter_gridfs_file_ids


def _named_by(document):
    return {str(file_id) for file_id in iter_gridfs_file_ids(document or {})}


def discard_failed_upload_payload(projects, files, delete_file, project_id,
                                  rollback_project_id=None):
    """Delete this upload's stored files that no surviving document names.

    *delete_file* takes one file id and is the application's batched deleter,
    for the same reason every other delete path uses it: a project tarfile runs
    to gigabytes and a single delete_many over its chunks exceeds the driver's
    socket timeout.

    Returns the number of files deleted. Never raises: this runs inside a
    failure handler, and an exception here would replace the error the user
    needs to see with one about cleaning up.
    """
    deleted = 0
    try:
        keep = set()
        for candidate_id in (project_id, rollback_project_id):
            if not candidate_id:
                continue
            try:
                document = projects.find_one({'_id': ObjectId(str(candidate_id))})
            except Exception:
                document = None
            if document is not None:
                keep |= _named_by(document)

        # Both spellings. build_metadata() stores an ObjectId, and querying
        # for the string matched nothing -- which unit tests with string
        # fixtures could not see, and a drill against the real database found
        # in one run. The names come from gridfs_backlinks rather than being
        # written again here.
        wanted = [value for value in
                  (as_object_id(project_id), str(project_id))
                  if value is not None]
        rows = list(files.find(
            {'%s.%s' % (METADATA_FIELD, PROJECT_ID): {'$in': wanted}},
            {'_id': 1}))
        for row in rows:
            file_id = row['_id']
            if str(file_id) in keep:
                continue
            try:
                delete_file(file_id)
                deleted += 1
            except Exception as error:
                # Warning, not debug. Nothing else will collect this file for a
                # week, and the id is the only way back to the bytes.
                logging.warning(
                    'failed-upload cleanup could not delete GridFS %s of '
                    'project %s: %s: %s', file_id, project_id,
                    type(error).__name__, error)
        if rows:
            logging.info(
                'failed-upload cleanup for project %s: %d file(s) written, '
                '%d removed, %d kept because a document still names them',
                project_id, len(rows), deleted, len(rows) - deleted)
    except Exception:
        logging.exception(
            'failed-upload cleanup raised for project %s; the payload stays '
            'and the weekly sweep will collect it', project_id)
    return deleted
