"""View and download counters: one per version, summed across the chain to read.

Each version keeps its own counts and starts at zero. Nothing is carried
forward, and that is what makes the project's number a sum rather than a
reconciliation: a download is counted against the version that served it, a
deleted version keeps its share on its tombstone, and the project's total is
``download_totals.chain_totals()`` over the chain. Deleting a version does not
change the total; promoting one does not need to copy anything.

Two records are written for a download and they are not duplicates. The
per-date dict (``project_downloads``, written in views.py) says which version
served it and on what day, which is what the admin statistics read. The int
here is that version's running total.

**Every write here is a ``$inc`` and touches exactly one field.** There used to
be an initialiser, and it read:

    if ('views' not in project) or ('downloads' not in project):
        update_one(query, {'$set': {'views': 1, 'downloads': 0}})

A project missing only ``views`` therefore had its download count set to zero
by the next page view. Measured on prod 2026-08-30, that had happened to seven
public projects -- CCLE, PCAWG and TCGA among them -- which read ``downloads:
0`` while their per-date records still held every one of the 12 to 27 downloads
that had been counted. Nothing noticed, because nothing compared the two
records. ``$inc`` creates an absent field at 1, so no initialiser is needed and
no counter can be reset as a side effect of another one.
"""

from .utils import *


def session_visit(request, project):
    """
    If the user session hasn't viewed that project page yet, then record it.
    If it has visited, don't increment.

    """
    ## if the user session hasn't visited the project page yet, increment.
    proj_id = project['_id']
    if (request.session.get(f'visited_{proj_id}') is None) or (request.session.get(f'visited_{proj_id}') == False):
        ## increment:

        request.session[f'visited_{proj_id}'] = True

        return get_increment_view_and_download_statistics(project)
    else:
        ## only get current stats
        res = collection_handle.find_one({'_id': proj_id}, {'views': 1}) or {}
        return res.get('views', 0)


def get_increment_view_and_download_statistics(project):
    """Add one to this version's view count and return the new value."""
    updated = collection_handle.find_one_and_update(
        {'_id': project['_id']}, {'$inc': {'views': 1}},
        projection={'views': 1}, return_document=True)
    return (updated or {}).get('views', 1)


def increment_download(project):
    """Add one to this version's download count.

    Per version and starting at zero, like every counter here; the project's
    number is the sum across the chain at read time.
    """
    collection_handle.update_one({'_id': project['_id']},
                                 {'$inc': {'downloads': 1}})
