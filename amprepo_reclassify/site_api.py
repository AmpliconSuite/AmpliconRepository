"""Reading a site's API. Standard library only, because `setup` runs before the environment exists."""

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request

from . import __version__

SITES = {'prod': 'https://ampliconrepository.org',
         'dev': 'https://dev.ampliconrepository.org'}
USER_AGENT = 'amprepo_reclassify/%s' % __version__
RETRIES = 4
REDIRECTS = (301, 302, 303, 307, 308)


class SiteError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at a redirect, so the token never goes to storage along with a signed URL."""

    def redirect_request(self, *args, **kwargs):
        return None


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
