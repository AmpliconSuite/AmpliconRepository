"""
What happens to a person's projects when their account goes away.

Deleting an account is not just deleting a row. Projects live in MongoDB and
name their members by string, so an account closing leaves every project that
named it needing a decision:

  * shared -- somebody else is still a member, so the project carries on without
    this one. Nothing to decide.
  * solo and private -- nobody else can see it and nobody is left to look after
    it. It is deleted, files and all. Keeping private data around after the only
    person who could reach it has asked to leave is the thing we are trying to
    avoid.
  * solo and publicly reachable -- deleting it would break links other people
    may already have published, so it is handed to a caretaker account instead.
    This covers ``hidden_public`` as well as ``public``: unlisted is not the
    same as unreachable, and a link in a manuscript keeps working either way.

This module holds that decision in one place so every route into it agrees --
the admin page, the account-holder's own "delete my account" button, Django's
built-in ``/admin/``, and ``manage.py shell``. The last two only ever call
``User.delete()``, so the work hangs off ``post_delete`` (see
``account_signals``) rather than off any particular view.

``plan_account_deletion`` answers "what would happen", which is what the two
confirmation screens show; ``dispose_of_projects`` does it. They walk the same
projects and apply the same rule, so the screen cannot promise one thing and the
deletion do another.
"""

import logging
from collections import namedtuple

from django.conf import settings
from django.contrib.auth import get_user_model


logger = logging.getLogger(__name__)


# What is going to happen to one project.
DELETE = 'delete'
REASSIGN = 'reassign'
RELEASE = 'release'

PlannedAction = namedtuple('PlannedAction', 'project action visibility')


def account_identifiers(username, email):
    """Every string this account can appear as in a project's member list.

    The members box takes a username or an email address interchangeably, so one
    account is often recorded both ways on different projects. Matching on the
    username alone misses the rest -- which is how a project that is really solo
    looks shared, escapes the decision below, and then has its last member
    stripped out from under it.
    """
    identifiers = []
    for value in (username, email):
        if value and value not in identifiers:
            identifiers.append(value)
    return identifiers


def _projects_collection(projects_collection=None):
    """Resolve the projects handle, importing lazily so tests can pass a double."""
    if projects_collection is not None:
        return projects_collection
    from .utils import collection_handle
    return collection_handle


def caretaker_username():
    """Who inherits a public project whose only member closed their account.

    Deliberately a lookup rather than a constant: the named caretaker may not
    exist on a given deployment (a fresh dev database, say), and reassigning to
    an account that is not there would leave the project as ownerless as doing
    nothing at all.
    """
    User = get_user_model()

    preferred = getattr(settings, 'ORPHANED_PROJECT_OWNER', None)
    if preferred and User.objects.filter(username=preferred).exists():
        return preferred

    staff = User.objects.filter(is_staff=True).order_by('pk').first()
    if staff is not None:
        return staff.username

    logger.warning(
        "No caretaker account available to inherit orphaned public projects; "
        "falling back to 'admin', which may not exist.")
    return 'admin'


def plan_account_deletion(username, email, *, projects_collection=None):
    """What deleting this account would do to each of its live projects.

    Returns a list of PlannedAction. Read-only -- safe to call from a GET, which
    is the point: both confirmation screens render this.
    """
    from .utils import normalize_visibility_field

    identifiers = account_identifiers(username, email)
    if not identifiers:
        return []

    collection = _projects_collection(projects_collection)
    projects = list(collection.find({
        'current': True,
        'project_members': {'$in': identifiers},
    }))

    plan = []
    for project in projects:
        others = [member for member in project.get('project_members', [])
                  if member not in identifiers]
        visibility = normalize_visibility_field(project.get('private', 'private'))

        if others:
            action = RELEASE
        elif visibility == 'private':
            action = DELETE
        else:
            # Public or hidden_public. Both are reachable by anyone holding the
            # link, so both are reassigned rather than destroyed. This is
            # deliberately narrower than is_project_private(), which groups
            # hidden_public with private for access-control purposes -- a
            # different question from whether destroying it would break a link
            # someone else has already published.
            action = REASSIGN

        plan.append(PlannedAction(project, action, visibility))

    return plan


def dispose_of_projects(username, email, *, projects_collection=None,
                        delete_project=None):
    """Carry out the plan for the solo projects.

    Shared projects are left alone here: dropping the member from them is a
    ``$pull`` that ``purge_account_references`` already does, along with the
    subscriber lists and the notification preferences. Doing it twice would be
    harmless but this way each removal has one owner.

    ``delete_project`` is injectable because the real one reaches into GridFS,
    the local filesystem and S3, which a unit test has no business doing.

    Returns a report the callers turn into a message for whoever asked.
    """
    collection = _projects_collection(projects_collection)

    if delete_project is None:
        # Imported here, not at module scope: views_admin pulls in a large slice
        # of the application, and this module is imported from AppConfig.ready().
        from .views_admin import admin_permanent_delete_project
        delete_project = admin_permanent_delete_project

    report = {'deleted': [], 'reassigned': [], 'released': [], 'errors': []}
    plan = plan_account_deletion(username, email,
                                 projects_collection=collection)
    caretaker = None

    for project, action, visibility in plan:
        project_name = project.get('project_name', '<unnamed>')
        project_id = project.get('_id')

        if action == RELEASE:
            report['released'].append(project_name)
            continue

        try:
            if action == DELETE:
                delete_project(project_id, project, project_name)
                report['deleted'].append(project_name)
            else:
                if caretaker is None:
                    caretaker = caretaker_username()
                collection.update_one(
                    {'_id': project_id},
                    {'$set': {'project_members': [caretaker]}},
                )
                report['reassigned'].append((project_name, caretaker))
        except Exception:
            logger.exception(
                "Failed to %s project %s while deleting an account",
                action, project_name)
            report['errors'].append(project_name)

    if report['deleted'] or report['reassigned'] or report['errors']:
        logger.info(
            "Account deletion disposed of projects: %d deleted, %d reassigned, "
            "%d left to other members, %d failed",
            len(report['deleted']), len(report['reassigned']),
            len(report['released']), len(report['errors']))

    return report


def summarize(report):
    """One human-readable line per thing that happened, for the admin page."""
    parts = []
    for name in report.get('deleted', []):
        parts.append(f"Project {name} was private with no other members and was deleted.")
    for name, caretaker in report.get('reassigned', []):
        parts.append(f"Project {name} was public with no other members and was reassigned to {caretaker}.")
    for name in report.get('released', []):
        parts.append(f"Removed from project {name}.")
    for name in report.get('errors', []):
        parts.append(f"Project {name} could not be cleaned up -- see the application log.")
    return ' '.join(parts)
