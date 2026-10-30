#!/usr/bin/env python3
"""Create a project on a LOCAL site exactly as a person using the upload form would.

Signs in through the site's login page, then submits /create-project/ with the
fields and headers the page's own upload script sends (new FormData(form) plus
X-Requested-With: XMLHttpRequest).  The running server does everything else:
the same view, middleware, background aggregation and indexing as a browser
upload.

Why not ``manage.py create_project``: that command calls the view inside its own
short-lived process.  The view hands aggregation to a background thread and
returns, the command exits, and the thread dies with it -- so those projects
were left marked ``aggregation_failed`` and without ``AA_PNG_file`` on any
feature (22 local projects, measured 2026-10-28).  Here the long-lived server
process does the aggregation, which is the whole point.

Refuses any host but localhost/127.0.0.1: it is for building local test
projects, never for writing to dev or prod.

Usage (with the local server running, see AGENTS.md):

    LOCAL_SITE_PASSWORD=... python local_upload_project.py \\
        --user jluebeck --name "GBM39 test" --description "Local test" \\
        --visibility public --metadata GBM39_metadata.csv GBM39_agg.tar.gz

Waiting for aggregation needs DB_URI_SECRET and DB_NAME in the environment
(``source caper/config.sh``); without them the script stops after the upload is
accepted and prints the project URL.
"""

import argparse
import getpass
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

LOCAL_HOSTS = {'localhost', '127.0.0.1'}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('files', nargs='+', help='.tar.gz or .zip AmpliconSuite result(s), as chosen in the form')
    p.add_argument('--base-url', default='http://127.0.0.1:8000')
    p.add_argument('--user', required=True, help='username or email to sign in with')
    p.add_argument('--name', required=True, help='project name')
    p.add_argument('--description', required=True)
    p.add_argument('--visibility', default='private', choices=['private', 'public', 'hidden_public'])
    p.add_argument('--members', default='', help='additional members, space or comma separated')
    p.add_argument('--publication', default='', help='PMID or link')
    p.add_argument('--alias', default='')
    p.add_argument('--metadata', help='sample metadata file (.csv, .tsv, .xlsx)')
    p.add_argument('--remap-sample-names', action='store_true',
                   help='tick "remap sample names" (needs a sample_name_alias column in --metadata)')
    p.add_argument('--no-wait', action='store_true', help='return once the upload is accepted')
    p.add_argument('--timeout', type=int, default=3600, help='seconds to wait for aggregation')
    return p.parse_args(argv)


def require_local(base_url):
    host = urlparse(base_url).hostname
    if host not in LOCAL_HOSTS:
        sys.exit(f'Refusing {base_url}: this script only writes to a local site.')


def csrf_from(session, response):
    """The token the page embeds, falling back to the cookie Django set."""
    match = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\']([^"\']+)', response.text)
    return match.group(1) if match else session.cookies.get('csrftoken')


def sign_in(session, base_url, user, password):
    login_url = urljoin(base_url, '/accounts/login/')
    page = session.get(login_url)
    page.raise_for_status()
    response = session.post(login_url, data={
        'csrfmiddlewaretoken': csrf_from(session, page),
        'login': user,
        'password': password,
    }, headers={'Referer': login_url})
    if 'sessionid' not in session.cookies:
        sys.exit(f'Sign-in failed for {user!r} (HTTP {response.status_code}).')


def submit(session, base_url, args):
    form_url = urljoin(base_url, '/create-project/')
    page = session.get(form_url)
    page.raise_for_status()
    data = {
        'csrfmiddlewaretoken': csrf_from(session, page),
        'project_name': args.name,
        'description': args.description,
        'publication_link': args.publication,
        'private': args.visibility,
        'project_members': args.members,
        'alias': args.alias,
        'accept_license': 'on',
    }
    if args.remap_sample_names:
        data['remap_sample_names'] = 'true'
    handles = [open(path, 'rb') for path in args.files]
    try:
        files = [('document', (os.path.basename(path), handle, 'application/octet-stream'))
                 for path, handle in zip(args.files, handles)]
        if args.metadata:
            meta = open(args.metadata, 'rb')
            handles.append(meta)
            files.append(('metadataFile', (os.path.basename(args.metadata), meta, 'application/octet-stream')))
        response = session.post(form_url, data=data, files=files, headers={
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': form_url,
        })
    finally:
        for handle in handles:
            handle.close()
    if response.status_code != 200 or not response.headers.get('Content-Type', '').startswith('application/json'):
        sys.exit(f'Upload not accepted (HTTP {response.status_code}): {response.text[:500]}')
    redirect = response.json().get('redirect', '')
    project_id = redirect.rstrip('/').rsplit('/', 1)[-1]
    return project_id, urljoin(base_url, redirect)


def wait_for_aggregation(project_id, timeout):
    """Poll the local database read-only; page views would count as visits."""
    from bson import ObjectId
    from pymongo import MongoClient

    client = MongoClient(os.environ['DB_URI_SECRET'], serverSelectionTimeoutMS=5000)
    client.admin.command('ping')   # address is unknown until a connection is made
    host = client.address[0]
    if host not in LOCAL_HOSTS:
        sys.exit(f'Refusing to poll database host {host}: local only.')
    projects = client[os.environ['DB_NAME']]['projects']
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = projects.find_one({'_id': ObjectId(project_id)},
                                {'FINISHED?': 1, 'aggregation_failed': 1, 'error_message': 1,
                                 'project_name': 1, 'sample_count': 1})
        if doc is None:
            # A failed upload's placeholder is discarded (_discard_failed_upload).
            sys.exit('The project is gone: the upload failed and was cleaned up; check the server log.')
        if doc.get('aggregation_failed'):
            sys.exit(f"Aggregation failed: {doc.get('error_message', '(no message)')}")
        if doc.get('FINISHED?'):
            return doc
        time.sleep(5)
    sys.exit(f'Still aggregating after {timeout} s; check the server log.')


def main(argv=None):
    args = parse_args(argv)
    require_local(args.base_url)
    for path in args.files + ([args.metadata] if args.metadata else []):
        if not os.path.isfile(path):
            sys.exit(f'No such file: {path}')
    password = os.environ.get('LOCAL_SITE_PASSWORD') or getpass.getpass(f'Password for {args.user}: ')

    session = requests.Session()
    sign_in(session, args.base_url, args.user, password)
    project_id, url = submit(session, args.base_url, args)
    print(f'Upload accepted: {url}')
    if args.no_wait or 'DB_URI_SECRET' not in os.environ:
        return
    doc = wait_for_aggregation(project_id, args.timeout)
    print(f"Finished: {doc.get('project_name')} -- {doc.get('sample_count')} samples")


if __name__ == '__main__':
    main()
