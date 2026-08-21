"""
Authorization on the admin statistics CSV exports.

Both download views sat on /admin-stats/ URLs next to views that were decorated
staff-only, but carried no decorator themselves, so anyone who knew or guessed
the path could fetch them:

    /admin-stats/download/user/     every username, email, join date, last login
    /admin-stats/download/project/  every project row, including its member list

These tests pin the decorator in place. The unauthorized cases are the point:
they must fail before the view body runs, which is also why they need no
database -- ``user_passes_test`` redirects first.
"""

import pytest


ADMIN_STATS_DOWNLOAD_VIEWS = ('user_stats_download', 'project_stats_download')


def _request(request_factory, user, path='/admin-stats/download/user/'):
    # HTTP_HOST because the rejection path builds an absolute redirect URI, which
    # goes through ALLOWED_HOSTS; RequestFactory's default 'testserver' is not in it.
    request = request_factory.get(path, HTTP_HOST='localhost')
    request.user = user
    return request


def _view(name):
    from caper import views
    return getattr(views, name)


@pytest.fixture
def anonymous_user():
    from django.contrib.auth.models import AnonymousUser
    return AnonymousUser()


# ---------------------------------------------------------------------------
# Anonymous and non-staff callers are turned away
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('view_name', ADMIN_STATS_DOWNLOAD_VIEWS)
def test_anonymous_user_cannot_download(view_name, request_factory,
                                        anonymous_user):
    response = _view(view_name)(_request(request_factory, anonymous_user))

    assert response.status_code == 302
    assert response['Location'].startswith('/notfound/')
    assert 'text/csv' not in response.get('Content-Type', '')


@pytest.mark.parametrize('view_name', ADMIN_STATS_DOWNLOAD_VIEWS)
def test_authenticated_non_staff_user_cannot_download(view_name,
                                                      request_factory,
                                                      non_member_user):
    """An ordinary logged-in account is no more entitled to this than a stranger."""
    assert non_member_user.is_staff is False

    response = _view(view_name)(_request(request_factory, non_member_user))

    assert response.status_code == 302
    assert response['Location'].startswith('/notfound/')
    assert 'text/csv' not in response.get('Content-Type', '')


# ---------------------------------------------------------------------------
# Staff callers still get their CSV
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_staff_user_downloads_user_csv(request_factory, admin_user):
    from caper.views import user_stats_download

    response = user_stats_download(_request(request_factory, admin_user))

    assert response.status_code == 200
    assert response['Content-Type'] == 'text/csv'
    assert response['Content-Disposition'].startswith('attachment; filename="users_')

    header = response.content.decode().splitlines()[0]
    assert header == 'username,email,date_joined,last_login'


@pytest.mark.integration
def test_staff_user_downloads_project_csv(request_factory, admin_user):
    from caper.views import project_stats_download

    response = project_stats_download(
        _request(request_factory, admin_user,
                 path='/admin-stats/download/project/'))

    assert response.status_code == 200
    assert response['Content-Type'] == 'text/csv'
    assert response['Content-Disposition'].startswith('attachment; filename="projects_')

    header = response.content.decode().splitlines()[0]
    assert header.startswith('project_name,description,project_members')


# ---------------------------------------------------------------------------
# The whole /admin-stats/ and /data-qc/ surface, not just the two that were found
# ---------------------------------------------------------------------------

def test_every_admin_route_is_staff_gated():
    """No admin view may be reachable by a non-staff caller.

    Written against the URLconf rather than a hand-kept list of view names, so a
    new admin route added later is covered the day it is added. Two protection
    styles count, because the codebase uses both: the ``user_passes_test``
    decorator, and an ``is_staff`` check in the first lines of the body.
    """
    import inspect
    from django.urls import get_resolver

    admin_prefixes = ('admin-', 'data-qc')
    checked = []

    for pattern in get_resolver().url_patterns:
        route = getattr(pattern, 'pattern', None)
        route = str(route) if route else ''
        if not route.startswith(admin_prefixes):
            continue

        # A decorated view is a wrapper closure, so read through __wrapped__ to
        # the real function. inspect.getsource() on it starts at the first
        # decorator line, so one read covers both protection styles.
        wrapped = getattr(pattern.callback, '__wrapped__', pattern.callback)
        source = inspect.getsource(wrapped)
        protected = 'user_passes_test' in source or 'is_staff' in source

        checked.append(route)
        assert protected, (
            f"admin route {route!r} ({wrapped.__name__}) has no staff check"
        )

    # Guard against the loop silently matching nothing and passing vacuously.
    assert len(checked) >= 10, f"expected to check the admin routes, saw {checked}"
    assert any('admin-stats/download' in route for route in checked)
