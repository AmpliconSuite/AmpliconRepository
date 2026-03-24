"""
Admin-related views for the Caper application.
This module contains all administrative functions that require staff privileges.
"""

import logging
import os
import csv
import subprocess
import shutil
import datetime
from pathlib import Path

from django.http import HttpResponse
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

from .forms import FeaturedProjectForm, DeletedProjectForm, SendEmailForm
from .utils import (
    collection_handle, collection_handle_primary, fs_handle, audit_log_handle,
    get_one_project, get_one_deleted_project, prepare_project_linkid,
    check_if_db_field_exists, get_date_short, previous_versions,
    form_to_dict, get_date, db_handle_primary, format_visibility_for_display,
    get_project_version_chain,
)

from .extra_metadata import *

from .site_stats import get_latest_site_statistics, regenerate_site_statistics
from .tar_utils import list_project_tar_contents


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
    live_sample_count = project.get('sample_count') or len(project.get('runs', {}))

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
    public_projects = list(collection_handle_primary.find({
        '$or': [
            {'private': False},
            {'private': 'public'}
        ],
        'delete': False,
        'current': True
    }))
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

    return render(request, 'pages/admin_version_details.html',
                  {'details': details, 'env': env, 'git': git_result, 'django_settings': settings_result})


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
    all_projects = list(collection_handle.find({'current': True, 'delete': False}))

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
                if project.get('private', False):
                    solo_private_projects += 1
                else:
                    solo_public_projects += 1

        # Store the stats for this user
        user_stats[user_id] = {
            'solo_private_projects': solo_private_projects,
            'solo_public_projects': solo_public_projects
        }

    # Get public project data for display
    public_projects = [p for p in all_projects if not p.get('private', True)]

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

    # Calculate stats
    for project in public_projects:
        project['sample_metadata_available'] = has_sample_metadata(project)
        if 'project_downloads' in project:
            # Process download stats
            pass
        else:
            project['project_downloads'] = {}

        if 'sample_downloads' in project:
            # Process sample download stats - sum the values from the dictionary
            if isinstance(project['sample_downloads'], dict):
                project['sample_downloads_sum'] = sum(project['sample_downloads'].values())
            else:
                # Handle legacy integer format
                project['sample_downloads_sum'] = project['sample_downloads']
        else:
            project['sample_downloads'] = {}
            project['sample_downloads_sum'] = 0

    repo_stats = get_latest_site_statistics()

    return render(request, 'pages/admin_stats.html', {
        'public_projects': public_projects,
        'users': users,
        'user_stats': user_stats,
        'site_stats': repo_stats
    })


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


# wrapper in views for site stats to keep the import of site_stats.py out of urls.py
@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def site_stats_regenerate(request):
    regenerate_site_statistics()
    return admin_stats(request)


def project_stats_download(request):
    # Get public and private project data
    public_projects = list(collection_handle.find({'private': False, 'delete': False, 'current': True}))
    for project in public_projects:
        if not 'project_downloads' in project:
            project['project_downloads_sum'] = 0
        else:
            project['project_downloads_sum'] = sum(project['project_downloads'].values())

        if not 'sample_downloads' in project:
            project['sample_downloads_sum'] = 0
        else:
            project['sample_downloads_sum'] = sum(project['sample_downloads'].values())

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


def admin_permanent_delete_project(project_id, project, project_name):
    """
    This function permanently deletes a project from s3 and from the server.
    """
    error_message = ""
    query = {'_id': ObjectId(project_id)}
    try:
        # delete Samples & Features and feature files from GridFS
        runs = project.get('runs', {})
        key_names = [
            'Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file',
            'AA directory', 'Sample metadata JSON', 'AA graph file', 'AA cycles file',
        ]
        for sample_name, features in runs.items():
            if not isinstance(features, list):
                continue
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                for k in key_names:
                    file_id = feature.get(k)
                    if file_id and file_id != 'Not Provided':
                        try:
                            fs_handle.delete(ObjectId(file_id))
                        except Exception:
                            logging.debug(f"Could not delete GridFS file {file_id} ({k}) for sample {sample_name}")
    except Exception:
        logging.exception('Problem deleting sample files from Mongo.')
        error_message = "Problem deleting sample files from Mongo."

    # delete project tar and files from mongo and local disk
    #    - assume all feature and sample files are in this dir
    try:
        fs_handle.delete(ObjectId(project['tarfile']))
    except KeyError:
        logging.exception(f'Problem deleting project tar file from mongo. {project["project_name"]}')
        error_message = error_message + " Problem deleting project tar file from mongo."

    try:
        if os.path.exists(f"../tmp/{project_id}/"):
            project_data_path = f"../tmp/{project_id}/"
        else:
            project_data_path = f"tmp/{project_id}/"

        shutil.rmtree(project_data_path)
    except:
        logging.exception(f'Problem deleting tar file from local drive. {project_data_path}')

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
            solo_projects = list(collection_handle.find({'current': True, 'project_members': [username]}))
            # Add formatted visibility to solo projects
            for proj in solo_projects:
                proj['visibility_display'] = format_visibility_for_display(proj.get('private', True))
            
            # Member projects: username is one of the members, but not the only one
            # Query for projects where the username is in the project_members array
            member_projects = list(collection_handle.find({
                'current': True,
                'project_members': {'$all': [username]}
            }))

            # Filter the results to ensure the project_members array has more than one member
            member_projects = [project for project in member_projects if len(project.get('project_members', [])) > 1]
            # Add formatted visibility to member projects
            for proj in member_projects:
                proj['visibility_display'] = format_visibility_for_display(proj.get('private', True))

        elif action == 'delete_user':

            # for solo projects that are private, delete them
            solo_projects = list(collection_handle.find({'current': True, 'project_members': [username]}))

            for project in solo_projects:
                project_name = project['project_name']
                project_id = project['_id']

                # delete the project
                if project['private']:
                    error_message += f"User {username} deleted, project {project_name} was private and deleted. "
                    error_message += admin_permanent_delete_project(project_id, project, project_name)
                else:
                    # replace username as owner with 'jluebeck' if a user exists by that name
                    # or by an admin user
                    query = {'_id': ObjectId(project_id)}
                    # check if user jluebeck exists
                    anAdmin = 'admin'
                    if User.objects.filter(username='jluebeck').exists():
                        anAdmin = 'jluebeck'
                    else:
                        # replace with admin user
                        if User.objects.filter(is_staff=True).exists():
                            anAdmin = User.objects.filter(is_staff=True).first().username
                    new_val = {"$set": {'project_members': [anAdmin]}}
                    error_message += f"User {username} deleted, project {project_name} was public and reassigned to {anAdmin}. "
                    collection_handle.update_one(query, new_val)

            # for member projects, remove the user from the project members
            member_projects = [
                project for project in collection_handle.find({
                    'current': True,
                    'project_members': {'$all': [username]}
                })
                if len(project.get('project_members', [])) > 1  # Ensure the array size is greater than 1
            ]
            for project in member_projects:
                project_name = project['project_name']
                project_id = project['_id']
                query = {'_id': ObjectId(project_id)}
                new_val = {"$pull": {'project_members': username}}
                collection_handle.update_one(query, new_val)
                error_message += f"User {username} removed from project {project_name}. "
            # delete the user
            try:
                user = User.objects.get(username=username)
                user.delete()
                error_message += f"User {username} deleted successfully."
            except User.DoesNotExist:
                error_message += f"User {username} does not exist."

            solo_projects = []
            member_projects = []

    return render(request, 'pages/admin_delete_user.html',
                  {'username': username,
                   'solo_projects': solo_projects,
                   'member_projects': member_projects,
                   'error_message': error_message})


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_delete_project(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    error_message = ""
    if request.method == "POST":
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
            new_val = {"$set": {'delete': False}}
            collection_handle.update_one(query, new_val)
            error_message = f"Project {project_name} restored."

        elif deleteit and (action == 'delete'):

            project = get_one_deleted_project(project_id)
            prev_ver_list, msg = previous_versions(project)
            ## find all previous versions of the project we are trying to delete.
            ## If this is an older version, do not delete

            if prev_ver_list:
                for proj in prev_ver_list:
                    p = get_one_deleted_project(proj['linkid'])
                    admin_permanent_delete_project(proj['linkid'], p, p['project_name'])

            error_message = admin_permanent_delete_project(project_id, project, project_name)

    deleted_projects = list(collection_handle.find({'delete': True, 'current': True}))
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
    # Run the schema validation directly
    try:
        from caper.schema_validate import run_fix_schema

        # Get the schema path relative to the project root
        schema_path = "schema/schema.json"

        # Run the validation and get the report
        fix_schema_report = run_fix_schema(
            db_host=None,  # Use the existing connection from utils
            collection_name="projects",
            schema_path=schema_path
        )

    except Exception as e:
        fix_schema_report = f"Error running fix schema: {str(e)}"

    return render(request, "pages/admin_fix_schema_report.html", {
        'fix_schema_report': fix_schema_report,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def data_qc(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email

        # private_projects = get_projects_close_cursor({'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}]  , 'delete': False})
        private_projects = list(collection_handle.find({'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}]  , 'delete': False, 'current': True}))
        for proj in private_projects:
            prepare_project_linkid(proj)
            proj['visibility_display'] = format_visibility_for_display(proj.get('private', True))
    else:
        private_projects = []

    public_proj_count = 0
    public_sample_count = 0

    # public_projects = get_projects_close_cursor({'private' : False, 'delete': False})
    public_projects = list(collection_handle.find({'private' : False, 'delete': False, 'current': True}))
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
        delete_val = project.get('delete', None)
        current_val = project.get('current', None)
        
        # Must have delete=False and current=False
        if delete_val == False and current_val == False:
            project_id = str(project.get('_id', 'NO_ID'))
            
            # Check if this project is truly orphaned:
            # 1. It should not have any entries in its own previous_versions array
            has_previous = len(project.get('previous_versions', [])) > 0
            
            # 2. It should not be referenced in any other project's previous_versions
            is_referenced = collection_handle.count_documents({
                'previous_versions.linkid': project_id
            }) > 0
            
            if not has_previous and not is_referenced:
                # No other versions found - this is orphaned!
                prepare_project_linkid(project)
                project['visibility_display'] = format_visibility_for_display(project.get('private', True))
                orphaned_projects.append(project)

    # Run the schema validation directly
    try:
        from caper.schema_validate import run_validation

        # Get the schema path relative to the project root
        schema_path = "schema/schema.json"

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
        
        try:
            # Get the project first to check its privacy setting
            project = collection_handle.find_one({'_id': ObjectId(project_id)})
            
            if not project:
                messages.error(request, f"Project {project_id} not found")
                return redirect('data_qc')
            
            # Update the project to set current=True
            result = collection_handle.update_one(
                {'_id': ObjectId(project_id)},
                {'$set': {'current': True}}
            )
            
            if result.modified_count > 0:
                # Add the project to site statistics
                is_private = project.get('private', False)
                add_project_to_site_statistics(project, is_private)
                
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
    
    return render(request, 'pages/admin_settings.html', {
        'shutdown_pending': shutdown_status,
        'registration_disabled': registration_disabled_status,
        'user': request.user
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
    all_projects = list(collection_handle.find({'current': True, 'delete': False}))
    
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
        is_private = project.get('private', True)
        
        # Initialize report data for this project
        report = {
            'project_id': project_id,
            'project_name': project_name,
            'is_private': is_private,
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


def _get_audit_log_context(request):
    """
    Build the audit log context dict shared by admin_project_files_report and admin_audit_log.
    """
    all_projects = list(collection_handle.find({'current': True, 'delete': False}))
    for proj in all_projects:
        prepare_project_linkid(proj)
        proj['id_str'] = str(proj['_id'])

    # Sort projects by name for easy browsing
    all_projects.sort(key=lambda p: (p.get('project_name') or '').lower())

    # For each project, find its most recent audit-log entry and run validation checks
    for proj in all_projects:
        search_term = proj.get('project_name') or proj['id_str']
        try:
            matched_uuids, _ = get_project_version_chain(search_term)
            latest_entry = None
            if matched_uuids:
                latest_entry = audit_log_handle.find_one(
                    {'project_uuid': {'$in': matched_uuids}},
                    sort=[('timestamp', -1)]
                )
            if latest_entry:
                result = _run_audit_checks(proj, latest_entry)
                proj['validation_status'] = result['status']  # 'pass' | 'mismatch' | 'missing_data'
                ts = latest_entry.get('timestamp')
                proj['latest_audit_timestamp'] = ts
                if isinstance(ts, datetime.datetime):
                    proj['latest_audit_timestamp_display'] = ts.strftime('%Y-%m-%d %H:%M:%S UTC')
                else:
                    proj['latest_audit_timestamp_display'] = str(ts) if ts else '—'
            else:
                proj['validation_status'] = 'no_log'
                proj['latest_audit_timestamp'] = None
                proj['latest_audit_timestamp_display'] = '—'
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

        latest_entry = None
        if matched_uuids:
            latest_entry = audit_log_handle.find_one(
                {'project_uuid': {'$in': matched_uuids}},
                sort=[('timestamp', -1)]
            )

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


