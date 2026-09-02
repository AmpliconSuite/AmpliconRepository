"""Backlinks from GridFS files to the documents that name them.

**The question this exists to make answerable:** is *this one file* orphaned?

Today that costs a traversal of every project document -- on prod, 345
documents and 232 MiB, to build a set of 948,515 ids and diff it against
1,065,019 ``fs.files`` rows. There is no way to ask about a single file, because
the reachability graph runs one way: documents name files, files name nothing.
Both production incidents behind this work happened *inside* that traversal, and
both were traversal bugs -- one of them worth 80,170 files.

A backlink in ``fs.files.metadata`` turns the whole-database traversal into a
``find_one``.

**Authority runs documents -> files, exactly as it does for the chain view.**
A file is retained because a retained document names it. The metadata is an
index into that fact and never a substitute for it: nothing may decide to delete
a file because its metadata says it is orphaned. I12 and I13 assert the two
agree, and on disagreement the metadata is rebuilt from the documents.

**Writing it at ``fs.put()`` time is the half that stops the problem growing.**
It also makes the known orphan-factory failure mode self-describing: an
ingestion that dies after storing a file but before updating the document
currently leaves an anonymous file, and with this it leaves one that says what
it was for.

**One owner is what the data has, not a rule this imposes.** No file is named by
two documents today -- on 2026-08-29 the distinct id count exactly equalled the
(document, file) pair count on both databases -- so ``project_id`` is a scalar.
Should a file ever have several owners, the query ``{'metadata.project_id': X}``
already matches an array containing X, so nothing that reads a backlink has to
change; the writer becomes ``$addToSet``. What would be a real piece of work is
deletion, which today deletes every file a project names and would then have to
delete only the ones nothing else names. That work is the same size whatever
shape this field is, so it is not a reason to complicate the field now.

The rule that keeps that future open is the one above: **a backlink is
provenance, never authority.** Nothing may delete a file because its metadata
says so.

What is known at ``put()`` time varies by call site, and this writes what is
known rather than blocking on what is not. A file uploaded before its project
document exists carries the *intended* project id; the backfill completes and
corrects everything from the documents afterwards, which it can do because the
documents are the authority.
"""

import datetime
import logging

from bson.objectid import ObjectId

# Where the backlink lives on an fs.files row.
METADATA_FIELD = 'metadata'
PROJECT_ID = 'project_id'

# The orphan classifications from the spec, in increasing order of "safe to
# remove". Nothing here deletes anything; these are labels for a report.
LIVE_FILE = 'live'
"""The named document exists and still references this file. Never delete."""

UNREFERENCED_BY_ITS_DOCUMENT = 'unreferenced-by-its-document'
"""The named document exists but no longer references the file -- residue of a
version edit. Deletable once that document's status is confirmed."""

DOCUMENT_GONE = 'document-gone'
"""The named document no longer exists: residue of a purge or permanent
delete."""

TOMBSTONE_PAYLOAD = 'tombstone-payload'
"""The named document is a TOMBSTONE, whose payload was supposed to have been
purged and was not. Deletable -- **and a bug worth reporting**, because a
tombstone holding files means a deletion path did not finish."""

UNLABELLED = 'unlabelled'
"""No backlink at all: written before this change, or by an ingestion that
crashed before recording it. Only after the backfill has run does this mean
"genuinely stranded"; before then it means nothing."""


def as_object_id(value):
    """*value* as an ObjectId, or None when it is not one.

    Ids reach this module as both ObjectId and str depending on the call site,
    and a metadata field that is sometimes one and sometimes the other makes
    every later query wrong for half the rows -- which is the shape of defect
    this whole area keeps producing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:                              # noqa: BLE001 -- not an id
        return None


def build_metadata(project_id, *, sample_name=None, feature_key=None,
                   version_chain_id=None, event_id=None, written_at=None):
    """The backlink subdocument for one file.

    Keys whose value is unknown are omitted rather than stored as None, so that
    ``{'metadata.project_id': {'$exists': True}}`` means what it says and the
    backfill can find rows that need completing.
    """
    metadata = {}
    project = as_object_id(project_id)
    if project is not None:
        metadata[PROJECT_ID] = project
    chain = as_object_id(version_chain_id)
    if chain is not None:
        metadata['version_chain_id'] = chain
    event = as_object_id(event_id)
    if event is not None:
        metadata['written_by_event'] = event
    if sample_name:
        metadata['sample_name'] = str(sample_name)
    if feature_key:
        metadata['feature_key'] = str(feature_key)
    if metadata:
        metadata['written_at'] = written_at or datetime.datetime.now(
            datetime.timezone.utc)
    return metadata


def put_with_backlink(fs_handle, fileobj, *, project_id, sample_name=None,
                      feature_key=None, version_chain_id=None, event_id=None,
                      filename=None):
    """``fs.put()``, carrying a backlink to the document that will name the file.

    **A backlink is never worth a file.** If building the metadata raises for
    any reason, the file is still stored without it -- an unlabelled file is a
    row the backfill will complete, while a failed upload is data the user
    loses. The failure is logged rather than swallowed silently, because a
    metadata write that fails every time should be visible.
    """
    try:
        metadata = build_metadata(
            project_id, sample_name=sample_name, feature_key=feature_key,
            version_chain_id=version_chain_id, event_id=event_id)
    except Exception as exc:                       # noqa: BLE001
        logging.warning(f'GridFS backlink could not be built for '
                        f'{feature_key!r} of project {project_id}: '
                        f'{type(exc).__name__}: {exc}')
        metadata = {}

    kwargs = {}
    if filename is not None:
        kwargs['filename'] = filename
    if metadata:
        kwargs[METADATA_FIELD] = metadata
    return fs_handle.put(fileobj, **kwargs)


def classify_file(file_id, metadata, document, referenced_ids):
    """Label one ``fs.files`` row. Pure; decides nothing about deletion.

    *file_id* is the row's ``_id``, *metadata* its metadata subdocument (or
    None), *document* the project document its ``project_id`` names (or None if
    that document no longer exists), and *referenced_ids* the set of GridFS ids
    that document currently names.

    The membership test is the whole point and is easy to get subtly wrong:
    the question is whether the document still names **this** file, not whether
    it names any files at all. A document that has been re-aggregated names a
    full set of new ids and none of the old ones, which is exactly the
    "residue of a version edit" case.

    Deliberately takes the document rather than looking it up: the caller holds
    the database, and a classifier that queries is one that cannot be tested
    over the awkward cases.
    """
    from .project_status import TOMBSTONE, classify

    if not metadata or metadata.get(PROJECT_ID) is None:
        return UNLABELLED
    if document is None:
        return DOCUMENT_GONE
    if classify(document) == TOMBSTONE:
        return TOMBSTONE_PAYLOAD
    # Membership is asked once per fs.files row, against a set that can hold six
    # figures of ids, so the normalising comprehension below cannot be the first
    # thing tried: rebuilding it per row is quadratic in the collection, and a
    # report over 931,262 prod rows against a 71,536-id document did not finish.
    # `iter_backlinks` already yields ObjectIds, so the direct test answers
    # almost every call in constant time; the slow path stays for callers that
    # pass ids as strings.
    referenced_ids = referenced_ids or ()
    file_id = as_object_id(file_id)
    if file_id in referenced_ids:
        return LIVE_FILE
    if any(as_object_id(i) == file_id for i in referenced_ids):
        return LIVE_FILE
    return UNREFERENCED_BY_ITS_DOCUMENT


def iter_backlinks(document):
    """``(file_id, sample_name, feature_key)`` for every GridFS file *document* names.

    The same traversal as ``iter_gridfs_file_ids()``, carrying the context that
    makes a backlink useful -- which sample and which feature slot the file came
    from. It delegates the "is this key a GridFS slot?" decision to
    ``GRIDFS_FILE_KEYS`` rather than keeping a list of its own, because a second
    key list that drifts is the defect this codebase produces most often: one
    such list was 8 keys behind and made 80,170 live files look like garbage.

    ``tests/test_gridfs_backlinks.py`` asserts the ids yielded here are exactly
    those ``iter_gridfs_file_ids()`` yields, so the two cannot come apart.
    """
    from .project_version_cleanup import GRIDFS_FILE_KEYS, iter_gridfs_file_ids

    def walk(value, parent_key, sample_name):
        if parent_key in GRIDFS_FILE_KEYS:
            for oid in iter_gridfs_file_ids(value, parent_key):
                yield oid, sample_name, parent_key
            return
        if isinstance(value, dict):
            # A feature row names its own sample; the runs key is positional
            # ('sample_1') and is only a fallback for rows that lack the name.
            name = value.get('Sample_name') or sample_name
            for key, child in value.items():
                yield from walk(child, key, name)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                yield from walk(child, parent_key, sample_name)

    runs = document.get('runs') or {}
    for run_key, features in runs.items():
        yield from walk(features, 'runs', run_key)

    for key, value in document.items():
        if key == 'runs':
            continue
        yield from walk(value, key, None)
