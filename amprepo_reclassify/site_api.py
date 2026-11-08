"""Talking to a site. Standard library only, because `setup` runs before the environment exists."""

import http.client
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid

from . import __version__

SITES = {'prod': 'https://ampliconrepository.org',
         'dev': 'https://dev.ampliconrepository.org'}
USER_AGENT = 'amprepo_reclassify/%s' % __version__
RETRIES = 4
REDIRECTS = (301, 302, 303, 307, 308)
# Longer than the load balancer's 900 s idle limit, so its answer is the one that's seen.
UPLOAD_TIMEOUT = 1000
STREAM_CHUNK = 1024 * 1024


class SiteError(Exception):
    pass


class Refused(SiteError):
    """The site answered and said no. `code` is what a program branches on."""

    def __init__(self, message, code=None, latest_id=None):
        super().__init__(message)
        self.code, self.latest_id = code, latest_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at a redirect, so the token never goes to storage along with a signed URL."""

    def redirect_request(self, *args, **kwargs):
        return None


def _multipart(fields, files):
    """(content type, length, a function giving a new body) for streaming files of any size.

    *files* is {field: path}. Each call of the function reads the files again, so a retry works.
    """
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode())
    for name, path in files.items():
        filename = os.path.basename(path).replace('"', '_')
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n'
                      % (boundary, name, filename)).encode())
        parts.append(path)
        parts.append(b'\r\n')
    parts.append(('--%s--\r\n' % boundary).encode())

    def body():
        for part in parts:
            if isinstance(part, bytes):
                yield part
            else:
                with open(part, 'rb') as handle:
                    yield from iter(lambda: handle.read(STREAM_CHUNK), b'')

    length = sum(len(p) if isinstance(p, bytes) else os.path.getsize(p) for p in parts)
    return 'multipart/form-data; boundary=%s' % boundary, length, body


class Site:
    def __init__(self, name, token=None, base_url=None):
        self.name = name
        self.token = token
        self.base_url = (base_url or SITES[name]).rstrip('/')
        self._api_opener = urllib.request.build_opener(_NoRedirect)

    def project_url(self, project_id):
        return '%s/project/%s' % (self.base_url, project_id)

    def _api_open(self, path):
        """Open an API path, waiting out rate limits. A redirect comes back as an HTTPError."""
        request = urllib.request.Request(self.base_url + '/api/v1' + path,
                                         headers={'User-Agent': USER_AGENT})
        if self.token:
            request.add_header('Authorization', 'Token ' + self.token)
        for attempt in range(RETRIES):
            try:
                return self._api_opener.open(request, timeout=120)
            except urllib.error.HTTPError as error:
                if error.code in REDIRECTS:
                    return error
                if error.code == 429 and attempt < RETRIES - 1:
                    time.sleep(int(error.headers.get('Retry-After') or 60))
                    continue
                if error.code == 401:
                    raise SiteError('%s refused the token (401)' % self.name)
                raise SiteError('%s: HTTP %d for %s' % (self.name, error.code, path))
            except urllib.error.URLError as error:
                raise SiteError('%s: %s' % (self.name, error.reason))
        raise SiteError('%s: still rate limited for %s' % (self.name, path))

    def list_projects(self):
        """Every project this caller can read, with the ids of its older versions."""
        with self._api_open('/projects/') as response:
            return json.load(response)

    def _storage_request(self, url, **headers):
        return urllib.request.Request(url, headers=dict(headers, **{'User-Agent': USER_AGENT}))

    def archive_size(self, project_id):
        """The archive's size in bytes, or None if the site has no stored copy to hand out.

        One byte of a ranged read, because a signed storage URL is for GET only. The site
        has no size field, and neither step changes anything on it.
        """
        response = self._api_open('/projects/%s/download/' % project_id)
        with response:
            if response.status not in REDIRECTS:
                length = response.headers.get('Content-Length')
                return int(length) if length else None
            location = response.headers['Location']
        try:
            request = self._storage_request(location, Range='bytes=0-0')
            with urllib.request.urlopen(request, timeout=120) as stored:
                match = re.search(r'/(\d+)$', stored.headers.get('Content-Range') or '')
                return int(match.group(1)) if match else int(stored.headers['Content-Length'])
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                return None
            raise SiteError('%s: storage answered HTTP %d' % (self.name, error.code))

    def download(self, project_id, dest):
        """Fetch the archive to dest. A partial file never takes the final name."""
        response = self._api_open('/projects/%s/download/' % project_id)
        if response.status in REDIRECTS:
            location = response.headers['Location']
            response.close()
            try:
                response = urllib.request.urlopen(self._storage_request(location), timeout=600)
            except urllib.error.HTTPError as error:
                raise SiteError('storage answered HTTP %d for %s. The site has no stored archive '
                                'for this version.' % (error.code, project_id))
        partial = dest + '.part'
        with response, open(partial, 'wb') as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
            expected = response.headers.get('Content-Length')
        if expected and os.path.getsize(partial) != int(expected):
            raise SiteError('download of %s stopped at %d of %s bytes'
                            % (project_id, os.path.getsize(partial), expected))
        os.replace(partial, dest)

    def project(self, project_id):
        """One version as the API describes it."""
        with self._api_open('/projects/%s/' % project_id) as response:
            return json.load(response)

    # ---- writing, for site admins ----

    def _post(self, path, body=b'', headers=None, timeout=120):
        """POST and return the JSON answer. *body* may be a function that gives a new body."""
        for attempt in range(RETRIES):
            request = urllib.request.Request(
                self.base_url + path, data=body() if callable(body) else body, method='POST',
                headers=dict(headers or {}, **{'User-Agent': USER_AGENT}))
            if self.token:
                request.add_header('Authorization', 'Token ' + self.token)
            try:
                with self._api_opener.open(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < RETRIES - 1:
                    time.sleep(int(error.headers.get('Retry-After') or 60))
                    continue
                raise self._error_from(error, path)
            except (OSError, http.client.HTTPException) as error:
                raise SiteError('%s: lost the connection during %s: %s' % (self.name, path, error))
        raise SiteError('%s: still rate limited for %s' % (self.name, path))

    def _error_from(self, error, path):
        try:
            answer = json.loads(error.read() or b'{}')
        except ValueError:
            answer = {}
        message = answer.get('error') if isinstance(answer, dict) else None
        if error.code >= 500:
            return SiteError('%s: HTTP %d for %s. %s' % (self.name, error.code, path, message or ''))
        code = answer.get('code')
        if error.code == 403 and code is None:
            return Refused('%s: a 403 for %s that did not come from the site, so probably the WAF'
                           % (self.name, path), 'blocked')
        return Refused('%s: %s' % (self.name, message or 'HTTP %d for %s' % (error.code, path)),
                       code, answer.get('latest_id'))

    def check_admin(self):
        """Raises Refused unless the token is a site admin's. Changes nothing.

        Asks about a job that can't exist, so an admin gets a 404 and anyone else a 401 or 403.
        """
        try:
            self._post('/upload_api/replace-status/check/')
        except Refused as refused:
            if refused.code != 'not_found':
                raise

    def keep_current_list(self):
        """The projects whose owners opted in to updates, private ones too. Admins only."""
        if not self.token:
            raise SiteError('reading %s\'s keep-current list needs an admin token' % self.name)
        return self._post('/upload_api/keep-current/')['projects']

    def replace(self, project_id, archive_path, expected_latest_id, dry_run, metadata_path=None):
        """Send an archive. The answer holds the `job_id` to follow."""
        files = {'document': archive_path}
        if metadata_path:
            files['metadataFile'] = metadata_path
        content_type, length, body = _multipart(
            {'dry_run': 'true' if dry_run else 'false', 'expected_latest_id': expected_latest_id}, files)
        return self._post('/upload_api/replace/%s/' % project_id, body,
                          {'Content-Type': content_type, 'Content-Length': str(length)},
                          timeout=UPLOAD_TIMEOUT)

    def replace_status(self, job_id):
        return self._post('/upload_api/replace-status/%s/' % job_id)

    def prepare_page(self, version_id):
        """Ask the site to store what this version's page needs. True if it's already done."""
        return self._post('/upload_api/replace-prepare/%s/' % version_id)['page_ready']
