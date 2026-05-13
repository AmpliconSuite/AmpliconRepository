"""
Integration tests for project lifecycle: privacy, featured flag, membership.

Isolation rule: every test that mutates project state creates its own
short-lived project and restores / cleans up in a try/finally block.
loaded_datasets projects are never modified here.
"""

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
    POLL_TIMEOUT,
)


class _AnonUser:
    """Unauthenticated placeholder for access-control tests."""
    username = ''
    email    = ''
    is_authenticated = False
    is_staff         = False
    is_active        = False
    is_superuser     = False

    def __str__(self):
        return 'anon'


# ---------------------------------------------------------------------------
# State-read tests (use loaded_datasets, no mutations)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_project_starts_private(loaded_datasets, mongo_collection):
    """A freshly created project must have a private/restricted visibility value."""
    from caper.utils import normalize_visibility_field, is_project_private
    doc = mongo_collection.find_one(
        {'_id': ObjectId(loaded_datasets['project_small'])})
    assert doc is not None, "project_small not found in MongoDB"
    visibility = normalize_visibility_field(doc.get('private'))
    assert is_project_private(visibility), \
        f"Expected private project, got visibility={visibility!r}"


# ---------------------------------------------------------------------------
# Mutation tests — each creates its own dedicated project
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.integration
def test_visibility_cycle(request_factory, test_user, mongo_collection):
    """
    Full visibility cycle on a dedicated project:
      private  → anonymous user is redirected to login
      public   → anonymous user can view (200)
      featured → project name appears on homepage
      private  → anonymous user is redirected again
    """
    from caper.views import create_project, project_page, index

    req, handles = _build_create_request(
        request_factory, test_user, 'LifecycleTest_VisCycle',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id, "Could not parse project_id from create redirect"

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message') if doc else 'timeout after ' + str(POLL_TIMEOUT) + 's'}"

        anon = _AnonUser()

        # --- private: anonymous should be redirected to login ---
        req_priv = request_factory.get(f'/project/{project_id}')
        req_priv.user = anon
        req_priv.session = {}
        resp_priv = project_page(req_priv, project_name=project_id)
        assert resp_priv.status_code in (301, 302), \
            "Private project must redirect unauthenticated user"
        assert 'login' in resp_priv.get('Location', '').lower(), \
            "Redirect location should contain 'login'"

        # --- make public: anonymous should now be able to view ---
        mongo_collection.update_one(
            {'_id': ObjectId(project_id)},
            {'$set': {'private': 'public'}})
        req_pub = request_factory.get(f'/project/{project_id}')
        req_pub.user = anon
        req_pub.session = {}
        resp_pub = project_page(req_pub, project_name=project_id)
        assert resp_pub.status_code == 200, \
            "Public project must be accessible to unauthenticated users"

        # --- make featured: project name should appear on homepage ---
        mongo_collection.update_one(
            {'_id': ObjectId(project_id)},
            {'$set': {'featured': True}})
        req_idx = request_factory.get('/')
        req_idx.user = anon
        req_idx.session = {}
        resp_idx = index(req_idx)
        assert resp_idx.status_code == 200
        assert b'LifecycleTest_VisCycle' in resp_idx.content, \
            "Featured project name must appear in homepage response"

        # --- back to private: anonymous should be redirected again ---
        mongo_collection.update_one(
            {'_id': ObjectId(project_id)},
            {'$set': {'private': 'private', 'featured': False}})
        req_repriv = request_factory.get(f'/project/{project_id}')
        req_repriv.user = anon
        req_repriv.session = {}
        resp_repriv = project_page(req_repriv, project_name=project_id)
        assert resp_repriv.status_code in (301, 302), \
            "Re-privated project must redirect unauthenticated user"

    finally:
        _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_add_and_remove_project_member(
        request_factory, test_user, non_member_user, mongo_collection):
    """
    Add non_member_user to project_members on a private project → gains access.
    Remove them → access denied again.
    """
    from caper.views import create_project, project_page

    req, handles = _build_create_request(
        request_factory, test_user, 'LifecycleTest_Members',
        tar_path=DATASET_SMALL_TAR)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id, "Could not parse project_id"

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"Aggregation failed: {doc.get('error_message') if doc else 'timeout'}"

        # non_member_user is not yet a member — should be redirected
        req_denied = request_factory.get(f'/project/{project_id}')
        req_denied.user = non_member_user
        req_denied.session = {}
        resp_denied = project_page(req_denied, project_name=project_id)
        assert resp_denied.status_code in (301, 302), \
            "Non-member should be redirected from private project"

        # Add non_member_user
        mongo_collection.update_one(
            {'_id': ObjectId(project_id)},
            {'$addToSet': {'project_members': non_member_user.username}})

        req_member = request_factory.get(f'/project/{project_id}')
        req_member.user = non_member_user
        req_member.session = {}
        resp_member = project_page(req_member, project_name=project_id)
        assert resp_member.status_code == 200, \
            "Added member must be able to view private project"

        # Remove non_member_user
        mongo_collection.update_one(
            {'_id': ObjectId(project_id)},
            {'$pull': {'project_members': non_member_user.username}})

        req_removed = request_factory.get(f'/project/{project_id}')
        req_removed.user = non_member_user
        req_removed.session = {}
        resp_removed = project_page(req_removed, project_name=project_id)
        assert resp_removed.status_code in (301, 302), \
            "Removed member must be denied access to private project"

    finally:
        _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_replace_project_file_version_history(
        request_factory, test_user, mongo_collection):
    """
    Create a project, then reaggregate it.
    Assert the new document records a previous_versions entry.
    xfail if the aggregator does not support reaggregation.
    """
    from caper.views import create_project, edit_project_page

    # --- create initial version ---
    req, handles = _build_create_request(
        request_factory, test_user, 'LifecycleTest_Version',
        tar_path=DATASET_SMALL_TAR)
    try:
        resp = create_project(req)
    finally:
        for h in handles:
            h.close()

    project_id = _project_id_from_redirect(resp)
    assert project_id, "Could not parse project_id"
    created_ids = [project_id]

    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"[Create] Aggregation failed: {doc.get('error_message') if doc else 'timeout'}"

        # --- reaggregate (no new file, just trigger re-aggregation) ---
        from conftest import _build_edit_request
        edit_req, edit_handles = _build_edit_request(
            request_factory, test_user, project_id,
            project_name='LifecycleTest_Version',
            xlsx_path=DATASET_SMALL_XLSX,
            remap=False)
        # Override project_mode to 'reaggregate'
        mutable = edit_req.POST.copy()
        mutable['project_mode'] = 'reaggregate'
        edit_req.POST = mutable
        try:
            edit_resp = edit_project_page(edit_req, project_name=project_id)
        finally:
            for h in edit_handles:
                h.close()

        assert edit_resp.status_code in (301, 302), \
            f"[Edit] Expected redirect, got {edit_resp.status_code}"

        new_id = _project_id_from_redirect(edit_resp)
        poll_id = new_id if (new_id and new_id != project_id) else project_id
        if new_id and new_id != project_id:
            created_ids.append(new_id)

        edited_doc = _poll_until_finished(mongo_collection, poll_id)
        assert edited_doc is not None, \
            f"[Edit] Timed out waiting for reaggregation ({POLL_TIMEOUT}s)"

        if edited_doc.get('aggregation_failed'):
            pytest.xfail(
                f"Reaggregation failed (may require a newer aggregator). "
                f"Error: {edited_doc.get('error_message', 'none')}")

        assert edited_doc.get('FINISHED?'), "[Edit] FINISHED? not set after reaggregation"

        # The new version should reference the old one in previous_versions
        prev = edited_doc.get('previous_versions', [])
        assert len(prev) > 0, \
            "Reaggregated project should have a previous_versions entry"

    finally:
        for pid in created_ids:
            _cleanup_project(mongo_collection, pid)
