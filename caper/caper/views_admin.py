"""
Admin-related views for the Caper application.
This module contains all administrative functions that require staff privileges.
"""

import logging
import os
import csv
import contextlib
import subprocess
import shutil
import datetime
import threading
import time
from pathlib import Path

from django.http import FileResponse, HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.conf import settings

from django.utils import timezone
from django.contrib import messages

from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags

import boto3

from bson.objectid import ObjectId

from . import storage_stats
from .account_deletion import RELEASE, plan_account_deletion, summarize
from .forms import FeaturedProjectForm, DeletedProjectForm, SendEmailForm
from .utils import (
    collection_handle, collection_handle_primary, current_flags, fs_handle,
    audit_log_handle, delete_gridfs_file,
    get_one_project, get_one_deleted_project, prepare_project_linkid,
    check_if_db_field_exists, get_date_short,
    form_to_dict, get_date, db_handle, db_handle_primary, format_visibility_for_display,
    get_project_version_chain, normalize_visibility_field, is_project_private,
    PUBLIC_QUERY_VALUES, RESTRICTED_QUERY_VALUES,
)
from .project_version_cleanup import (
    delete_gridfs_payload_for_project, delete_payload_within_deadline,
)
from .background_tasks import get_background_task_status
from .project_status import (
    DETACHED,
    LIVE,
    SOFT_DELETED,
    STATUS_QUERIES,
    classify,
    is_reachable_by_url,
    is_tombstone,
    iter_previous_versions,
    status_after,
    status_flags,
    status_query,
)
from . import download_totals, lineage, ownership_survey, provenance

from .extra_metadata import *

from .site_stats import get_latest_site_statistics, regenerate_site_statistics
from .tar_utils import list_project_tar_contents


def _partition_admin_stats_projects(projects):
    """Split current projects using both legacy and string visibility values."""
    public_projects = []
    private_projects = []
    for project in projects:
        visibility = normalize_visibility_field(project.get('private', 'private'))
        if is_project_private(visibility):
            private_projects.append(project)
        else:
            public_projects.append(project)
    return public_projects, private_projects


def _get_s3_file_size_bytes_admin(s3_uri):
    """
    Returns the size in bytes of the S3 object at *s3_uri*, or None on any error.
    Mirrors the helper in views.py but lives here to avoid a circular import.
    """
    if not s3_uri:
        return None
    try:
        without_prefix = s3_uri[len('s3://'):]
        bucket, _, key = without_prefix.partition('/')
        if not bucket or not key:
            return None
        profile = getattr(settings, 'AWS_PROFILE_NAME', None) or 'default'
        session = boto3.Session(profile_name=profile)
        s3client = session.client('s3')
        resp = s3client.head_object(Bucket=bucket, Key=key)
        return resp.get('ContentLength')
    except Exception as e:
        logging.debug(f"Could not get S3 file size for {s3_uri}: {e}")
        return None


def _short_err(exc):
    """Compact, human-readable one-liner for a botocore/boto3 exception."""
    msg = str(exc).strip().replace('\n', ' ')
    return (msg[:300] + '…') if len(msg) > 300 else msg


def _s3_health_check():
    """
    Live check of the AWS credentials the app uses for S3, surfaced on the admin
    version-details page. For each boto3 profile the app uses, resolve the
    credentials and confirm they can actually reach S3 — so an expired SSO token
    or a broken instance role is visible at a glance here, instead of only
    showing up when a download/upload/static-sync fails.

    Returns a list of dicts (one per distinct profile/bucket the app uses):
        {'used_for', 'profile', 'source', 'identity', 'bucket', 'status', 'detail'}
    where status is 'ok' | 'error' | 'disabled'.
    """
    from botocore.config import Config as BotoConfig

    if not getattr(settings, 'USE_S3_DOWNLOADS', False) and not getattr(settings, 'USE_S3', False):
        return [{
            'used_for': '—', 'profile': '—', 'source': '—', 'identity': '—', 'bucket': '—',
            'status': 'disabled',
            'detail': 'S3 is not enabled (S3_FILE_DOWNLOADS and S3_STATIC_FILES are both off).',
        }]

    # The app uses two profiles: downloads use settings.AWS_PROFILE_NAME
    # (default 'default'); the startup static-sync in apps.py uses the
    # AWS_PROFILE_NAME env var, defaulting to 'amprepo'.
    download_profile = getattr(settings, 'AWS_PROFILE_NAME', None) or 'default'
    static_profile = os.environ.get('AWS_PROFILE_NAME', 'amprepo')
    download_bucket = getattr(settings, 'S3_DOWNLOADS_BUCKET', 'amprepo-private')

    # (used_for, profile, bucket) — dedup on (profile, bucket) below.
    checks = [
        ('downloads (amprepo-private)', download_profile, download_bucket),
        ('static sync (amprepobucket)', static_profile, 'amprepobucket'),
    ]

    # Keep the page snappy even if IMDS/network is unreachable.
    boto_cfg = BotoConfig(connect_timeout=3, read_timeout=5, retries={'max_attempts': 1})

    results = []
    seen = set()
    for used_for, profile, bucket in checks:
        key = (profile, bucket)
        if key in seen:
            continue
        seen.add(key)

        entry = {'used_for': used_for, 'profile': profile, 'bucket': bucket,
                 'source': 'unknown', 'identity': '—'}
        try:
            session = boto3.Session(profile_name=profile)

            creds = session.get_credentials()
            if creds is None:
                entry['status'] = 'error'
                entry['source'] = 'none'
                entry['detail'] = 'No credentials could be resolved for this profile.'
                results.append(entry)
                continue
            # e.g. 'iam-role' (instance role), 'sso', 'shared-credentials-file' (static key)
            entry['source'] = getattr(creds, 'method', 'unknown')

            # Who are we, really? (also forces SSO/role token load, surfacing expiry.)
            try:
                sts = session.client('sts', config=boto_cfg)
                entry['identity'] = sts.get_caller_identity().get('Arn', 'unknown')
            except Exception as e:
                entry['identity'] = f'(sts failed: {_short_err(e)})'

            # Can we actually reach the bucket with these creds?
            s3 = session.client('s3', config=boto_cfg)
            s3.head_bucket(Bucket=bucket)
            entry['status'] = 'ok'
            entry['detail'] = f'Reached s3://{bucket} successfully.'
        except Exception as e:
            entry['status'] = 'error'
            entry['detail'] = _short_err(e)
        results.append(entry)

    return results


def _run_audit_checks(project, latest_entry):
    """
    Compare *latest_entry* (an audit-log document) against the live *project* document.
    Fetches the current S3 file size via head_object.

    Returns a dict:
        {
            'checks':       list of check dicts (field/log_value/live_value/match/missing),
            'all_match':    True  – every present field matches,
            'any_mismatch': True  – at least one present field differs,
            'status':       'pass' | 'mismatch' | 'missing_data',
        }
    """
    def _str(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s not in ('', 'None') else None

    def _make_check(field, log_raw, live_raw, *, numeric=False):
        log_val  = _str(log_raw)
        live_val = _str(live_raw)
        if numeric and log_val is not None and live_val is not None:
            try:
                match = int(log_val) == int(live_val)
            except (ValueError, TypeError):
                match = log_val == live_val
        else:
            match = log_val == live_val
        return {
            'field':      field,
            'log_value':  log_val  if log_val  is not None else '—',
            'live_value': live_val if live_val is not None else '—',
            'match':      match,
            'missing':    log_val is None or live_val is None,
        }

    s3_uri = latest_entry.get('s3_uri')
    live_s3_size = _get_s3_file_size_bytes_admin(s3_uri) if s3_uri else None
    # 'runs' is only counted when it was actually loaded. Callers that project
    # it away get None here, which _make_check renders as missing -- the honest
    # answer. Falling through to len({}) would report a confident 0 for a
    # project whose samples simply were not fetched.
    live_sample_count = project.get('sample_count')
    if live_sample_count is None and 'runs' in project:
        live_sample_count = len(project['runs'] or {})

    checks = [
        _make_check('AA Version',          latest_entry.get('AA_version'),          project.get('AA_version')),
        _make_check('AC Version',          latest_entry.get('AC_version'),          project.get('AC_version')),
        _make_check('ASP Version',         latest_entry.get('ASP_version'),         project.get('ASP_version')),
        _make_check('Sample Count',        latest_entry.get('sample_count'),        live_sample_count, numeric=True),
        _make_check('tar.gz Size (bytes)', latest_entry.get('s3_file_size_bytes'),  live_s3_size,      numeric=True),
    ]

    any_mismatch = any(not c['match'] and not c['missing'] for c in checks)
    all_match    = not any_mismatch and all(c['match'] or c['missing'] for c in checks)
    has_missing  = any(c['missing'] for c in checks)

    if any_mismatch:
        status = 'mismatch'
    elif has_missing:
        status = 'missing_data'
    else:
        status = 'pass'

    return {
        'checks':       checks,
        'all_match':    all_match,
        'any_mismatch': any_mismatch,
        'status':       status,
    }

def check_datetime(projects):
    import dateutil.parser
    errors = 0
    for project in projects:
        try:
            date = dateutil.parser.parse(project['date'])
            date.strftime(f'%Y-%m-%dT%H:%M:%S')
        except:
            errors += 1

    if errors == 0:
        datetime_status = 1
    else:
        datetime_status = 0

    return datetime_status


def check_sample_count_status(projects):
    for project in projects:
        if not 'sample_count' in project:
            return 1

    return 0


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_featured_projects(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    if request.method == "POST":

        form = FeaturedProjectForm(request.POST)
        form_dict = form_to_dict(form)
        project_name = form_dict['project_name']
        project_id = form_dict['project_id']
        featured = form_dict['featured']

        project = get_one_project(project_id)
        query = {'_id': ObjectId(project_id)}
        new_val = {"$set": {'featured': featured}}
        collection_handle_primary.update_one(query, new_val)

    # Handle both legacy boolean False and new string 'public'
    public_projects = list(collection_handle_primary.find(
        status_query(LIVE, private={'$in': PUBLIC_QUERY_VALUES})))
    for proj in public_projects:
        prepare_project_linkid(proj)

    return render(request, 'pages/admin_featured_projects.html', {'public_projects': public_projects})


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_version_details(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    try:
        details = []
        comment_char = "#"
        sep = "="
        with open("version.txt", 'r') as version_file:
            for line in version_file:
                l = line.strip()
                if l and not l.startswith(comment_char):
                    key_value = l.split(sep)
                    key = key_value[0].strip()
                    value = sep.join(key_value[1:]).strip().strip('"')
                    details.append({"name": key, "value": value})
    except:
        details = [{"name": "version", "value": "unknown"}, {"name": "creator", "value": "unknown"},
                   {"name": "date", "value": "unknown"}]

    env_to_skip = ['DB_URI_SECRET', "GOOGLE_SECRET", "GLOBUS_SECRET"]
    env = []
    for key, value in os.environ.items():
        if not ("SECRET" in key) and not key in env_to_skip:
            env.append({"name": key, "value": value})

    try:
        gitcmd = 'export GIT_DISCOVERY_ACROSS_FILESYSTEM=1;git config --global --add safe.directory /srv;git status;echo \"Commit id:\"; git rev-parse HEAD'
        git_result = subprocess.check_output(gitcmd, shell=True)
        git_result = git_result.decode("UTF-8")
    except:
        git_result = "git status call failed"

    try:
        # try to account for different working directories as found in dev and prod
        conf_file_locs = ["../config.sh", "./caper/config.sh", "./config.sh"]
        manage_file_locs = ["./manage.py", "./caper/manage.py"]
        for apath in conf_file_locs:
            my_file = Path(apath)
            if my_file.is_file():
                config_path = apath
                break
        for apath in manage_file_locs:
            my_file = Path(apath)
            if my_file.is_file():
                manage_path = apath
                break

        settingscmd = f'source {config_path};python {manage_path} diffsettings --all'

        settings_raw_result = subprocess.check_output(settingscmd, shell=True)
        settings_result = ""
        for line in settings_raw_result.splitlines():
            line_enc = line.decode("UTF-8")
            if not (("SECRET" in line_enc.upper()) or ("mongodb" in line_enc)):
                settings_result = settings_result + line_enc + "\n"

    except:
        settings_result = "An error occurred getting the contents of settings.py."

    try:
        s3_status = _s3_health_check()
    except Exception as e:
        s3_status = [{'used_for': '—', 'profile': '—', 'source': '—', 'identity': '—',
                      'bucket': '—', 'status': 'error',
                      'detail': f'S3 health check failed to run: {_short_err(e)}'}]

    return render(request, 'pages/admin_version_details.html',
                  {'details': details, 'env': env, 'git': git_result,
                   'django_settings': settings_result, 's3_status': s3_status})


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_sendemail(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    message_to_user = ""

    if request.method == "POST":
        form = SendEmailForm(request.POST)
        form_dict = form_to_dict(form)
        to = form_dict['to']
        cc = form_dict['cc']
        subject = form_dict['subject']
        body = form_dict['body']
        logging.debug(" FORM = " + str(form_dict))

        # add details for the template
        form_dict['SITE_TITLE'] = settings.SITE_TITLE
        form_dict['SITE_URL'] = settings.SITE_URL
        html_message = render_to_string('contacts/mail_template.html', form_dict)
        plain_message = strip_tags(html_message)

        email = EmailMessage(
            subject,
            html_message,
            settings.EMAIL_HOST_USER,
            [to],
            [cc],
            reply_to=[settings.EMAIL_HOST_USER]
        )
        email.content_subtype = "html"
        email.send(fail_silently=False)

        message_to_user = "Email sent"

    return render(request, 'pages/admin_sendemail.html',
                  {'message_to_user': message_to_user, 'user': request.user, 'SITE_TITLE': settings.SITE_TITLE})


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_stats(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    # Get all user data
    User = get_user_model()
    users = User.objects.all()

    # Get all projects
    all_projects = list(collection_handle.find(STATUS_QUERIES[LIVE]))

    # Create a dictionary to store user stats
    user_stats = {}

    # For each user, count their private and public projects where they are the only member
    for user in users:
        username = user.username
        email = user.email
        user_id = user.id

        # Initialize counts for this user
        solo_private_projects = 0
        solo_public_projects = 0

        # Check each project
        for project in all_projects:
            project_members = project.get('project_members', [])

            # Check if user is a member (by username or email) and they're the only one
            is_only_member = (len(project_members) == 1 and
                              (username in project_members or email in project_members))

            if is_only_member:
                if is_project_private(normalize_visibility_field(
                        project.get('private', 'private'))):
                    solo_private_projects += 1
                else:
                    solo_public_projects += 1

        # Store the stats for this user
        user_stats[user_id] = {
            'solo_private_projects': solo_private_projects,
            'solo_public_projects': solo_public_projects
        }

    # Get public project data for display
    public_projects, private_projects = _partition_admin_stats_projects(all_projects)

    for project in private_projects:
        project['sample_metadata_available'] = has_sample_metadata(project)

    for proj in public_projects:
        prepare_project_linkid(proj)
        if check_if_db_field_exists(proj, 'project_downloads'):
            project_download_data = proj['project_downloads']
            if isinstance(project_download_data, int):
                temp_data = project_download_data
                project_download_data = dict()
                project_download_data[get_date_short()] = temp_data
                proj_id = proj['_id']
                query = {'_id': ObjectId(proj_id)}
                new_val = {"$set": {'project_downloads': project_download_data}}
                collection_handle.update_one(query, new_val)
        if check_if_db_field_exists(proj, 'sample_downloads'):
            sample_download_data = proj['sample_downloads']
            if isinstance(sample_download_data, int):
                if sample_download_data > 0:
                    # Migration for sample downloads format
                    temp_data = sample_download_data
                    sample_download_data = dict()
                    sample_download_data[get_date_short()] = temp_data
                    proj_id = proj['_id']
                    query = {'_id': ObjectId(proj_id)}
                    new_val = {"$set": {'sample_downloads': sample_download_data}}
                    collection_handle.update_one(query, new_val)

    # Calculate stats.
    #
    # The counters are summed over the whole version chain, not read off the
    # current version. Downloads are attributed to whichever document served
    # them, and reaggregation starts the new version's tally empty, so the
    # current version holds only its own share -- 8% of the project's dated
    # download history on prod, measured 2026-08-29. The rest was on the older
    # versions the whole time.
    chain_counts = download_totals.chain_totals_for(collection_handle,
                                                    public_projects)
    for project in public_projects:
        project['sample_metadata_available'] = has_sample_metadata(project)
        totals = chain_counts[str(project['_id'])]
        project['project_downloads'] = totals['project_downloads']
        project['sample_downloads'] = totals['sample_downloads']
        project['sample_downloads_sum'] = sum(totals['sample_downloads'].values())

    repo_stats = get_latest_site_statistics()

    # Read, never measured here. The walk behind these numbers is seconds of
    # work over hundreds of thousands of fs.files rows, so it runs once a day
    # from cron and this page shows the newest snapshot and the trend.
    storage = storage_stats.latest()
    storage_history = storage_stats.history(days=90)
    storage_buckets = []
    if storage:
        for key, label in storage_stats.BUCKETS:
            bucket = storage.get('buckets', {}).get(key, {})
            storage_buckets.append({
                'key': key,
                'label': label,
                'files': bucket.get('files', 0),
                'bytes': bucket.get('bytes', 0),
                'documents': storage.get('documents', {}).get(key),
                'share': (100.0 * bucket.get('bytes', 0) / storage['bytes']
                          if storage.get('bytes') else 0),
            })

    return render(request, 'pages/admin_stats.html', {
        'public_projects': public_projects,
        'private_projects': private_projects,
        'users': users,
        'user_stats': user_stats,
        'site_stats': repo_stats,
        'storage': storage,
        'storage_buckets': storage_buckets,
        'storage_chart': storage_stats.sparkline(storage_history),
        'storage_history': storage_history,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def user_stats_download(request):
    # Get all user data
    User = get_user_model()
    users = User.objects.all()

    # Create the HttpResponse object with the appropriate CSV header.
    today = get_date_short()
    response = HttpResponse(
        content_type="text/csv",
    )
    response['Content-Disposition'] = f'attachment; filename="users_{today}.csv"'

    user_data = []
    for user in users:
        user_dict = {'username': user.username, 'email': user.email, 'date_joined': user.date_joined,
                     'last_login': user.last_login}
        user_data.append(user_dict)

    writer = csv.writer(response)
    keys = ['username', 'email', 'date_joined', 'last_login']
    writer.writerow(keys)
    for dictionary in user_data:
        output = {k: dictionary.get(k, None) for k in keys}
        writer.writerow(output.values())

    return response


# ─────────────────────────────────────────────────────────────────────
# Backups an administrator can take off AWS
# ─────────────────────────────────────────────────────────────────────

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_backups(request):
    """The two copies that can leave AWS, and how stale each one is.

    Every other backup this site has lands in the same AWS account as the data
    it protects, so an account-level event takes all of them.  These two are
    small enough to keep anywhere, which is the only reason the page exists.

    The staleness figure is a comparison of row and document counts against the
    totals recorded when the file was last downloaded.  That is deliberately
    modest: it catches anything created or removed, and it does *not* catch an
    edit that leaves the counts equal.  The page says so rather than implying a
    guarantee it cannot make.
    """
    from . import admin_backups as backups

    entries = []
    for kind, title, note in (
        (backups.SQLITE, 'Accounts database',
         'User accounts, email and OAuth links, and the CMS pages. This is the '
         'one piece of site data that is in no DocumentDB snapshot. Sessions '
         'are dropped from the copy; the live file is never touched.'),
        (backups.METADATA, 'Metadata dump',
         'Every collection except the GridFS payload: the whole catalogue of '
         'what exists, who owns it and what it is called. The payload itself '
         'stays behind and is covered by the cluster snapshots.'),
        (backups.URLS, 'Project URL manifest',
         'Every URL that resolves to a project today, and what it resolves to. '
         'Rebuilding from re-uploaded projects mints new ids, so this is what '
         'lets old links be re-pointed instead of simply breaking.'),
    ):
        try:
            totals = {backups.SQLITE: backups.sqlite_totals,
                      backups.METADATA: backups.metadata_totals,
                      backups.URLS: backups.url_totals}[kind]()
        except Exception:
            logging.exception('Could not read totals for the %s backup', kind)
            totals = {}
        previous = backups.last_download(kind)
        entries.append({
            'kind': kind,
            'title': title,
            'note': note,
            'totals': sorted(totals.items()),
            'total_rows': sum(totals.values()),
            'last': previous,
            'delta': backups.compare((previous or {}).get('totals'), totals),
        })

    return render(request, 'pages/admin_backups.html', {'entries': entries})


class _TempDirFileResponse(FileResponse):
    """A FileResponse that removes the directory it streamed from.

    The build writes into a scratch directory that must not outlive the
    response, and there is no later pass that would collect it: this is the
    only code that knows the path.  Cleanup hangs off close() because that is
    the one call the server makes whether the client read the whole body,
    disconnected half way, or errored.
    """

    def __init__(self, *args, cleanup_dir=None, **kwargs):
        self._cleanup_dir = cleanup_dir
        super().__init__(*args, **kwargs)

    def close(self):
        try:
            super().close()
        finally:
            if self._cleanup_dir and os.path.isdir(self._cleanup_dir):
                try:
                    shutil.rmtree(self._cleanup_dir)
                except Exception:
                    logging.exception('Could not remove backup scratch directory %s',
                                      self._cleanup_dir)


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_backup_download(request, kind):
    """Build one backup and stream it, recording that it was taken.

    Built on demand rather than served from a stored copy, because a stored
    copy is a third thing to keep fresh and the whole point of this page is
    that the fresh copy ends up on the administrator's own disk.
    """
    from . import admin_backups as backups

    if kind not in backups.KINDS:
        messages.error(request, 'Unknown backup type.')
        return redirect('admin_backups')

    workdir = backups.workspace()
    try:
        if kind == backups.SQLITE:
            path, digest = backups.build_sqlite(workdir)
            totals = backups.sqlite_totals()
        elif kind == backups.URLS:
            path, _count = backups.build_urls(workdir)
            digest = backups.sha256_of(path)
            totals = backups.url_totals()
        else:
            path, _manifest = backups.build_metadata(workdir)
            digest = backups.sha256_of(path)
            totals = backups.metadata_totals()
    except Exception:
        logging.exception('Could not build the %s backup', kind)
        shutil.rmtree(workdir, ignore_errors=True)
        messages.error(request, 'The backup could not be built. The server log '
                                'has the reason.')
        return redirect('admin_backups')

    size = os.path.getsize(path)
    try:
        backups.record_download(kind, request.user.username, totals, size, digest)
    except Exception:
        # A recorded download that did not happen is a mild annoyance; an
        # unrecorded one that did is how a backup's age becomes unknowable.
        # Neither is worth withholding the file the administrator asked for.
        logging.exception('Could not record the %s backup download', kind)

    response = _TempDirFileResponse(
        open(path, 'rb'), as_attachment=True,
        filename=os.path.basename(path), cleanup_dir=workdir)
    response['Content-Length'] = size
    response['X-Content-SHA256'] = digest
    return response


# wrapper in views for site stats to keep the import of site_stats.py out of urls.py
@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def site_stats_regenerate(request):
    regenerate_site_statistics()
    return admin_stats(request)


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def project_stats_download(request):
    # Get public and private project data
    public_projects = list(collection_handle.find(
        status_query(LIVE, private={'$in': PUBLIC_QUERY_VALUES})))
    # Summed over the version chain: this CSV is what the weekly report reads,
    # and its "all time" columns are built from these two dicts. Reading them
    # off the current version made "all time" mean "since the last
    # reaggregation" -- 8% of the real figure on prod, measured 2026-08-29.
    #
    # download_totals.as_dated also removes a crash that was waiting here: 12
    # project documents on prod still store project_downloads as a bare int,
    # and .values() on an int raises. None is public today, which is the only
    # reason this endpoint has not failed.
    chain_counts = download_totals.chain_totals_for(collection_handle,
                                                    public_projects)
    for project in public_projects:
        totals = chain_counts[str(project['_id'])]
        project['project_downloads'] = totals['project_downloads']
        project['sample_downloads'] = totals['sample_downloads']
        project['project_downloads_sum'] = sum(totals['project_downloads'].values())
        project['sample_downloads_sum'] = sum(totals['sample_downloads'].values())

    for proj in public_projects:
        prepare_project_linkid(proj)

    # Create the HttpResponse object with the appropriate CSV header.
    today = get_date_short()
    response = HttpResponse(
        content_type="text/csv",
    )
    response['Content-Disposition'] = f'attachment; filename="projects_{today}.csv"'

    writer = csv.writer(response)
    keys = ['project_name', 'description', 'project_members', 'date_created', 'project_downloads',
            'project_downloads_sum', 'sample_downloads', 'sample_downloads_sum']
    writer.writerow(keys)
    for dictionary in public_projects:
        output = {k: dictionary.get(k, None) for k in keys}
        writer.writerow(output.values())
    return response


PARTIAL_DELETE_MARKER = 'is partly deleted:'
"""How a caller recognises a delete that stopped at its deadline.

The message is the only channel: admin_permanent_delete_project() returns a
string, and several tests and the account-deletion path substitute callables
with exactly its three positional parameters, so widening the return type or
the signature would break them.  Asking the database instead would report every
substituted callable as partial, since those delete nothing.
"""

REQUEST_DELETE_DEADLINE = 780
"""Seconds of payload deleting one web request may spend, across all projects.

gunicorn kills a sync worker at 900 s (gunicorn_config.py), and until this
existed a single large project could not be deleted from the browser at all:
measured on prod 2026-09-01, a delete costs about 96.4 s per GiB plus 0.035 s
per file, which put the four biggest soft-deleted projects at 1,590-2,189 s
each.  The worker died partway through, and because the payload loop had no
notion of stopping, whatever it had reached was already gone.

780 leaves 120 s for the response to render and for whatever the deletes were
waiting on to unwind.  Nothing is lost by stopping: see
delete_payload_within_deadline() for why a half-deleted payload is a resumable
state rather than a leak.
"""

_delete_deadline = threading.local()
"""Request-scoped, so it must not be a module global.

A thread-local is right for the sync worker class this runs under -- one
request per thread, no overlap -- and it keeps the deadline out of
admin_permanent_delete_project()'s signature, which several callers and the
account-deletion path pass positionally.
"""


@contextlib.contextmanager
def delete_deadline(seconds, now=time.monotonic):
    """Give every delete inside this block a shared stopping time."""
    previous = getattr(_delete_deadline, 'value', None)
    _delete_deadline.value = now() + seconds
    try:
        yield
    finally:
        _delete_deadline.value = previous


def current_delete_deadline():
    """The active deadline, or None when deleting outside a request."""
    return getattr(_delete_deadline, 'value', None)


def admin_permanent_delete_project(project_id, project, project_name):
    """
    This function permanently deletes a project from s3 and from the server.

    Stops at the deadline set by delete_deadline() if one is active, keeping the
    S3 object and the project document so the remaining payload stays reachable.
    Outside a request there is no deadline and the whole payload goes.
    """
    error_message = ""
    query = {'_id': ObjectId(project_id)}

    # drop any cached co-amplification graph built from this project
    from .views import invalidate_project_coamp_graphs
    invalidate_project_coamp_graphs(project_id)

    # Delete the payload through the application's own walk, not a list kept
    # here.  This used to iterate project['runs'] one level deep against eight
    # hardcoded field names.  GRIDFS_FILE_KEYS holds 35, and the difference is
    # not academic: measured on prod 2026-08-31 across the 72 SOFT_DELETED and
    # TOMBSTONE documents, the canonical walk finds 133,655 files and the old
    # eight-key walk found 17,175 -- so a hard delete removed 13% of the payload
    # and stranded **116,480 files, 42.04 GiB**, with no document left pointing
    # at them afterwards.
    #
    # Almost all of it was spelling.  Prod documents overwhelmingly use the
    # underscore forms -- Sample_metadata_JSON, AA_directory, CNV_BED_file,
    # cnvkit_directory -- and the list here had only the space-separated ones.
    # A second key list is the recurring defect in this repository and this is
    # what it costs; test_admin_delete_uses_the_canonical_key_list is the guard.
    try:
        outcome = delete_payload_within_deadline(
            delete_gridfs_file, project, deadline=current_delete_deadline())
    except Exception:
        logging.exception('Problem deleting project files from Mongo.')
        error_message = "Problem deleting project files from Mongo."
    else:
        if outcome.remaining:
            # Stop here, before S3 and before the document.  Both are records
            # that the project existed; removing either now would leave the
            # remaining files named by nothing, which is the exact way the
            # existing orphan population was created.  Keeping them makes the
            # next attempt a continuation.
            return (
                f"Project {project_name} {PARTIAL_DELETE_MARKER} "
                f"{outcome.deleted} file(s) removed, {len(outcome.remaining)} still "
                f"to go. It is still listed as deleted -- select it and submit again "
                f"to continue where this left off."
            )

    # Only one of these exists, and usually neither: the extracted upload is a
    # scratch copy that the temp-dir reaper has normally already taken. Picking a
    # path without checking it made every clean delete log a FileNotFoundError
    # traceback at ERROR level, so the delete path looked like it was failing
    # each time it succeeded.
    project_data_path = next(
        (path for path in (f"../tmp/{project_id}/", f"tmp/{project_id}/")
         if os.path.isdir(path)), None)
    if project_data_path is not None:
        try:
            shutil.rmtree(project_data_path)
        except Exception:
            logging.exception('Problem deleting the extracted upload directory %s',
                              project_data_path)

    if hasattr(settings, 'S3_DOWNLOADS_BUCKET_PATH'):
        print("============= HAS ATTR  ================")
        s3_file_path = f'{settings.S3_DOWNLOADS_BUCKET_PATH}{project_id}/{project_id}.tar.gz'
        try:
            session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
            s3client = session.client('s3')
            s3client.delete_object(Bucket=settings.S3_DOWNLOADS_BUCKET, Key=s3_file_path)
        except:
            logging.exception(f'Problem deleting tar file from S3. {s3_file_path}')
            error_message = error_message + " Problem deleting tar file from S3. "
    else:

        error_message = error_message + " No S3 bucket path set. No attempt made to delete the tar file from S3. "

    # Final step, delete the project
    try:
        collection_handle.delete_one(query)
    except:
        logging.exception('Problem deleting Project document from Mongo.')
        error_message = error_message + " Problem deleting Project document from Mongo. "

    if error_message:
        error_message = error_message + " Other project artifacts successfully deleted. Please refer to the application log files for details. "
    else:
        error_message = f"Project {project_name} deleted."

    return error_message


def _lookup_account(username):
    """The Django user behind a username, or None."""
    return User.objects.filter(username=username).first()


def _preview_projects(user, username):
    """Split the account's live projects for the confirmation table.

    Same walk as the deletion itself -- plan_account_deletion is what decides,
    here and in caper.account_signals -- so the table cannot show one outcome
    and the deletion produce another.

    The username is passed separately from the user because the page is also
    asked about names that do not resolve to an account, and a project can still
    name one of those in its member list.
    """
    solo, shared = [], []
    plan = plan_account_deletion(username, getattr(user, 'email', None))
    for project, action, _visibility in plan:
        project['visibility_display'] = format_visibility_for_display(
            project.get('private', True))
        project['deletion_action'] = action
        (shared if action == RELEASE else solo).append(project)
    return solo, shared


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_delete_user(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    error_message = ""
    solo_projects = []  # Initialize with a default value
    member_projects = []  # Initialize with a default value
    username = ""

    if request.method == "POST":
        username = request.POST.get("user_name", "")
        action = request.POST.get("action", "select_user")

        if action == 'select_user':
            solo_projects, member_projects = _preview_projects(
                _lookup_account(username), username)

        elif action == 'delete_user':
            user = _lookup_account(username)

            # Everything that happens to the projects is done by the post_delete
            # receiver in account_signals.py: solo projects deleted or reassigned,
            # membership of shared projects dropped, subscriber lists and
            # notification preferences cleared, and the CASCADE on the auth token
            # taking the API credentials with it. Doing it there rather than here
            # is what makes Django's own /admin/ and the shell behave the same as
            # this page. Historical project versions and the audit log keep their
            # record of who submitted what.
            if user is not None:
                user.delete()
                report = getattr(user, '_amprepo_deletion_report', None)
                error_message += f"User {username} deleted successfully. "
                if report:
                    error_message += summarize(report)
            else:
                error_message += f"User {username} does not exist."

            solo_projects = []
            member_projects = []

    return render(request, 'pages/admin_delete_user.html',
                  {'username': username,
                   'solo_projects': solo_projects,
                   'member_projects': member_projects,
                   'error_message': error_message})


def _tombstoned_ancestors(project):
    """(id, document) for each tombstoned version before *project* in its chain.

    Empty for an unpointered document, and empty for a chain that has never had
    a version deleted -- which is every chain that predates the pointer writes.
    """
    members = lineage.chain_members(collection_handle, project,
                                    lineage.POINTER_PROJECTION)
    if members is None:
        return []

    named = {str(entry.get('linkid'))
             for entry, _encoding in iter_previous_versions(project)}
    found = []
    for member in lineage.ancestors(members, project):
        member_id = str(member['_id'])
        if member_id in named:
            continue                      # the loop above already has it
        document = get_one_deleted_project(member_id)
        if document is not None and is_tombstone(document):
            found.append((member_id, document))
    return found


def permanently_delete_with_history(project_id, project, project_name):
    """
    Permanently delete a soft-deleted project along with the older versions its
    history names, and return a message describing what happened.

    A history entry whose target does not resolve is skipped and reported
    rather than raising.  get_one_deleted_project() returns None both for a
    reference to a document that is no longer in the collection and for one
    that is not flagged deleted; dereferencing that None used to raise
    TypeError before anything was deleted, which made a project with a single
    dangling reference impossible to delete from this page at all.

    The history walked here is the project's own ``previous_versions`` field,
    read through the resolver.  This used to call previous_versions(), which
    builds the table the project page renders: that list always ends with an
    entry for the version being viewed, and when the project is not the head of
    its chain it describes the *head's* history instead, head included.  So the
    loop deleted this project's payload once inside it and again below, and for
    an older version would have reached across the chain to versions nobody
    selected.  The stored field names ancestors and nothing else.

    Tombstoned ancestors are added from the chain, because the array cannot
    name them: deleting a version removes it from every ``previous_versions[]``
    while it stays in the chain holding its ordinal.  Left behind, such a
    document would outlive the project it belonged to as a tombstone
    redirecting at an id that no longer exists.  Only ancestors are taken --
    strictly lower ordinal -- so this still never reaches forward across the
    chain to versions nobody selected.
    """
    unresolved = []
    for entry, _encoding in iter_previous_versions(project):
        linkid = entry.get('linkid')
        older = get_one_deleted_project(linkid) if linkid else None
        if older is None:
            logging.warning(
                f"History entry {linkid!r} of project {project_id} names a version "
                f"that is gone or is not flagged deleted; skipping it.")
            unresolved.append(linkid)
            continue
        admin_permanent_delete_project(linkid, older, older['project_name'])

    for tombstone_id, tombstone in _tombstoned_ancestors(project):
        admin_permanent_delete_project(tombstone_id, tombstone,
                                       tombstone.get('project_name'))

    message = admin_permanent_delete_project(project_id, project, project_name)

    if unresolved:
        plural = 'y' if len(unresolved) == 1 else 'ies'
        message = (f"{message} Skipped {len(unresolved)} history entr{plural} naming a "
                   f"version that no longer exists: {', '.join(str(u) for u in unresolved)}.")
    return message


BULK_DELETE_TIME_BUDGET = 600
"""Seconds after which delete_selected_projects() stops starting new projects.

gunicorn kills a worker at 900 seconds (gunicorn_config.py), and a selection
large enough to reach that would take the report down with it: the deletes that
had already run would be invisible, leaving the operator to work out by hand
which rows are gone.  Stopping at 600 leaves room for the project already in
flight to finish and for the page to render.  Nothing is lost by stopping --
each project is deleted completely before the next one starts -- so selecting
what remains and submitting again continues from here.
"""


def delete_selected_projects(project_ids, time_budget=BULK_DELETE_TIME_BUDGET,
                             user=None):
    """
    Permanently delete each project in *project_ids*, and return a message.

    One project failing does not abandon the rest: a batch is only useful if a
    single bad document costs one row rather than the whole selection, and the
    caller cannot tell in advance which documents carry a defect.

    Every delete writes a provenance event before it happens, the same as the
    single-project path. This path did not, and it is the path that does the
    deleting: on prod, 71 projects were permanently deleted through it and the
    audit log holds 5 permanent_delete events, all from the single-project
    button. Measured 2026-09-02. Phase 3 exists because deletion used to write
    nothing at all, and the bulk button was still in that state.
    """
    started = time.monotonic()
    deleted = 0
    problems = []
    partial = []
    unattempted = 0
    # One deadline for the whole request, not one per project: the 900 s
    # gunicorn ceiling is per request, so a budget that only decides when to
    # *start* work cannot enforce it.  Before this, a project begun at 599 s
    # that ran 512 s reached 1,111 s and the worker was killed mid-delete.
    with delete_deadline(REQUEST_DELETE_DEADLINE):
        for index, project_id in enumerate(project_ids):
            # Checked before the work, never before the first project: a single
            # project always gets its attempt, however long the batch may run.
            if index and time.monotonic() - started > time_budget:
                unattempted = len(project_ids) - index
                break

            project = get_one_deleted_project(project_id)
            if project is None:
                problems.append(f"{project_id} (not found, or no longer flagged deleted)")
                continue
            name = project.get('project_name', project_id)
            # Recorded before the work, so a delete that dies half way leaves a
            # trace: afterwards there is no document left to ask what happened
            # to it, which is the whole reason this event exists.
            purge_event = provenance.record(
                audit_log_handle, provenance.PERMANENT_DELETE, user, project,
                intended={'status': None, 'documents_removed': True})
            try:
                outcome = permanently_delete_with_history(project_id, project, name)
            except Exception:
                logging.exception(f"Permanent delete failed for project {project_id} ({name}).")
                provenance.confirm(audit_log_handle, purge_event,
                                   outcome='failed',
                                   error_message='exception during delete')
                problems.append(name)
                continue
            if outcome and PARTIAL_DELETE_MARKER in str(outcome):
                provenance.confirm(audit_log_handle, purge_event,
                                   outcome='failed', error_message=str(outcome))
                partial.append(name)
                unattempted = len(project_ids) - index - 1
                break
            provenance.confirm(audit_log_handle, purge_event,
                               outcome='failed' if outcome else 'removed',
                               error_message=str(outcome) if outcome else None)
            deleted += 1

    message = f"Permanently deleted {deleted} of {len(project_ids)} selected project(s)."
    if partial:
        message = (f"{message} Ran out of time partway through "
                   f"{', '.join(partial)}; it is still listed as deleted and keeps "
                   f"the files it has left, so submitting it again continues from "
                   f"where this stopped.")
    if problems:
        # 'Problem' is the word the page's script looks for to colour the
        # banner as a warning rather than a success.
        message = f"{message} Problem deleting: {'; '.join(problems)}."
    if unattempted:
        message = (f"{message} Stopped after {int(time.monotonic() - started)} seconds with "
                   f"{unattempted} project(s) not attempted; select them and submit again "
                   f"to continue.")
    return message


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_delete_project(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    error_message = ""
    if request.method == "POST" and request.POST.get('action') == 'delete-selected':
        # Read straight from POST: DeletedProjectForm describes a single
        # project, and this action carries a list instead.
        error_message = delete_selected_projects(
            request.POST.getlist('project_ids'), user=request.user)

    elif request.method == "POST":
        form = DeletedProjectForm(request.POST)
        form_dict = form_to_dict(form)
        project_name = form_dict['project_name']
        project_id = form_dict['project_id']
        deleteit = form_dict['delete']
        logging.debug(" FORM = " + str(form_dict))
        action = form_dict['action']

        if action == 'un-delete':
            # remove the delete flag, this project goes back
            project = get_one_deleted_project(project_id)
            query = {'_id': ObjectId(project_id)}
            # Clears the delete flag and nothing else, which is what makes
            # this the reverse of a soft delete: the document keeps whatever
            # 'current' it had.  Only SOFT_DELETED documents are listed below,
            # so in practice that lands on LIVE.
            intended_status = status_after(current_flags(project), delete=False)
            restore_event = provenance.record(
                audit_log_handle, provenance.RESTORE_PROJECT, request.user,
                project, intended={'status': intended_status})
            new_val = {"$set": {'delete': status_flags(LIVE)['delete'],
                                'status': intended_status}}
            collection_handle.update_one(query, new_val)
            provenance.confirm(audit_log_handle, restore_event)
            error_message = f"Project {project_name} restored."

        elif deleteit and (action == 'delete'):

            ## deletes the project and all previous versions its history names.
            project = get_one_deleted_project(project_id)
            # The only irreversible path in the application, and the one whose
            # absence from the log was felt most in August 2026: afterwards
            # there is no document left to ask what happened to it.
            purge_event = provenance.record(
                audit_log_handle, provenance.PERMANENT_DELETE, request.user,
                project, intended={'status': None, 'documents_removed': True})
            # Same ceiling applies to a single delete: the four largest projects
            # on prod each need more than the 900 s a worker gets.
            with delete_deadline(REQUEST_DELETE_DEADLINE):
                error_message = permanently_delete_with_history(
                    project_id, project, project_name)
            provenance.confirm(audit_log_handle, purge_event,
                               outcome='failed' if error_message else 'removed',
                               error_message=error_message or None)

    deleted_projects = list(collection_handle.find(STATUS_QUERIES[SOFT_DELETED]))
    for proj in deleted_projects:
        prepare_project_linkid(proj)
        try:
            tar_file_len = fs_handle.get(ObjectId(proj['tarfile'])).length
            proj['tar_file_len'] = sizeof_fmt(tar_file_len)
            if proj['delete_date']:
                import datetime
                dt = datetime.datetime.strptime(proj['delete_date'], f"%Y-%m-%dT%H:%M:%S")
                proj['delete_date'] = (dt.strftime(f'%B %d, %Y %I:%M:%S %p %Z'))
        except:
            # ignore missing date
            logging.warning(proj['project_name'] + " missing date")

    return render(request, 'pages/admin_delete_project.html',
                  {'deleted_projects': deleted_projects, 'error_message': error_message})


def sizeof_fmt(num, suffix="B"):
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def fix_schema(request):
    apply_changes = request.POST.get("action") == "apply"
    try:
        from caper.schema_validate import run_fix_schema

        schema_path = os.path.join(settings.BASE_DIR, "schema", "schema.json")

        fix_schema_report = run_fix_schema(
            db_host=None,  # Use the existing connection from utils
            collection_name="projects",
            schema_path=schema_path,
            apply_changes=apply_changes,
        )

    except Exception as e:
        fix_schema_report = f"Error running fix schema: {str(e)}"

    return render(request, "pages/admin_fix_schema_report.html", {
        'fix_schema_report': fix_schema_report,
        'can_apply': not apply_changes,
        'repair_applied': apply_changes,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def data_qc(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email

        private_projects = list(collection_handle.find(status_query(
            LIVE,
            private={'$in': RESTRICTED_QUERY_VALUES},
            **{"$or": [{"project_members": username}, {"project_members": useremail}]})))
        for proj in private_projects:
            prepare_project_linkid(proj)
            proj['visibility_display'] = format_visibility_for_display(proj.get('private', True))
    else:
        private_projects = []

    public_proj_count = 0
    public_sample_count = 0

    public_projects = list(collection_handle.find(
        status_query(LIVE, private={'$in': PUBLIC_QUERY_VALUES})))
    for proj in public_projects:
        prepare_project_linkid(proj)
        proj['visibility_display'] = format_visibility_for_display(proj.get('private', True))
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    datetime_status = check_datetime(public_projects) + check_datetime(private_projects)
    sample_count_status = check_sample_count_status(private_projects) + check_sample_count_status(public_projects)

    # Check for orphaned old versions (delete=False, current=False, but no other versions)
    all_projects = list(collection_handle.find({}))
    orphaned_projects = []
    
    for project in all_projects:
        # DETACHED is the status for a document whose meaning the schema cannot
        # determine, and is_reachable_by_url() narrows that to the ones the
        # application will still serve -- which is the pair this page was
        # hand-testing as delete=False AND current=False.  Same population on
        # prod, where every document carries a 'delete' field; the difference
        # is only a DETACHED document with no 'delete' field at all, which the
        # old test would have missed and this one reports.
        if classify(project) == DETACHED and is_reachable_by_url(project):
            project_id = str(project.get('_id', 'NO_ID'))
            
            # Orphaned means "alone in its chain": no ancestors, and nothing
            # names it as one. With pointers that is the size of the chain, one
            # indexed equality match.
            #
            # The array form below is kept for documents the backfill has not
            # reached, and it is the weaker test of the two. Its reverse lookup
            # matches on previous_versions.linkid, so a reference stored in the
            # pre-April 2024 encoding -- a JSON string, with no linkid key --
            # matches nothing, and a document that is genuinely part of a chain
            # gets reported here as an orphan. That is the same blind spot as
            # check_project_flags.py:195.
            members = lineage.chain_members(collection_handle, project,
                                            lineage.POINTER_PROJECTION)
            if members is not None:
                alone = len(members) == 1
            else:
                has_previous = len(project.get('previous_versions', [])) > 0
                is_referenced = collection_handle.count_documents({
                    'previous_versions.linkid': project_id
                }) > 0
                alone = not has_previous and not is_referenced

            if alone:
                # No other versions found - this is orphaned!
                prepare_project_linkid(project)
                project['visibility_display'] = format_visibility_for_display(project.get('private', True))
                orphaned_projects.append(project)

    # Run the schema validation directly
    try:
        from caper.schema_validate import run_validation

        schema_path = os.path.join(settings.BASE_DIR, "schema", "schema.json")

        # Run the validation and get the report
        schema_report = run_validation(
            db_host=None,  # Use the existing connection from utils
            collection_name="projects",
            schema_path=schema_path
        )

    except Exception as e:
        schema_report = f"Error running schema validation: {str(e)}"

    return render(request, "pages/admin_quality_check.html", {
        'public_projects': public_projects,
        'private_projects': private_projects,
        'datetime_status': datetime_status,
        'sample_count_status': sample_count_status,
        'orphaned_projects': orphaned_projects,
        'schema_report': schema_report,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def make_project_current(request, project_id):
    """Set a project's current flag to True and regenerate site statistics"""
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    
    if request.method == "POST":
        from bson.objectid import ObjectId
        from .site_stats import add_project_to_site_statistics
        from .utils import normalize_visibility_field

        try:
            # Get the project first to check its privacy setting
            project = collection_handle.find_one({'_id': ObjectId(project_id)})
            
            if not project:
                messages.error(request, f"Project {project_id} not found")
                return redirect('data_qc')
            
            # Update the project to set current=True
            result = collection_handle.update_one(
                {'_id': ObjectId(project_id)},
                # Half of the flag pair on purpose: this admin repair marks a
                # document as the head of its chain without touching 'delete'.
                # The resulting status therefore depends on the 'delete' already
                # stored, which is what status_after() reads.
                {'$set': {'current': status_flags(LIVE)['current'],
                          'status': status_after(current_flags(project),
                                                 current=True)}}
            )
            
            if result.modified_count > 0:
                add_project_to_site_statistics(
                    project, normalize_visibility_field(project.get('private', 'private')))
                
                messages.success(request, f"Project {project_id} has been set to current=True and added to site statistics")
            else:
                messages.warning(request, f"Project {project_id} was not modified (may already be current)")
        except Exception as e:
            messages.error(request, f"Error updating project: {str(e)}")
    
    # Redirect back to the data QC page
    return redirect('data_qc')


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_prepare_shutdown(request):
    """Toggle shutdown pending mode and registration disabled mode"""
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    
    from .context_processor import (
        get_shutdown_pending, set_shutdown_pending,
        get_registration_disabled, set_registration_disabled
    )
    
    if request.method == "POST":
        action = request.POST.get('action')
        
        if action == 'toggle_shutdown':
            # Toggle the shutdown mode
            current_status = get_shutdown_pending()
            set_shutdown_pending(not current_status)
        elif action == 'toggle_registration':
            # Toggle the registration disabled mode
            current_status = get_registration_disabled()
            set_registration_disabled(not current_status)
        
        return redirect('/admin-prepare-shutdown')
    
    # Get current status for both flags
    shutdown_status = get_shutdown_pending()
    registration_disabled_status = get_registration_disabled()

    # Get background task status for display at the top of the page
    task_status = get_background_task_status()

    return render(request, 'pages/admin_settings.html', {
        'shutdown_pending': shutdown_status,
        'registration_disabled': registration_disabled_status,
        'user': request.user,
        'task_status': task_status,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_project_files_report(request):
    """Generate a report on project files both on server and S3"""
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    audit_ctx = _get_audit_log_context(request)

    logger = logging.getLogger(__name__)
    
    # Get collection for saved reports
    saved_reports_collection = db_handle_primary['project_files_reports']
    
    # Handle POST requests (delete only - save is now automatic)
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'delete':
            # Delete a saved report
            report_id = request.POST.get('report_id')
            if report_id:
                saved_reports_collection.delete_one({'_id': ObjectId(report_id)})
                logger.info(f"Deleted saved report {report_id}")
            return redirect('/admin-project-files-report/')
    
    # Check if we're loading a saved report
    load_report_id = request.GET.get('load_report')
    if load_report_id:
        saved_report = saved_reports_collection.find_one({'_id': ObjectId(load_report_id)})
        if saved_report:
            # Get list of all saved reports
            all_saved_reports = list(saved_reports_collection.find().sort('created_at', -1))
            
            # Convert _id to string for Django templates (can't access underscore-prefixed attributes)
            for report in all_saved_reports:
                report['id_str'] = str(report['_id'])
            
            # Convert project_reports to JSON for JavaScript
            import json
            project_reports_json = json.dumps(saved_report['project_reports'])
            
            return render(request, 'pages/admin_project_files_report.html', {
                'project_reports': saved_report['project_reports'],
                'project_reports_json': project_reports_json,
                's3_enabled': saved_report['s3_enabled'],
                'projects_with_local_tar': saved_report['projects_with_local_tar'],
                'projects_with_s3_tar': saved_report['projects_with_s3_tar'],
                'file_pattern': saved_report['file_pattern'],
                'user': request.user,
                'SITE_TITLE': settings.SITE_TITLE,
                'saved_reports': all_saved_reports,
                'is_loaded_report': True,
                'loaded_report_date': saved_report['created_at'],
                **audit_ctx,
            })
    
    # Check if file_pattern parameter is provided
    file_pattern = request.GET.get('file_pattern', '').strip()
    
    # Get list of all saved reports
    all_saved_reports = list(saved_reports_collection.find().sort('created_at', -1))
    
    # Convert _id to string for Django templates (can't access underscore-prefixed attributes)
    for report in all_saved_reports:
        report['id_str'] = str(report['_id'])
    
    # If no file_pattern provided, just show the empty form
    if not file_pattern:
        s3_enabled = hasattr(settings, 'USE_S3_DOWNLOADS') and settings.USE_S3_DOWNLOADS
        return render(request, 'pages/admin_project_files_report.html', {
            'project_reports': [],
            'project_reports_json': '[]',
            's3_enabled': s3_enabled,
            'projects_with_local_tar': 0,
            'projects_with_s3_tar': 0,
            'file_pattern': '',
            'user': request.user,
            'SITE_TITLE': settings.SITE_TITLE,
            'saved_reports': all_saved_reports,
            **audit_ctx,
        })
    
    logger.info("Generating project files report...")
    logger.info(f"Searching for files matching pattern: {file_pattern}")
    
    # Get all projects (public and private)
    all_projects = list(collection_handle.find(STATUS_QUERIES[LIVE]))
    
    # Initialize S3 client if S3 downloads are enabled
    s3_client = None
    s3_enabled = hasattr(settings, 'USE_S3_DOWNLOADS') and settings.USE_S3_DOWNLOADS
    
    if s3_enabled:
        try:
            session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
            s3_client = session.client('s3')
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            s3_enabled = False
    
    project_reports = []
    
    for project in all_projects:
        project_id = str(project['_id'])
        project_name = project.get('project_name', 'Unknown')
        # `project.get('private', True)` was truthy for every value the field
        # actually holds, including 'public', so this report badged every
        # project Private.  Resolve it to the visibility string and let the
        # template read the label rather than re-deciding what counts as public.
        visibility = normalize_visibility_field(project.get('private', 'private'))

        # Initialize report data for this project
        report = {
            'project_id': project_id,
            'project_name': project_name,
            'is_private': is_project_private(visibility),
            'visibility_display': format_visibility_for_display(visibility),
            'has_local_tar': False,
            'has_s3_tar': False,
            'local_ecDNA_files': [],
            's3_ecDNA_files': [],
            'gridfs_ecDNA_files': []
        }
        
        # Check for local tar file
        local_tar_path = None
        if os.path.exists(f"../tmp/{project_id}/"):
            local_tar_path = f"../tmp/{project_id}/{project_id}.tar.gz"
        else:
            local_tar_path = f"tmp/{project_id}/{project_id}.tar.gz"
        
        if os.path.exists(local_tar_path):
            report['has_local_tar'] = True
        
        # Check for S3 tar file
        if s3_enabled:
            s3_tar_key = f'{settings.S3_DOWNLOADS_BUCKET_PATH}{project_id}/{project_id}.tar.gz'
            try:
                s3_client.head_object(Bucket=settings.S3_DOWNLOADS_BUCKET, Key=s3_tar_key)
                report['has_s3_tar'] = True
            except:
                report['has_s3_tar'] = False
        
        # Check for local ecDNA context files
        local_project_path = None
        if os.path.exists(f"../tmp/{project_id}/"):
            local_project_path = f"../tmp/{project_id}/"
        else:
            local_project_path = f"tmp/{project_id}/"
        
        if os.path.exists(local_project_path):
            for root, dirs, files in os.walk(local_project_path):
                for file in files:
                    if file_pattern in file:
                        abs_path = os.path.abspath(os.path.join(root, file))
                        report['local_ecDNA_files'].append(abs_path)
        
        # Check for S3 ecDNA context files
        if s3_enabled:
            s3_project_prefix = f'{settings.S3_DOWNLOADS_BUCKET_PATH}{project_id}/'
            try:
                paginator = s3_client.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=settings.S3_DOWNLOADS_BUCKET, Prefix=s3_project_prefix):
                    if 'Contents' in page:
                        for obj in page['Contents']:
                            key = obj['Key']
                            if file_pattern in key:
                                # Get relative path (remove project prefix)
                                relative_key = key[len(s3_project_prefix):]
                                report['s3_ecDNA_files'].append(relative_key)
            except Exception as e:
                logger.error(f"Failed to list S3 objects for project {project_id}: {e}")
        
        # Check for ecDNA context files in GridFS tar
        if 'tarfile' in project:
            try:
                tar_contents = list_project_tar_contents(project_id)
                # Filter for files containing the pattern
                ecDNA_files = [f for f in tar_contents if file_pattern in f]
                report['gridfs_ecDNA_files'] = ecDNA_files
            except Exception as e:
                logger.error(f"Failed to list GridFS tar contents for project {project_id}: {e}")
        
        project_reports.append(report)
    
    # Calculate summary statistics
    projects_with_local_tar = sum(1 for r in project_reports if r['has_local_tar'])
    projects_with_s3_tar = sum(1 for r in project_reports if r['has_s3_tar']) if s3_enabled else 0
    
    logger.info(f"Generated report for {len(project_reports)} projects")
    
    # Automatically save the report
    import json
    saved_report = {
        'file_pattern': file_pattern,
        'created_at': timezone.now(),
        'created_by': request.user.username,
        'project_reports': project_reports,
        's3_enabled': s3_enabled,
        'projects_with_local_tar': projects_with_local_tar,
        'projects_with_s3_tar': projects_with_s3_tar,
        'total_projects': len(project_reports)
    }
    
    saved_reports_collection.insert_one(saved_report)
    logger.info(f"Auto-saved report for pattern '{file_pattern}' by {request.user.username}")
    
    # Refresh the saved reports list to include the newly saved report
    all_saved_reports = list(saved_reports_collection.find().sort('created_at', -1))
    for report in all_saved_reports:
        report['id_str'] = str(report['_id'])
    
    # Convert project_reports to JSON for JavaScript
    project_reports_json = json.dumps(project_reports)
    
    return render(request, 'pages/admin_project_files_report.html', {
        'project_reports': project_reports,
        'project_reports_json': project_reports_json,
        's3_enabled': s3_enabled,
        'projects_with_local_tar': projects_with_local_tar,
        'projects_with_s3_tar': projects_with_s3_tar,
        'file_pattern': file_pattern,
        'user': request.user,
        'SITE_TITLE': settings.SITE_TITLE,
        'saved_reports': all_saved_reports,
        'is_loaded_report': False,
        'report_auto_saved': True,
        **audit_ctx,
    })


#: The only project fields the audit table renders or compares.
#
# Without a projection this query pulls every live project document whole --
# `runs`, `sample_data` and `aggregate_df` included -- to display six values per
# row. Measured on prod 2026-08-29 from the gunicorn access log, the two pages
# that call this helper were the two slowest URLs on the site by an order of
# magnitude: /admin-project-files-report/ averaged 157s and
# /admin-file-ownership/ 91s, against 3.5s for the next admin page.
#
# `project_name_unique` is read by the template and is not written by the
# upload path; it is listed so that a document which does have it keeps it.
_AUDIT_PROJECT_FIELDS = {
    '_id': 1, 'project_name': 1, 'project_name_unique': 1, 'private': 1,
    'AA_version': 1, 'AC_version': 1, 'ASP_version': 1, 'sample_count': 1,
}


#: Selects the newest audit entry that describes a stored payload.
#
# `_run_audit_checks` compares an entry's AA/AC/ASP versions, sample count and
# S3 size against the live document. A deletion event carries none of those --
# it records a lifecycle change, not a payload -- so letting one be the "latest
# entry" makes every field read as missing and downgrades a healthy project to
# "Partial". That is what happened on dev on 2026-08-30: a promote_version
# event was the only entry for a project, and the page reported Partial for a
# project with nothing wrong with it.
#
# $nin also matches documents with no event_type at all, which is what the
# oldest entries look like -- they predate the constants.
#
# A backfilled event is excluded for the same reason and it is the same defect:
# it was reconstructed from the project document's date and creator, so it has
# no s3_uri and no recorded file size, and letting one be the latest entry makes
# the size check read as missing. Measured on prod 2026-08-30, the hour the
# backfill ran: it took the projects with a payload-describing entry from 116 to
# 311, and every one of the 195 it added carried no s3_uri, so all 195 rows
# downgraded to "Partial". Those events exist to fill the history table, which
# is where a reconstructed creation belongs; they are not a record of a payload
# and must not be compared to one. A project with no real entry should say so.
_PAYLOAD_DESCRIBING_ENTRY = {
    'event_type': {'$nin': list(provenance.DELETION_EVENTS)},
    'backfilled': {'$ne': True},
}


def _latest_payload_entry(matched_uuids):
    """The newest entry that can meaningfully be compared to a live document."""
    if not matched_uuids:
        return None
    return audit_log_handle.find_one(
        {'project_uuid': {'$in': matched_uuids}, **_PAYLOAD_DESCRIBING_ENTRY},
        sort=[('timestamp', -1)])


def _latest_entry_of_any_kind(matched_uuids):
    """The newest entry of any type, for "when did this last change?"."""
    if not matched_uuids:
        return None
    return audit_log_handle.find_one({'project_uuid': {'$in': matched_uuids}},
                                     sort=[('timestamp', -1)])


def _has_observed_entry(matched_uuids):
    """Whether anything was recorded when it happened, rather than rebuilt after.

    Asked only of projects with no payload-describing entry, to tell two states
    apart that otherwise share a badge. A project whose surviving entry is a
    deletion *was* watched -- something recorded the event as it ran. A project
    whose only entry is a reconstructed creation was not: that entry was read
    off the project document after the fact, and carries no more than the
    document already says.
    """
    if not matched_uuids:
        return False
    return audit_log_handle.find_one(
        {'project_uuid': {'$in': matched_uuids},
         'backfilled': {'$ne': True}}) is not None


def _get_audit_log_context(request):
    """
    Build the audit log context dict shared by admin_project_files_report and admin_audit_log.
    """
    all_projects = list(collection_handle.find(STATUS_QUERIES[LIVE],
                                               _AUDIT_PROJECT_FIELDS))
    for proj in all_projects:
        prepare_project_linkid(proj)
        proj['id_str'] = str(proj['_id'])
        # Resolved here so the template does not have to spell out its own
        # "what counts as public" test against the raw field.
        proj['visibility_display'] = format_visibility_for_display(
            proj.get('private', 'private'))

    # Sort projects by name for easy browsing
    all_projects.sort(key=lambda p: (p.get('project_name') or '').lower())

    # For each project, find its most recent audit-log entry and run validation checks
    for proj in all_projects:
        search_term = proj.get('project_name') or proj['id_str']
        try:
            matched_uuids, _ = get_project_version_chain(search_term)

            # Two different questions, which used to share one answer.
            # "When did this project last change?" is about every event,
            # deletions included -- a deletion is a change, and hiding it in
            # this column would be a worse answer than the one it replaced.
            # "Does the log agree with the document?" can only be asked of an
            # entry that describes a payload.
            latest_entry = _latest_payload_entry(matched_uuids)
            latest_any = _latest_entry_of_any_kind(matched_uuids)

            if latest_entry:
                result = _run_audit_checks(proj, latest_entry)
                proj['validation_status'] = result['status']  # 'pass' | 'mismatch' | 'missing_data'
            elif latest_any is None:
                proj['validation_status'] = 'no_log'
            elif _has_observed_entry(matched_uuids):
                # A project whose only events are deletions is not unaudited;
                # it is audited and has nothing to compare. Those are different
                # states and the template distinguishes them.
                proj['validation_status'] = 'lifecycle_only'
            else:
                # Only a reconstructed creation. Saying "lifecycle only" here
                # would claim a deletion or promotion was recorded, and none
                # was: the entry was rebuilt from the document after the fact.
                # Measured on prod 2026-08-31, every one of the 61 projects
                # wearing the "Lifecycle only" badge was this case and not one
                # had a real lifecycle event, because the creation backfill had
                # just given a reconstructed entry to every project that lacked
                # one. Before the backfill they read "N/A", which was honest;
                # this state is the accurate version of that -- there is a
                # history to show, but nothing independently recorded in it.
                proj['validation_status'] = 'reconstructed_only'

            ts = latest_any.get('timestamp') if latest_any else None
            proj['latest_audit_timestamp'] = ts
            if isinstance(ts, datetime.datetime):
                proj['latest_audit_timestamp_display'] = ts.strftime('%Y-%m-%d %H:%M:%S UTC')
            else:
                proj['latest_audit_timestamp_display'] = str(ts) if ts else '—'
        except Exception as e:
            logging.warning(f"Could not compute validation status for {proj['id_str']}: {e}")
            proj['validation_status'] = 'error'
            proj['latest_audit_timestamp'] = None
            proj['latest_audit_timestamp_display'] = '—'

    # Sort projects by most recent audit log entry first, then by name
    all_projects.sort(key=lambda p: (
        p.get('latest_audit_timestamp') or datetime.datetime.min,
        (p.get('project_name') or '').lower()
    ), reverse=True)

    # Detail: load audit log for selected project
    selected_project_id = request.GET.get('project_id', '').strip()

    entries = []
    display_name = None
    matched_uuids = []
    error_message = None
    total_entries = 0
    selected_project = None

    if selected_project_id:
        try:
            # Find the selected project in the master list
            selected_project = next(
                (p for p in all_projects if p['id_str'] == selected_project_id), None
            )
            if selected_project is None:
                # Try to find it even if it's been marked deleted/non-current
                raw_proj = collection_handle.find_one({'_id': ObjectId(selected_project_id)})
                if raw_proj:
                    raw_proj['id_str'] = str(raw_proj['_id'])
                    prepare_project_linkid(raw_proj)
                    selected_project = raw_proj

            if selected_project:
                search_term = selected_project.get('project_name') or selected_project_id
                matched_uuids, display_name = get_project_version_chain(search_term)

                if matched_uuids:
                    raw_entries = list(
                        audit_log_handle.find(
                            {'project_uuid': {'$in': matched_uuids}}
                        ).sort('timestamp', -1)
                    )
                    for entry in raw_entries:
                        entry['id_str'] = str(entry['_id'])
                        ts = entry.get('timestamp')
                        if isinstance(ts, datetime.datetime):
                            entry['timestamp_display'] = ts.strftime('%Y-%m-%d %H:%M:%S UTC')
                        else:
                            entry['timestamp_display'] = str(ts) if ts else '—'
                    entries = raw_entries
                    total_entries = len(entries)
                else:
                    error_message = 'No audit log version chain found for this project.'
            else:
                error_message = 'Project not found.'
        except Exception as e:
            logging.error(f"Error querying audit log for project_id '{selected_project_id}': {e}")
            error_message = f'Error querying audit log: {e}'

    return {
        'audit_all_projects': all_projects,
        'selected_project_id': selected_project_id,
        'selected_project': selected_project,
        'display_name': display_name,
        'matched_uuids': matched_uuids,
        'entries': entries,
        'total_entries': total_entries,
        # So the template can mark a deletion event that was recorded before
        # its mutation and never confirmed -- an operation that started and did
        # not finish. Only these types are written that way; an unconfirmed
        # create or edit means nothing, because those are logged afterwards.
        'deletion_events': list(provenance.DELETION_EVENTS),
        'audit_error_message': error_message,
    }


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_audit_log(request):
    """Backward-compat redirect — audit log is now part of the project files report page."""
    qs = request.GET.urlencode()
    url = '/admin-project-files-report/'
    if qs:
        url += '?' + qs
    return redirect(url)


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_audit_log_validate(request):
    """
    AJAX endpoint: compares the most recent audit-log entry for a project against
    the live project document and the current S3 tar.gz file size.

    GET params:
        project_id  – MongoDB _id string of the project

    Returns JSON with a list of check results, each:
        { field, log_value, live_value, match: true|false }
    """
    from django.http import JsonResponse

    project_id = request.GET.get('project_id', '').strip()
    if not project_id:
        return JsonResponse({'error': 'project_id is required'}, status=400)

    try:
        # ── 1. Load live project ──────────────────────────────────────────────
        project = collection_handle.find_one({'_id': ObjectId(project_id)})
        if project is None:
            return JsonResponse({'error': 'Project not found'}, status=404)

        prepare_project_linkid(project)

        # ── 2. Find the most recent audit-log entry for this project ──────────
        from .utils import get_project_version_chain
        search_term = project.get('project_name') or project_id
        matched_uuids, _ = get_project_version_chain(search_term)

        latest_entry = _latest_payload_entry(matched_uuids)

        if latest_entry is None:
            return JsonResponse({'error': 'No audit log entries found for this project'}, status=404)

        # ── 3. Run shared checks ──────────────────────────────────────────────
        result = _run_audit_checks(project, latest_entry)

        ts = latest_entry.get('timestamp')
        ts_display = ts.strftime('%Y-%m-%d %H:%M:%S UTC') if isinstance(ts, datetime.datetime) else str(ts)

        return JsonResponse({
            'checks':         result['checks'],
            'log_timestamp':  ts_display,
            'log_event_type': latest_entry.get('event_type') or ('edit_new_version' if latest_entry.get('new_version') else 'edit_no_version'),
            'all_match':      result['all_match'],
            'any_mismatch':   result['any_mismatch'],
        })

    except Exception as e:
        logging.error(f"admin_audit_log_validate error for project_id '{project_id}': {e}")
        return JsonResponse({'error': str(e)}, status=500)


# ---------------------------------------------------------------------------
# GridFS ownership
# ---------------------------------------------------------------------------

#: Re-exported so the page, its tests and the command all name one thing. The
#: survey itself does not run here -- see caper/ownership_survey.py for why.
OWNERSHIP_REPORTS = ownership_survey.REPORTS_COLLECTION
OWNERSHIP_PROJECT_ROWS = ownership_survey.PROJECT_ROWS
OWNERSHIP_SURVEY_ABANDONED_AFTER = ownership_survey.ABANDONED_AFTER
mark_abandoned_surveys = ownership_survey.mark_abandoned_surveys


def _ownership_reports():
    return db_handle_primary[OWNERSHIP_REPORTS]


def admin_file_ownership(request):
    """Report which project owns each GridFS file, and how much is residue.

    A report, and only a report. There is no control on this page that deletes
    anything, deliberately: the two production incidents behind this work were
    both a count like this one being believed and acted on. What the page is
    for is deciding whether a deletion is worth investigating, not performing
    it.
    """
    from .gridfs_ownership import (
        ORDER, RESIDUE_DOCUMENT_GONE, RESIDUE_UNLABELLED, RESIDUE_UNREFERENCED,
        human,
    )

    if not request.user.is_staff:
        return redirect('/accounts/logout')

    reports = _ownership_reports()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'run':
            # Started, not run. The walk takes minutes against prod, and this
            # process has a request to finish and eight siblings serving
            # traffic. spawn() returns as soon as the child is running; the row
            # inserted here is what the page shows until the child fills it in.
            notify = getattr(request.user, 'email', '') or None
            report_id = ownership_survey.open_report(
                reports, str(request.user), notify)
            if ownership_survey.spawn(report_id, str(request.user),
                                      notify) is None:
                reports.update_one(
                    {'_id': report_id},
                    {'$set': {'state': 'failed',
                              'error': 'the survey process would not start'}})
        elif action == 'remove_snapshot':
            # Not spelled 'delete': that literal is reserved for the status
            # flag field, and the guard in tests/test_project_status_guard.py
            # cannot tell a form action from a flag. The name is better here
            # anyway -- what it removes is a measurement, not data.
            snapshot_id = request.POST.get('report_id')
            if snapshot_id:
                reports.delete_one({'_id': ObjectId(snapshot_id)})
        return redirect('/admin-file-ownership/')

    snapshots = list(reports.find().sort('started_at', -1).limit(25))
    for snapshot in snapshots:
        snapshot['id_str'] = str(snapshot['_id'])

    mark_abandoned_surveys(snapshots)

    wanted = request.GET.get('snapshot')
    latest = None
    if wanted:
        latest = next((s for s in snapshots if s['id_str'] == wanted), None)
    if latest is None:
        latest = next((s for s in snapshots if s.get('state') == 'done'), None)
    running_snapshot = next((s for s in snapshots
                             if s.get('state') == 'running'), None)
    running = running_snapshot is not None

    rows = []
    if latest:
        total = latest.get('total_files') or 0
        for label in ORDER:
            count = (latest.get('counts') or {}).get(label, 0)
            rows.append({
                'label': label,
                'count': count,
                'share': (count / total * 100) if total else 0,
                'size': human((latest.get('bytes') or {}).get(label, 0)),
                'owned': label.startswith('owned'),
                'unlabelled': label == RESIDUE_UNLABELLED,
            })

    return render(request, 'pages/admin_file_ownership.html', {
        'user': request.user,
        'SITE_TITLE': settings.SITE_TITLE,
        'snapshots': snapshots,
        'latest': latest,
        'running': running,
        'running_progress': running_snapshot,
        'rows': rows,
        'residue_size': human(latest.get('residue_bytes', 0)) if latest else '',
        'total_size': human(latest.get('total_bytes', 0)) if latest else '',
        'unlabelled_label': RESIDUE_UNLABELLED,
        'gone_label': RESIDUE_DOCUMENT_GONE,
        'unreferenced_label': RESIDUE_UNREFERENCED,
    })
