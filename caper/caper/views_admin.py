"""
Admin-related views for the Caper application.
This module contains all administrative functions that require staff privileges.
"""

import logging
import os
import csv
import subprocess
import shutil
from pathlib import Path

from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags

import boto3

from bson.objectid import ObjectId

from .forms import FeaturedProjectForm, DeletedProjectForm, SendEmailForm
from .utils import (
    collection_handle, collection_handle_primary, fs_handle,
    get_one_project, get_one_deleted_project, prepare_project_linkid,
    check_if_db_field_exists, get_date_short, previous_versions,
    form_to_dict, get_date
)

from .extra_metadata import *

from .site_stats import get_latest_site_statistics, regenerate_site_statistics

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

    public_projects = list(collection_handle_primary.find({'private': False, 'delete': False}))
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
            # Process sample download stats
            pass
        else:
            project['sample_downloads'] = {}

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
        # delete Samples & Features and feature files from mongo,
        # Is this needed or will deleting the parent project delete the whole thing
        current_runs = project['runs']
        runs = project['runs']
        for sample in runs:
            for feature in sample:
                key_names = ['Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file', 'AA directory',
                             'cnvkit directory']
                for k in key_names:
                    try:
                        fs_handle.delete(ObjectId(sample[k]))

                    except:
                        # DO NOTHING, its not there
                        id_var = "Not Provided"
    except:
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
            # Member projects: username is one of the members, but not the only one
            # Query for projects where the username is in the project_members array
            member_projects = list(collection_handle.find({
                'current': True,
                'project_members': {'$all': [username]}
            }))

            # Filter the results to ensure the project_members array has more than one member
            member_projects = [project for project in member_projects if len(project.get('project_members', [])) > 1]

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
    else:
        private_projects = []

    public_proj_count = 0
    public_sample_count = 0

    # public_projects = get_projects_close_cursor({'private' : False, 'delete': False})
    public_projects = list(collection_handle.find({'private' : False, 'delete': False, 'current': True}))
    for proj in public_projects:
        prepare_project_linkid(proj)
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    datetime_status = check_datetime(public_projects) + check_datetime(private_projects)
    sample_count_status = check_sample_count_status(private_projects) + check_sample_count_status(public_projects)

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
        'schema_report': schema_report,
    })


@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_prepare_shutdown(request):
    """Toggle shutdown pending mode"""
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    
    from .context_processor import get_shutdown_pending, set_shutdown_pending
    
    if request.method == "POST":
        # Toggle the shutdown mode
        current_status = get_shutdown_pending()
        set_shutdown_pending(not current_status)
        return redirect('/admin-prepare-shutdown')
    
    # Get current status
    shutdown_status = get_shutdown_pending()
    
    return render(request, 'pages/admin_prepare_shutdown.html', {
        'shutdown_pending': shutdown_status,
        'user': request.user
    })
