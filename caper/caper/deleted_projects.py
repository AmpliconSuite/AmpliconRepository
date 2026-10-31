"""
Deleting a project deletes all of it, and its URLs are remembered.

Before 2026-10-30 the Delete button soft-deleted the current version and
nothing else: the older versions stayed SUPERSEDED, and the resolver's
prior-version step kept serving them, page and API, to anyone holding their
links, until an admin permanently deleted the project.  Now an older version
whose project has been deleted resolves to nothing, decided by
``chain_is_deleted()`` at read time rather than by writing to the old versions.
So restoring the head from the admin page reopens every version with it, and
nothing has to be undone version by version.

The URLs are recorded in ``deleted_project_urls`` when the project is deleted,
one row per version, keyed by the version's id: the name it had, its alias, its
visibility, who deleted it and when.  The row outlives the documents -- a
permanent delete leaves it, marked -- so an old link in a paper or an email
can still be answered with "this was project X, deleted on date" instead of a
bare 404, and an admin can tell what a dead id used to be.
"""

import datetime
import logging

from bson import ObjectId

from .project_status import (
    LIVE, SOFT_DELETED, STATUS_FLAG_FIELDS, TOMBSTONE_MARKER_FIELDS, classify,
)
from .visibility import normalize_visibility_field, is_project_private, is_project_hidden_public

COLLECTION = 'deleted_project_urls'

_STATUS_PROJECTION = {f: 1 for f in (*STATUS_FLAG_FIELDS, *TOMBSTONE_MARKER_FIELDS)}
_RECORD_PROJECTION = {**_STATUS_PROJECTION, 'project_name': 1, 'alias_name': 1,
                      'private': 1, 'version_chain_id': 1, 'version_ordinal': 1}


def _chain_documents(collection, doc, projection):
    """Every version of *doc*'s project, *doc* included.

    By the chain pointers where the document has them; otherwise the heads
    whose previous_versions[] name it, plus the versions *doc*'s own array
    names.  Unpointered documents: 0 on prod, 26 on dev (2026-08-27).
    """
    chain_id = doc.get('version_chain_id')
    if chain_id is not None:
        members = list(collection.find({'version_chain_id': chain_id}, projection))
        return members or [doc]
    linkid = str(doc.get('_id'))
    found = {doc['_id']: doc}
    for other in collection.find({'previous_versions.linkid': linkid}, projection):
        found[other['_id']] = other
    for entry in doc.get('previous_versions') or []:
        old = entry.get('linkid') if isinstance(entry, dict) else entry
        try:
            old_doc = collection.find_one({'_id': ObjectId(str(old))}, projection)
        except Exception:
            old_doc = None
        if old_doc is not None:
            found[old_doc['_id']] = old_doc
    return list(found.values())


def chain_is_deleted(collection, doc):
    """Whether *doc* belongs to a project that has been deleted.

    True when its chain holds a SOFT_DELETED version and no LIVE one.  A chain
    with a LIVE head is open whatever else it holds; one with neither (a chain
    of DETACHED documents) is not this function's to close.
    """
    if doc is None:
        return False
    if 'version_chain_id' not in doc:
        # A caller's projection can leave the pointers out, and without them
        # the fallback below would scan previous_versions[] for nothing.
        doc = collection.find_one({'_id': doc['_id']},
                                  {'version_chain_id': 1, 'previous_versions': 1}) or doc
    statuses = {classify(m) for m in _chain_documents(collection, doc, _STATUS_PROJECTION)}
    return SOFT_DELETED in statuses and LIVE not in statuses


def _now():
    return datetime.datetime.utcnow()


def record_project_deleted(collection, record_handle, head, user, by_admin=False,
                           late=False):
    """Write a row for every version of *head*'s project. Returns the row count.

    Upserts: a project deleted, restored and deleted again keeps one row per
    version, with each event appended to its history.  *late* marks rows
    written at permanent-delete time for a project deleted before this record
    existed, whose deletion time and author are then only what the document
    says (delete_date, delete_user).
    """
    try:
        members = _chain_documents(collection, head, _RECORD_PROJECTION)
        when = _now()
        actor = getattr(user, 'email', None) or getattr(user, 'username', None) or str(user)
        for m in members:
            record_handle.update_one(
                {'_id': m['_id']},
                {'$set': {'project_name': m.get('project_name'),
                          'alias_name': m.get('alias_name'),
                          'visibility': normalize_visibility_field(m.get('private', 'private')),
                          'version_chain_id': m.get('version_chain_id'),
                          'version_ordinal': m.get('version_ordinal'),
                          'head_id': head['_id'],
                          'was_head': m['_id'] == head['_id'],
                          'deleted_at': when, 'deleted_by': actor, 'by_admin': bool(by_admin),
                          'restored_at': None, 'recorded_late': bool(late),
                          'delete_date_on_document': head.get('delete_date') if late else None},
                 '$setOnInsert': {'first_deleted_at': when},
                 '$push': {'history': {'event': 'deleted', 'at': when, 'by': actor}}},
                upsert=True)
        return len(members)
    except Exception:
        logging.exception('Could not record the URLs of deleted project %s', head.get('_id'))
        return 0


def ensure_recorded(collection, record_handle, head):
    """Rows for *head*'s project, written now if its deletion predates them.

    Called before a permanent delete, the last moment the documents exist to
    say what their URLs were.
    """
    try:
        if record_handle.count_documents({'head_id': head['_id']}, limit=1):
            return 0
    except Exception:
        logging.exception('Could not read the URL record for %s', head.get('_id'))
        return 0
    return record_project_deleted(collection, record_handle, head,
                                  head.get('delete_user') or 'unknown', late=True)


def _mark_project(record_handle, head_id, event, user, extra):
    when = _now()
    actor = getattr(user, 'email', None) or getattr(user, 'username', None) or str(user)
    try:
        record_handle.update_many(
            {'head_id': ObjectId(str(head_id))},
            {'$set': {**{k: when for k in extra}},
             '$push': {'history': {'event': event, 'at': when, 'by': actor}}})
    except Exception:
        logging.exception('Could not mark deleted project %s as %s', head_id, event)


def record_project_restored(record_handle, head_id, user):
    _mark_project(record_handle, head_id, 'restored', user, ['restored_at'])


def record_project_purged(record_handle, head_id, user):
    _mark_project(record_handle, head_id, 'permanently_deleted', user,
                  ['permanently_deleted_at'])


def find_deleted(record_handle, project_id_or_alias):
    """The record for a URL's project part, if that project is deleted now.

    By id first, then by alias -- an alias is a URL too.  A restored project's
    rows are kept but not returned: its URLs work again.
    """
    try:
        oid = ObjectId(str(project_id_or_alias))
        row = record_handle.find_one({'_id': oid})
    except Exception:
        row = None
    if row is None and project_id_or_alias:
        row = record_handle.find_one({'alias_name': str(project_id_or_alias)},
                                     sort=[('deleted_at', -1)])
    if row is None or row.get('restored_at'):
        return None
    return row


def public_name(row, request=None):
    """The project's name, if this visitor may be told it.

    A private project's name is not shown to someone who could not have seen
    the project; site admins are told it either way.  A hidden_public one was
    readable by anyone holding the link, which is what this visitor has.
    """
    if row is None:
        return None
    visibility = row.get('visibility', 'private')
    if not is_project_private(visibility) or is_project_hidden_public(visibility):
        return row.get('project_name')
    if getattr(getattr(request, 'user', None), 'is_staff', False):
        return row.get('project_name')
    return None
