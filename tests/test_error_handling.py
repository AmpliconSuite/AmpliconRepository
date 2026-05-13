"""
Integration tests for error and edge-case handling.

When view functions are called directly (not through the URL router),
Django's Http404 propagates as a Python exception rather than being
converted to an HTTP 404 response.  Tests use pytest.raises(Http404)
for cases where the view raises it.
"""

import pytest
from bson.objectid import ObjectId

from conftest import POLL_TIMEOUT, _cleanup_project, _project_id_from_redirect

# A 24-character hex string that is a valid ObjectId format but will never
# match a real project document.
_GHOST_ID = '000000000000000000000000'


# ---------------------------------------------------------------------------
# create_project error cases
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_create_project_without_file(request_factory, test_user, mongo_collection):
    """
    POST /create-project/ with no tar file must not return a 500.

    The view creates a placeholder document immediately and runs the aggregator
    in a background thread.  Without a file the aggregator fails, but the view
    itself must still respond gracefully (any status except 500).  Any placeholder
    document that was created is cleaned up after the assertion.
    """
    from caper.views import create_project

    req = request_factory.post('/create-project/', {
        'project_name':        'ErrorTest_NoFile',
        'description':         'Automated pytest error test',
        'private':             'private',
        'publication_link':    '',
        'project_members':     '',
        'alias':               '',
        'remap_sample_names':  'false',
        'accept_license':      'on',
    })
    req.user = test_user
    resp = create_project(req)

    assert resp.status_code != 500, \
        "create_project must not return a 500 when no file is uploaded"

    # Clean up the placeholder document the view always creates
    new_id = _project_id_from_redirect(resp)
    if new_id:
        _cleanup_project(mongo_collection, new_id)


# ---------------------------------------------------------------------------
# project_page error cases
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_project_page_nonexistent_id(request_factory, test_user):
    """
    GET /project/<ghost_id> must raise Http404 when no document exists.
    """
    from django.http import Http404
    from caper.views import project_page
    req = request_factory.get(f'/project/{_GHOST_ID}')
    req.user = test_user
    with pytest.raises(Http404):
        project_page(req, project_name=_GHOST_ID)


@pytest.mark.integration
@pytest.mark.functional
def test_private_project_unauthenticated_redirect(
        loaded_datasets, request_factory, mongo_collection):
    """
    GET /project/<private_id> without authentication must return a 302
    redirect to the login page, not a 200 or an error.
    """
    from caper.views import project_page

    class _AnonUser:
        username = ''
        email    = ''
        is_authenticated = False
        is_staff         = False

    pid = loaded_datasets['project_small']

    # Confirm the project is actually private before the assertion
    doc = mongo_collection.find_one({'_id': ObjectId(pid)})
    from caper.utils import normalize_visibility_field, is_project_private
    visibility = normalize_visibility_field(doc.get('private'))
    assert is_project_private(visibility), \
        "project_small must be private for this test to be meaningful"

    req = request_factory.get(f'/project/{pid}')
    req.user = _AnonUser()
    resp = project_page(req, project_name=pid)
    assert resp.status_code in (301, 302), \
        f"Expected redirect for unauthenticated access to private project, got {resp.status_code}"
    assert 'login' in resp.get('Location', '').lower(), \
        "Redirect location must point to the login page"


# ---------------------------------------------------------------------------
# download error cases
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_download_nonexistent_project(request_factory, test_user):
    """
    GET /project/<ghost_id>/download must return 404 or redirect,
    not a 500 server error.
    """
    from caper.views import project_download
    req = request_factory.get(f'/project/{_GHOST_ID}/download')
    req.user = test_user
    # project_download calls get_one_project which returns None for unknown IDs;
    # the view then tries to access project['project_name'] which raises an
    # exception handled internally, resulting in a 404 or redirect.
    try:
        resp = project_download(req, project_name=_GHOST_ID)
        assert resp.status_code in (302, 404), \
            f"Expected 302 or 404 for nonexistent project download, got {resp.status_code}"
    except Exception as exc:
        # Acceptable: the view may raise Http404 or similar for a missing project
        from django.http import Http404
        assert isinstance(exc, Http404), \
            f"Unexpected exception type: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# sample page error cases
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.functional
def test_sample_page_nonexistent_sample(
        loaded_datasets, request_factory, test_user):
    """
    GET /project/<id>/sample/NOSUCHSAMPLE must not return a 500.
    Acceptable outcomes: 404 (Http404 raised), redirect, or a rendered error page.
    """
    from caper.views import sample_page
    from django.http import Http404

    pid = loaded_datasets['project_small']
    req = request_factory.get(f'/project/{pid}/sample/NOSUCHSAMPLE_XYZ')
    req.user = test_user
    try:
        resp = sample_page(req, project_name=pid, sample_name='NOSUCHSAMPLE_XYZ')
        assert resp.status_code != 500, \
            "sample_page must not return 500 for a nonexistent sample"
    except Http404:
        pass  # expected: view raises Http404 for unknown sample
