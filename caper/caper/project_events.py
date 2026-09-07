"""One place that says what happens when a project changes.

Two things are derived from the project documents and have to be kept in step
with them: ``site_statistics``, which counts projects and samples, and the
feature index, which makes them searchable. Before this module each of those
was wired in separately, and ``site_statistics`` was wired at thirteen call
sites across three view modules.

Thirteen call sites is the shape this codebase keeps getting caught by -- a
list, or a predicate, or a hook, maintained in more than one place, where the
copies diverge silently and the divergence is only found by measuring. The
answer that has worked here before is to make it a declaration in one place and
have the users read it: ``project_fields.py`` did it for promotion, and
``visibility.py`` did it for the encoding of ``private``. This does it for
lifecycle events.

So a third consumer of "a project changed" is a change to this file, not a hunt
through views.py for the sites somebody remembered.

## The two are not treated the same way, on purpose

A statistics update is part of the operation: if it fails, the counters are
wrong and nothing else will notice. An index update is an accelerator for a
search that can still be served the old way, and there is a standing check
(``manage.py rebuild_feature_index --check``) that finds and names anything it
missed. So indexing failures are logged and swallowed, and a user's upload is
never failed because a derived search row could not be written. Statistics
calls keep whatever error handling their call site already gave them.

## Why the reindex re-reads the document

``reindex_project`` is handed an id, not a document, and asks the database what
that project now is. That is what makes one function correct for creation,
promotion, demotion, soft delete, restore and chain-emptying alike: after any
of them the right answer is "index it if it is indexable, drop it if it is
not", and the database knows which, where the caller only knows what it was
trying to do. The version-delete path is the case that forced this -- deleting
the head of a chain promotes its predecessor, and indexing the deleted document
while forgetting the promoted one would take the whole project out of search.
"""

import logging

from bson.objectid import ObjectId

from . import feature_index
from .site_stats import (
    add_project_to_site_statistics,
    delete_project_from_site_statistics,
    edit_proj_privacy,
)
from .utils import collection_handle


def reindex_project(project_id):
    """Make the feature index agree with the database about one project.

    Indexes it if it is currently indexable, removes it if it is not. Never
    raises: see the module docstring for why a failure here must not fail the
    operation that triggered it.

    Returns the number of rows written, 0 if the project was removed from the
    index, and None if the attempt failed.
    """
    if not project_id:
        return None
    try:
        object_id = ObjectId(str(project_id))
    except Exception:
        logging.warning('feature index: not a project id: %r', project_id)
        return None

    try:
        query = dict(feature_index.indexable_projects_query())
        query['_id'] = object_id
        project = collection_handle.find_one(query, feature_index.INDEX_SOURCE_PROJECTION)
        if project is None:
            feature_index.unindex_project(object_id)
            return 0
        # find_one with a projection drops _id from neither, but index_project
        # reads it, so make sure it is the ObjectId and not a string.
        project['_id'] = object_id
        return feature_index.index_project(project)
    except Exception:
        # Deliberately broad. The standing drift check is what turns this into
        # a fault someone can see; failing the caller instead would turn a
        # search accelerator into a reason an upload does not complete.
        logging.exception('feature index: reindex failed for %s', project_id)
        return None


def project_changed(project, visibility):
    """A project now exists, or its content changed. Count it and index it."""
    add_project_to_site_statistics(project, visibility)
    reindex_project(project.get('_id'))


def project_removed(project, visibility):
    """A project is no longer live. Uncount it and take it out of the index.

    The reindex is not assumed to be a removal: the same id can come back
    indexable -- an admin repair that sets ``current`` is exactly that -- so it
    asks rather than deletes.
    """
    delete_project_from_site_statistics(project, visibility)
    reindex_project(project.get('_id'))


def project_visibility_changed(project, old_privacy, new_privacy):
    """Only the visibility moved. Rebucket the statistics, reindex the rows.

    Visibility is stored on every index row because it decides who a row is
    returned to, so a visibility change is an index change even though not one
    gene moved.
    """
    edit_proj_privacy(project, old_privacy, new_privacy)
    reindex_project(project.get('_id'))


def project_content_changed(project_id):
    """``runs`` was rewritten without the statistics changing.

    The aggregated upload path is the case this exists for: the document is
    inserted, the statistics are taken from it, and the samples are written
    afterwards by the extraction thread. Nothing about the counts changes at
    that point, but everything the index holds does -- before it, the project
    has no rows at all.
    """
    return reindex_project(project_id)
