"""Absolute URLs that survive the load balancer.

The ELB terminates TLS, so the WSGI scheme inside the container is `http` and
anything built from it hands the caller an `http://` URL for a site that only
answers on `https://`. That was already logged as a bug once, against the batch
download's `download_url` (#600), and the fix lived at the one call site that
had the bug. Written here instead so the next surface that builds a URL gets it
right without knowing the history.

`build_absolute_uri()` is not used: besides the scheme, it raises DisallowedHost
in test environments, which turns a URL field into a 500.
"""


def request_scheme(request):
    """The scheme the *client* used, not the one the container sees."""
    forwarded = request.META.get('HTTP_X_FORWARDED_PROTO', '')
    # X-Forwarded-Proto can be a list when more than one proxy is in the path;
    # the client-facing one is first.
    return (forwarded.split(',')[0].strip()
            or request.META.get('wsgi.url_scheme')
            or ('https' if request.META.get('HTTPS') == 'on' else 'http'))


def request_host(request):
    """The host the client addressed.

    `HTTP_HOST` is what a real WSGI server sets from the Host header. It is
    absent under Django's RequestFactory, so fall back to SERVER_NAME the way
    Django's own `get_host()` does -- otherwise every URL built in a test is
    silently relative and a test asserting on the scheme cannot see anything.
    """
    host = request.META.get('HTTP_HOST', '')
    if host:
        return host
    name = request.META.get('SERVER_NAME', '')
    if not name:
        return ''
    port = str(request.META.get('SERVER_PORT', '') or '')
    default = '443' if request_scheme(request) == 'https' else '80'
    return name if port in ('', default) else f'{name}:{port}'


def absolute_base(request):
    """scheme://host, or empty when the request carries no host at all."""
    if request is None:
        return ''
    host = request_host(request)
    if not host:
        return ''
    return f'{request_scheme(request)}://{host}'


def absolute_url(request, path):
    """`path` made absolute against the client-facing scheme and host."""
    base = absolute_base(request)
    return f'{base}{path}' if base else path
