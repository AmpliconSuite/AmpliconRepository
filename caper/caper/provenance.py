"""Who did what to a project, recorded before it happens.

The site already logged creation and edits. It logged nothing about deletion:
not the soft delete a user performs, not the version delete that turns a
version into a tombstone, not the promotion that follows, not the permanent
delete an admin performs. After a deletion there was no record of who did it,
what the project looked like beforehand, or which of the several deletion paths
ran. The August 2026 cleanup could not be reconstructed afterwards for exactly
that reason.

**Events are written before the mutation, not after.** A record written
afterwards is missing precisely when it matters most -- the process that dies
half way through a purge is the one whose trace you need. So ``record()``
returns an id, the mutation runs, and ``confirm()`` marks the event completed.
An event with ``completed`` unset is not a bookkeeping wart: it is the signature
of an operation that started and did not finish, and it is the only way to tell
that apart from an operation that never started.

**Nothing here raises.** An audit write that fails must not turn a working
deletion into a 500 -- the deletion is the user's intent and the log is our
record of it. Failures are logged and swallowed, which means the log is
evidence and never authority. Do not build a check that assumes an event exists
for every mutation; the absence of an event is weaker evidence than the
presence of one.

**No backfill.** Deletions before this shipped are not recoverable and are not
invented. A fabricated event is worse than a gap, because a gap is honest.
"""

import datetime
import logging

# Deletion and lifecycle events. The three creation/edit spellings already in
# the collection live in views.py and are not moved here -- renaming a value
# that 121 stored documents already carry buys nothing.
DELETE_PROJECT = 'delete_project'
"""A user soft-deleted a project. Reversible from the admin page."""

RESTORE_PROJECT = 'restore_project'
"""A soft-deleted project was put back."""

DELETE_VERSION = 'delete_version'
"""One version became a tombstone; its payload was purged."""

PROMOTE_VERSION = 'promote_version'
"""A surviving version took the deleted head's place as the live document."""

PERMANENT_DELETE = 'permanent_delete'
"""An admin removed a project's documents and payload for good."""

PURGE_PAYLOAD = 'purge_payload'
"""GridFS files were removed while the document stayed."""

DELETION_EVENTS = (DELETE_PROJECT, RESTORE_PROJECT, DELETE_VERSION,
                   PROMOTE_VERSION, PERMANENT_DELETE, PURGE_PAYLOAD)


def _actor(user):
    """A person as a stable string, without assuming a Django User."""
    if user is None:
        return 'unknown'
    email = getattr(user, 'email', None)
    if email:
        return email
    username = getattr(user, 'username', None)
    return username or str(user)


def snapshot(project):
    """The part of a document worth keeping before it is changed.

    Deliberately small and deliberately not the whole document: `runs` and
    `sample_data` are most of a project's bytes, and copying them into the
    audit collection on every deletion would make the log the largest thing in
    the database. What is kept is what a person reconstructing the event needs
    -- which project, which version, what state it was in, and whether it still
    held a payload.
    """
    if not project:
        return {}
    # Imported here rather than at module scope: views and views_admin import
    # this module, and utils reaches those through its own chain.
    from .utils import normalize_visibility_field

    return {
        'project_id': str(project.get('_id')),
        'project_name': project.get('project_name'),
        'version_chain_id': (str(project['version_chain_id'])
                             if project.get('version_chain_id') is not None
                             else None),
        'version_ordinal': project.get('version_ordinal'),
        'status': project.get('status'),
        'is_latest': project.get('is_latest'),
        # The raw stored value, on purpose. Normalising it here would destroy
        # the evidence: both encodings are live, and a record that says
        # 'private' cannot later tell you whether the document held the string
        # or the boolean. The interpreted form sits beside it for readers.
        'private': project.get('private'),
        'visibility': normalize_visibility_field(
            project.get('private', 'private')),
        'sample_count': project.get('sample_count'),
        'had_tarfile': bool(project.get('tarfile')),
        'date': project.get('date'),
    }


def record(collection, event_type, user, project, intended=None, **details):
    """Write the event that is *about* to happen. Returns its id, or None.

    *intended* describes the state the mutation is trying to reach, so that an
    unconfirmed event still says what was being attempted.
    """
    try:
        entry = {
            'timestamp': datetime.datetime.utcnow(),
            'user_email': _actor(user),
            'event_type': event_type,
            'completed': False,
            'before': snapshot(project),
            'intended': intended or {},
        }
        # The three fields the existing reader indexes on, mirrored at the top
        # level so that one query serves old and new events alike.
        entry['project_uuid'] = entry['before'].get('project_id')
        entry['project_name'] = entry['before'].get('project_name')
        entry['version_chain_id'] = entry['before'].get('version_chain_id')
        entry.update(details)
        return collection.insert_one(entry).inserted_id
    except Exception:
        logging.exception('Could not record %s provenance event', event_type)
        return None


def confirm(collection, event_id, **outcome):
    """Mark a recorded event as having completed, with what actually happened."""
    if event_id is None:
        return
    try:
        update = {'completed': True,
                  'completed_at': datetime.datetime.utcnow()}
        update.update(outcome)
        collection.update_one({'_id': event_id}, {'$set': update})
    except Exception:
        logging.exception('Could not confirm provenance event %s', event_id)


def history_for(collection, project_ids, limit=500):
    """Every event naming any of *project_ids*, newest first.

    Takes ids rather than a chain id because the collection's older entries
    predate ``version_chain_id`` -- 121 documents on prod on 2026-08-29 carry
    only ``project_uuid``. Callers pass the whole chain's ids, which is what
    ``get_project_version_chain()`` already returns for the existing view.
    """
    ids = [str(pid) for pid in project_ids if pid is not None]
    if not ids:
        return []
    try:
        return list(collection.find({'project_uuid': {'$in': ids}})
                    .sort('timestamp', -1).limit(limit))
    except Exception:
        logging.exception('Could not read provenance for %s', ids[:3])
        return []
