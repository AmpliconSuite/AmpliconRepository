"""
One error shape for `/api/v1/`.

The v1 views return `{"error": "..."}`.  DRF's own machinery does not: when the
framework raises before a view body runs -- throttling (429), a malformed JSON
body (400), an unsupported method (405) -- it renders `{"detail": "..."}`
instead.  A client therefore has to check two keys to find out what went wrong,
and the failures that need the *most* reliable handling (429 back-off above all)
are exactly the ones on the `detail` side.

`normalize_api_v1_errors` is installed as DRF's `EXCEPTION_HANDLER`.  It calls
the default handler and then, **only for `/api/v1/` paths**, restates the body
in the shape the v1 views already use, adding a stable machine-readable `code`.

Why the path check rather than normalizing everywhere: the legacy upload
endpoints (`/upload_api/`, `/add_samples_to_project_api/`) are consumed by
released versions of AmpliconSuiteAggregator, which parse those responses.
Their error bodies are a contract with software already in the field and are
deliberately left exactly as they are.  This handler is opt-in by URL prefix so
that adding it cannot reach them.

Response shape for every v1 error:

    {"error": "<human-readable message>", "code": "<stable slug>"}

`error` is unchanged from what the views already emitted, so nothing that reads
it breaks.  `code` is additive, and is what a program should branch on -- the
prose in `error` is not a contract, the slug is.
"""

from rest_framework.views import exception_handler as drf_exception_handler

API_V1_PREFIX = '/api/v1/'

# Status code -> stable slug.  These are the codes DRF itself can raise before a
# v1 view body runs; the views' own errors carry their code explicitly.
_CODE_BY_STATUS = {
    400: 'bad_request',
    401: 'authentication_required',
    403: 'permission_denied',
    404: 'not_found',
    405: 'method_not_allowed',
    406: 'not_acceptable',
    415: 'unsupported_media_type',
    429: 'rate_limited',
    503: 'service_unavailable',
}


def _message_from(data, fallback):
    """Pull a human-readable string out of whatever DRF put in the body."""
    if isinstance(data, dict):
        for key in ('detail', 'error'):
            value = data.get(key)
            if isinstance(value, str):
                return value
        # Field-validation errors: {"field": ["msg", ...]}.  Keep the first.
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], str):
                return value[0]
            if isinstance(value, str):
                return value
    elif isinstance(data, list) and data and isinstance(data[0], str):
        return data[0]
    return fallback


def normalize_api_v1_errors(exc, context):
    """DRF EXCEPTION_HANDLER: uniform `{"error", "code"}` bodies under /api/v1/."""
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not an APIException -- Django's 500 handling owns it.  Deliberately
        # not converted: turning an unhandled crash into a tidy JSON body would
        # hide it from the error reporting that should see it.
        return None

    request = context.get('request')
    path = getattr(request, 'path', '') or ''
    if not path.startswith(API_V1_PREFIX):
        return response

    code = getattr(exc, 'default_code', None) or _CODE_BY_STATUS.get(
        response.status_code, 'error')
    # DRF's own slug for throttling is 'throttled'; 'rate_limited' is the name
    # used in the published docs and the Retry-After header's context.
    if response.status_code == 429:
        code = 'rate_limited'

    message = _message_from(response.data,
                            _CODE_BY_STATUS.get(response.status_code, 'Error'))
    body = {'error': message, 'code': code}

    # Throttling carries the one piece of data a client must act on.  DRF sets
    # the Retry-After header; repeat it in the body so a client that only parses
    # JSON can still back off correctly.
    retry_after = response.headers.get('Retry-After') if hasattr(
        response, 'headers') else None
    if retry_after:
        body['retry_after'] = int(retry_after) if str(retry_after).isdigit() \
            else retry_after

    response.data = body
    return response


# ── Errors raised before DRF is reached ─────────────────────────────────────
#
# The handler above only sees exceptions raised *inside* a DRF view.  A URL that
# resolves to nothing never reaches one: Django calls the project's handler404,
# which is Mezzanine's HTML error page.  A client that fetches
# /api/v1/openapi.json against a deployment that lacks it therefore receives
# 21 KB of HTML where JSON was promised -- measured against production on
# 2026-09-04, before this existed.  Same for an unhandled crash and handler500.
#
# These two wrappers answer in JSON under /api/v1/ and delegate everything else
# to Mezzanine unchanged, so the site's own error pages are untouched.

from django.http import JsonResponse
from mezzanine.core.views import page_not_found as _mezzanine_404
from mezzanine.core.views import server_error as _mezzanine_500


def api_aware_page_not_found(request, exception=None, template_name='errors/404.html'):
    if request.path.startswith(API_V1_PREFIX):
        return JsonResponse(
            {'error': 'No such endpoint. See /api/v1/openapi.json for the '
                      'endpoints this API has.',
             'code': 'endpoint_not_found'},
            status=404)
    return _mezzanine_404(request, exception, template_name)


def api_aware_server_error(request, template_name='errors/500.html'):
    if request.path.startswith(API_V1_PREFIX):
        # Deliberately says nothing about the failure: this path is reached by
        # unhandled exceptions, and their text is not fit for a public body.
        return JsonResponse(
            {'error': 'Internal server error', 'code': 'internal_error'},
            status=500)
    return _mezzanine_500(request, template_name)
