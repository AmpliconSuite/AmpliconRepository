"""
Keeping the MongoDB side in step with the Django user record.

Projects live in MongoDB and refer to people by string -- a username or an email
address, whichever was typed into the members box -- while accounts live in the
Django ORM. Nothing joins the two, so when an account changes underneath them
those strings go stale on their own. The two functions here are the repair:
one for an email that changed, one for an account that went away.
"""

import logging

from allauth.account.signals import email_changed
from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete
from django.dispatch import receiver


logger = logging.getLogger(__name__)


def _collections(projects_collection, preferences_collection):
    """Resolve the two collection handles, importing lazily.

    Both functions below take the handles as keyword arguments so tests can pass
    doubles; when they are omitted the real primary-node handles are used, which
    is what the signal receivers do.
    """
    if projects_collection is not None and preferences_collection is not None:
        return projects_collection, preferences_collection

    from .utils import (
        collection_handle_primary,
        db_handle_primary,
        get_collection_handle,
    )

    if projects_collection is None:
        projects_collection = collection_handle_primary
    if preferences_collection is None:
        preferences_collection = get_collection_handle(
            db_handle_primary,
            'user_preferences',
        )
    return projects_collection, preferences_collection


def migrate_email_references(
        old_email, new_email, *, projects_collection=None,
        preferences_collection=None):
    if not old_email or not new_email or old_email == new_email:
        return

    projects_collection, preferences_collection = _collections(
        projects_collection, preferences_collection)

    for field in ('project_members', 'subscribers'):
        projects_collection.update_many(
            {field: old_email},
            {'$addToSet': {field: new_email}},
        )
        projects_collection.update_many(
            {field: old_email},
            {'$pull': {field: old_email}},
        )

    preferences_collection.update_many(
        {'email': old_email},
        {'$set': {'email': new_email}},
    )


@receiver(email_changed)
def migrate_primary_email_references(
        sender, request, user, from_email_address, to_email_address, **kwargs):
    old_email = getattr(from_email_address, 'email', None)
    new_email = getattr(to_email_address, 'email', None)
    try:
        migrate_email_references(old_email, new_email)
    except Exception:
        logger.exception(
            "Failed to migrate MongoDB references from %s to %s for user %s",
            old_email,
            new_email,
            user.pk,
        )


def purge_account_references(
        username, email, *, projects_collection=None,
        preferences_collection=None):
    """Drop a deleted account's identifiers from the active MongoDB records.

    Both identifiers are removed, not just the username: the members box accepts
    either, ``get_user_obj()`` resolves either, and the subscribers list is
    written in email form, so cleaning up only one of them reliably leaves the
    other behind.

    What is deliberately *not* touched:

    * Documents with ``current`` unset or false. Those are the superseded
      versions of a project and the audit log's view of who submitted what.
      They are the repository's provenance record and are supposed to say what
      was true at the time, so an account going away is not a reason to rewrite
      them. Only the live project documents are edited.
    * API tokens, which are a Django model with a CASCADE relation to the user
      and are already gone by the time this runs.

    Returns a dict of counts, which the caller logs. Safe to call more than once
    -- every operation is a removal that matches nothing the second time.
    """
    identifiers = [value for value in (username, email) if value]
    if not identifiers:
        return {'projects_updated': 0, 'preferences_deleted': 0}

    projects_collection, preferences_collection = _collections(
        projects_collection, preferences_collection)

    projects_updated = 0
    for field in ('project_members', 'subscribers'):
        result = projects_collection.update_many(
            {'current': True, field: {'$in': identifiers}},
            {'$pull': {field: {'$in': identifiers}}},
        )
        projects_updated += getattr(result, 'modified_count', 0) or 0

    # Preferences are keyed by email; a username never appears in this
    # collection, so only the email is worth querying on.
    preferences_deleted = 0
    if email:
        result = preferences_collection.delete_many({'email': email})
        preferences_deleted = getattr(result, 'deleted_count', 0) or 0

    return {
        'projects_updated': projects_updated,
        'preferences_deleted': preferences_deleted,
    }


@receiver(post_delete, sender=get_user_model())
def purge_references_for_deleted_account(sender, instance, **kwargs):
    """Run the purge for every deletion path, not just the admin page.

    Accounts are also deleted through Django's own /admin/ and from the shell,
    and those paths would otherwise leave the Mongo-side references behind.
    Hanging it on post_delete covers all of them from one place.

    Failures are logged and swallowed: the account is already gone by the time
    this runs, and a MongoDB hiccup should not turn a completed deletion into a
    500 for the administrator who requested it.
    """
    try:
        counts = purge_account_references(
            getattr(instance, 'username', None),
            getattr(instance, 'email', None),
        )
        logger.info(
            "Purged deleted account's references: %d project list(s) updated, "
            "%d preference record(s) deleted",
            counts['projects_updated'],
            counts['preferences_deleted'],
        )
    except Exception:
        logger.exception(
            "Failed to purge MongoDB references for deleted user %s",
            getattr(instance, 'pk', None),
        )
