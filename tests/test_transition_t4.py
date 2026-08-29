"""T4 -- a project is soft-deleted, then restored -- against a real MongoDB.

The transition the write-path work never exercised end to end, and the one
whose two half-writes turned out to be reading a replica: an edit calls
project_update() then project_delete(), and each recomputes the stored status
from the document it reads.  T4 is where that pair lives, which makes it worth
running rather than reasoning about.

What T4 asserts, in the spec's words: SOFT_DELETED applies to the head and
therefore to the whole lineage.  It is a **visibility state, not a lineage
state** -- the head keeps the head, the pointers do not move, the ordinals do
not change, a SUPERSEDED member of a soft-deleted chain stays SUPERSEDED, and
both payloads are retained.  Restoring puts it back exactly.

Everything here goes through the real views against the real collection, so a
half-write that lands on the wrong document fails here rather than in front of
a user.

**One thing this file cannot catch on its own.**  Run locally it talks to a
single-node MongoDB, where a read issued after a write always sees the write.
The stale-replica read that produced a wrong status on dev needs a secondary to
lag behind, so the assertions below about the *stored* status pass here whether
or not that bug is present.  They still belong here -- this is the shape that
exercises them, and the same file run against DocumentDB does catch it.  What
covers the staleness deterministically is the OneWriteBehind collection in
tests/test_lineage_writes.py, which fakes a replica that is exactly one write
behind because that is what caper-dev measured: 34 of 40, 2026-08-28.
"""

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _build_edit_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
)


def _flags(collection, project_id):
    """Status, flags and lineage of one document, straight from the collection."""
    from caper.project_status import classify
    doc = collection.find_one({'_id': ObjectId(project_id)})
    assert doc is not None, f'{project_id} vanished'
    return {
        'stored': doc.get('status'),
        'classified': classify(doc),
        'delete': doc.get('delete'),
        'current': doc.get('current'),
        'is_latest': doc.get('is_latest'),
        'ordinal': doc.get('version_ordinal'),
        'chain': doc.get('version_chain_id'),
        'previous': doc.get('previous_version_id'),
        'next': doc.get('next_version_id'),
        'has_payload': bool(doc.get('tarfile')),
    }


def _lineage_of(state):
    """The part of a document T4 must not touch."""
    return {key: state[key] for key in
            ('ordinal', 'chain', 'previous', 'next', 'has_payload')}


def _restore(request_factory, user, project_id, project_name):
    """Drive the admin page's un-delete, which is T4's second half."""
    from caper.views_admin import admin_delete_project
    request = request_factory.post('/admin-delete-project/', data={
        'project_name': project_name,
        'project_id': str(project_id),
        'action': 'un-delete',
        'delete': False,
    })
    request.user = user
    return admin_delete_project(request)


@pytest.mark.slow
@pytest.mark.integration
def test_soft_delete_and_restore_leave_the_chain_exactly_as_they_found_it(
        request_factory, test_user, admin_user, mongo_collection):
    """T4 over a two-version chain, which is the case with something to break.

    A one-version project cannot show the interesting property -- that the
    SUPERSEDED member is untouched -- because it has no superseded member. The
    two half-writes are also indistinguishable on a chain of one, since the
    document they read and the document they write are the same either way.
    """
    from caper.views import create_project, edit_project_page, project_delete

    created = []
    try:
        # --- two versions, so there is a SUPERSEDED member to protect ---
        request, handles = _build_create_request(
            request_factory, test_user, 'T4_SoftDelete',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        first = _project_id_from_redirect(response)
        created.append(first)
        doc = _poll_until_finished(mongo_collection, first)
        assert doc and not doc.get('aggregation_failed'), \
            f"create failed: {doc.get('error_message') if doc else 'timed out'}"

        edit_request, edit_handles = _build_edit_request(
            request_factory, test_user, first, project_name='T4_SoftDelete',
            xlsx_path=DATASET_SMALL_XLSX)
        try:
            edit_response = edit_project_page(edit_request, project_name=first)
        finally:
            for handle in edit_handles:
                handle.close()
        second = _project_id_from_redirect(edit_response)
        assert second and second != first, 'the edit did not make a new version'
        created.append(second)
        doc = _poll_until_finished(mongo_collection, second)
        assert doc and not doc.get('aggregation_failed'), \
            f"re-aggregation failed: {doc.get('error_message') if doc else 'timed out'}"

        before_head = _flags(mongo_collection, second)
        before_prior = _flags(mongo_collection, first)

        assert before_head['classified'] == 'LIVE'
        assert before_head['stored'] == 'LIVE', \
            'the stored status must agree with the flags before we start'
        assert before_prior['classified'] == 'SUPERSEDED'
        assert before_prior['stored'] == 'SUPERSEDED', (
            'the superseding half-writes wrote a status that disagrees with '
            'the flags -- this is the stale-replica read, and it is what T4 '
            'was written to catch')
        assert before_head['is_latest'] is True
        assert before_prior['is_latest'] is False

        # --- soft delete the head ---
        delete_request = request_factory.post(f'/project/{second}/delete')
        delete_request.user = test_user
        project_delete(delete_request, project_name=second)

        after_head = _flags(mongo_collection, second)
        after_prior = _flags(mongo_collection, first)

        assert after_head['classified'] == 'SOFT_DELETED'
        assert after_head['stored'] == 'SOFT_DELETED', \
            'the stored status has to follow the flags through a half-write'
        assert after_head['is_latest'] is True, \
            'a soft delete does not move the head -- it is visibility, not lineage'
        assert _lineage_of(after_head) == _lineage_of(before_head), \
            'a soft delete changed something structural'
        assert after_head['has_payload'], 'both payloads are retained through T4'

        assert after_prior['classified'] == 'SUPERSEDED', \
            'a SUPERSEDED member of a soft-deleted chain stays SUPERSEDED'
        assert after_prior == before_prior, \
            'the soft delete reached a document that was not its target'

        # --- restore ---
        _restore(request_factory, admin_user, second, 'T4_SoftDelete')

        restored_head = _flags(mongo_collection, second)
        restored_prior = _flags(mongo_collection, first)

        assert restored_head['classified'] == 'LIVE'
        assert restored_head['stored'] == 'LIVE', \
            'restore recomputes the status too, and from the same stale read'
        assert restored_head == before_head, \
            'restore did not put the head back exactly as it was'
        assert restored_prior == before_prior, \
            'restore reached a document that was not its target'

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_a_soft_deleted_chain_still_validates(
        request_factory, test_user, admin_user, mongo_collection):
    """I3 and I16 hold across a soft delete, which is the point of T4's shape.

    The head keeps is_latest while soft-deleted, so the chain still has exactly
    one head. A restore that moved the flag, or a soft delete that cleared it,
    would leave the chain headless -- and the failure would surface as a
    history page with no current version rather than as an error.
    """
    from caper.views import create_project, project_delete
    from caper import lineage

    created = []
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'T4_HeadKeepsHead',
            tar_path=DATASET_SMALL_TAR)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        project_id = _project_id_from_redirect(response)
        created.append(project_id)
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed')

        delete_request = request_factory.post(f'/project/{project_id}/delete')
        delete_request.user = test_user
        project_delete(delete_request, project_name=project_id)

        doc = mongo_collection.find_one({'_id': ObjectId(project_id)})
        members = lineage.chain_members(mongo_collection, doc,
                                        lineage.POINTER_PROJECTION)
        assert members is not None, 'the soft-deleted head lost its pointers'
        heads = [m for m in members if m.get('is_latest') is True]
        assert len(heads) == 1, \
            f'a soft delete left {len(heads)} heads; I3 and I16 want exactly one'
        assert str(heads[0]['_id']) == project_id

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)
