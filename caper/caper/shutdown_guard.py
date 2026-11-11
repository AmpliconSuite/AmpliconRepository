"""
Refuse new uploads while shutdown mode is on.

Shutdown mode (`system_settings/{_id: system_flags}.shutdown_pending`, toggled
from /admin-prepare-shutdown/ or caper/caper/shutdown_mode.py) is how a deploy stops new
background work before the restart that would kill it.  Until this middleware
it reached only the templates: the banner greys out the create and edit links,
and nothing refused the request itself.  Measured on dev 2026-11-02 with the
flag on: a form POST to /create-project/ and three run_uploader.py calls to
/add_samples_to_project_api/ were all accepted and aggregated.

The paths refused here are the ones the banner already tells a browser user are
unavailable, plus the two upload endpoints the AmpliconSuite pipeline calls and the admins'
replace route.
Only POSTs are refused; the pages themselves still load.  The flag is read only
for a POST to one of these paths, so no other request pays for the lookup.

A browser is sent back with the banner's own message.  An API client gets a 503
with Retry-After, which run_uploader.py reports with the response body.

One refusal is not here: batch_sample_download in views.py refuses its emailed
link path, which runs in the background, but still streams a batch small enough
to answer in the request.  Which path a batch takes is decided inside the view,
after access filtering, so this middleware cannot tell the two apart.
"""

import json
import re

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect

from .context_processor import get_shutdown_pending

SHUTDOWN_MESSAGE = ('That feature is not available currently, server shutdown is pending. '
                    'Please try again later.')

# Seconds.  A deploy's shutdown window is normally a few minutes.
RETRY_AFTER = '600'

_API_UPLOAD_RE = re.compile(r'^/(?:upload_api(?:/replace/[^/]+)?|add_samples_to_project_api)/?$')
_PAGE_UPLOAD_RE = re.compile(r'^/(?:create-project|create-empty-project|project/[^/]+/edit)/?$')


class ShutdownUploadGuardMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'POST':
            path = request.path
            if _API_UPLOAD_RE.match(path):
                if get_shutdown_pending():
                    return self._api_refusal()
            elif _PAGE_UPLOAD_RE.match(path):
                if get_shutdown_pending():
                    messages.error(request, SHUTDOWN_MESSAGE)
                    if path.rstrip('/') == '/create-empty-project':
                        return redirect('profile')
                    return redirect(path)
        return self.get_response(request)

    @staticmethod
    def _api_refusal():
        response = HttpResponse(json.dumps({'error': SHUTDOWN_MESSAGE}),
                                content_type='application/json', status=503)
        response['Retry-After'] = RETRY_AFTER
        response['Cache-Control'] = 'no-store'
        return response
