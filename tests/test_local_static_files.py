"""With S3 static files off, the site serves caper/static itself.

DEBUG is False, so runserver serves no static files; before this, every
/static/ request on a local run was a 404 (logo, style.css, jQuery; measured
2026-10-29).
"""

import pytest
from django.conf import settings
from django.http import Http404
from django.urls import resolve


pytestmark = pytest.mark.skipif(getattr(settings, 'USE_S3', False),
                                reason='static files come from S3 in this configuration')


def _get(request_factory, path):
    match = resolve(path)
    return match.func(request_factory.get(path), *match.args, **match.kwargs)


def test_static_files_are_served(request_factory):
    response = _get(request_factory, '/static/img/AmpliconSuiteIconCirc.png')
    assert response.status_code == 200
    assert response['Content-Type'] == 'image/png'


def test_static_route_stays_inside_the_static_root(request_factory):
    with pytest.raises(Exception) as caught:
        _get(request_factory, '/static/../caper/settings.py')
    # Django refuses the path (SuspiciousFileOperation) or finds nothing.
    assert caught.type.__name__ in ('SuspiciousFileOperation', 'Http404')
