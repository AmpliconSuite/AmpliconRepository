"""Which versions of a project a publication actually used.

``publication_link`` belongs to the project and says a paper exists. It cannot
say which version the paper used: measured read-only on prod 2026-11-04, 36
projects carry a link, across 99 versions. A paper's results can only be checked
against the version it used, so that version needs to be known before anything
removes old versions.

**The tag sits on the version** (``publication_tag``). It is version-level: a new
version does not inherit it, and promotion does not carry it. It is set by a site
admin, from what the paper itself says, and ``evidence`` records how we know: a
version id in the paper's data availability statement, or something weaker.

**Deleting a tagged version takes a typed project name.** A person deleting one
project or version is asked for it. A path with nobody to ask (the admin's bulk
delete, an account deletion, the orphan cleanup script) does not delete the
project and reports why; an admin can then delete it on its own.

``admin_permanent_delete_project`` checks as well as its callers, because it is
the one place every permanent delete passes through.

A tombstone keeps its tag as a record of what was deleted, and is no longer
protected: its files are already gone.
"""

import contextlib
import datetime
import threading

from . import lineage
from .project_status import is_tombstone

FIELD = 'publication_tag'

MAX_PUBLICATION_LENGTH = 1000
MAX_EVIDENCE_LENGTH = 2000


class PublicationVersionProtected(Exception):
    """A delete reached a version a publication used, and nobody confirmed it."""


def is_protected(document):
    """True when deleting *document* needs the typed confirmation."""
    return bool(document) and bool(document.get(FIELD)) and not is_tombstone(document)


def build_tag(publication, evidence, user):
    """The value of the field, or ValueError saying what is missing or too long."""
    publication = (publication or '').strip()
    evidence = (evidence or '').strip()
    if not publication:
        raise ValueError('Say which publication used this version.')
    if not evidence:
        raise ValueError('Say how we know the publication used this version.')
    if len(publication) > MAX_PUBLICATION_LENGTH or len(evidence) > MAX_EVIDENCE_LENGTH:
        raise ValueError('The publication or the evidence is too long.')
    return {
        'publication': publication,
        'evidence': evidence,
        'tagged_by': getattr(user, 'username', None) or str(user),
        'tagged_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }


def link_to_offer(collection, project):
    """The project's publication link, to offer when tagging one of its versions.

    Read from the head, since the link belongs to the project and an older
    version need not carry it. Measured read-only on prod 2026-11-04: all 36
    chains with a link hold one distinct link and the head has it, while 14 of
    their 59 superseded versions have none.
    """
    head = lineage.latest_version(collection, project, {'publication_link': 1, 'is_latest': 1,
                                                        'version_ordinal': 1})
    return ((head or project).get('publication_link') or '').strip()


def name_confirmed(document, typed):
    """True when *typed* is the project's name. Blank never is."""
    name = (document or {}).get('project_name')
    return bool(name) and (typed or '').strip() == name


_confirmed = threading.local()


@contextlib.contextmanager
def delete_confirmed():
    """Permanent deletes inside this block have had the name typed for them."""
    previous = getattr(_confirmed, 'value', False)
    _confirmed.value = True
    try:
        yield
    finally:
        _confirmed.value = previous


def refuse_unless_confirmed(document):
    """Raise PublicationVersionProtected if *document* may not go unconfirmed."""
    if is_protected(document) and not getattr(_confirmed, 'value', False):
        raise PublicationVersionProtected(
            f"{document.get('project_name')} ({document.get('_id')}) is a version used in "
            f"{document[FIELD].get('publication')}")
