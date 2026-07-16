import logging

from allauth.account.signals import email_changed
from django.dispatch import receiver


logger = logging.getLogger(__name__)


def migrate_email_references(
        old_email, new_email, *, projects_collection=None,
        preferences_collection=None):
    if not old_email or not new_email or old_email == new_email:
        return

    if projects_collection is None or preferences_collection is None:
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
