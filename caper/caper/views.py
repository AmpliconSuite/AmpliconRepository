from django.http import HttpResponse, StreamingHttpResponse, HttpResponseRedirect, Http404, JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import user_passes_test, login_required

from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.contrib.auth.models import User

## API framework packages
from rest_framework.response import Response
from werkzeug.debug import console

from .user_preferences import update_user_preferences, get_user_preferences, notify_users_of_project_membership_change
from .site_stats import regenerate_site_statistics, get_latest_site_statistics, add_project_to_site_statistics, delete_project_from_site_statistics, edit_proj_privacy
from .serializers import FileSerializer
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser
from rest_framework import status
from pathlib import Path

# from django.views.generic import TemplateView
# from pymongo import MongoClient
from django.conf import settings

# from .models import File
from .forms import RunForm, UpdateForm, FeaturedProjectForm, DeletedProjectForm, SendEmailForm, UserPreferencesForm
from .extra_metadata import *
from django.forms.models import model_to_dict

# imports for coamp graph
from .neo4j_utils import load_graph, fetch_subgraph

import subprocess
import shutil
import caper.sample_plot as sample_plot
import caper.StackedBarChart as stacked_bar
import caper.project_pie_chart as piechart
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile

from wsgiref.util import FileWrapper
import boto3, botocore, fnmatch, uuid, datetime, time, logging
from threading import Thread
import dateutil.parser

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

## Message framework
from django.contrib import messages
from django.utils.safestring import mark_safe

from .view_download_stats import *

## aggregator
from AmpliconSuiteAggregator import *
import AmpliconSuiteAggregator

# search
from .search import *


# SET UP HANDLE
def loading(request):
    return render(request, "pages/loading.html")


# Site-wide focal amp color scheme
fa_cmap = {
                'ecDNA': "rgb(255, 0, 0)",
                'BFB': 'rgb(0, 70, 46)',
                'Complex non-cyclic': 'rgb(255, 190, 0)',
                'Linear amplification': 'rgb(27, 111, 185)',
                'Complex-non-cyclic': 'rgb(255, 190, 0)',
                'Linear': 'rgb(27, 111, 185)',
                'Virus': 'rgb(163,163,163)',
                }


def get_date():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%dT%H:%M:%S.%f')
    return date


def get_date_short():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%d')
    return date


def get_one_deleted_project(project_name_or_uuid):
    try:

        # old cursor
        project = collection_handle.find({'_id': ObjectId(project_name_or_uuid), 'delete': True})[0]
        # project = get_projects_close_cursor({'_id': ObjectId(project_name_or_uuid), 'delete': True})[0]


        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        project = collection_handle.find_one({'project_name': project_name_or_uuid, 'delete': False})
        logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
        prepare_project_linkid(project)

    if project is None:
        logging.error(f"Project is None for {project_name_or_uuid}")

    return project


def get_one_feature(project_name, sample_name, feature_name):
    project, sample = get_one_sample(project_name, sample_name)
    feature = list(filter(lambda s: s['Feature_ID'] == feature_name, sample))
    return project, sample, feature


def check_project_exists(project_id):
    if collection_handle.count_documents({'_id': ObjectId(project_id)}, limit = 1):
        return True

    elif collection_handle.count_documents({'project_name': project_id}, limit = 1):
        return True

    else:
        return False


def samples_to_dict(form_file):
    return json.load(form_file)['runs']


def form_to_dict(form):
    # print(form)
    run = form.save(commit=False)
    form_dict = model_to_dict(run)

    if "alias" in form_dict:
        try:
            form_dict['alias'] = form_dict['alias'].replace(' ', '_')
            print(f'alias for this project is: {form_dict["alias"]}')
        except:
            print('No alias provided, probably Null')
    return form_dict


def get_project_oncogenes(runs):
    return list({
        gene.strip().replace("'", '')
        for sample in runs
        for feature in runs[sample]
        if feature['Oncogenes']
        for gene in feature['Oncogenes']
        if gene
    })


def get_project_classifications(runs):
    return list({
        feature['Classification'].upper()
        for sample in runs
        for feature in runs[sample]
        if feature['Classification']
    })


def get_sample_oncogenes(feature_list, sample_name):
    """
    Finds the oncogenes for a given sample_name
    """
    return sorted({
        gene.strip().replace("'", '')
        for feature in feature_list
        if feature['Sample_name'] == sample_name and feature['Oncogenes']
        for gene in feature['Oncogenes']
        if gene
    })


def get_sample_classifications(feature_list, sample_name):
    return list({
        feature['Classification'].upper()
        for feature in feature_list
        if feature['Sample_name'] == sample_name and feature['Classification']
    })


# @caper.context_processor
def get_files(fs_id):
    wrapper = fs_handle.get(fs_id)
    # response =  StreamingHttpResponse(FileWrapper(wrapper),content_type=file_['contentType'])
    return wrapper


# This function appears to be unused
def modify_date(projects):
    """
    Modifies the date to this format:

    MM DD, YYYY HH:MM:SS AM/PM

    """

    for project in projects:
        formats_to_try = [f"%Y-%m-%dT%H:%M:%S.%f", f"%B %d, %Y %I:%M:%S %p ", f"%Y-%m-%d", f"%Y-%m-%dT%H:%M:%S"]
        for fmt in formats_to_try:
            try:
                dt = datetime.datetime.strptime(project['date'], fmt)
                project['date'] = (dt.strftime(f'%B %d, %Y %I:%M:%S %p %Z'))
            except Exception as e:
                # logging.exception("Could not modify date for " + project['project_name'])
                continue

    return projects


def data_qc(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email

        # private_projects = get_projects_close_cursor({'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}]  , 'delete': False})
        private_projects = list(collection_handle.find({'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}]  , 'delete': False}))
        for proj in private_projects:
            prepare_project_linkid(proj)
    else:
        private_projects = []

    public_proj_count = 0
    public_sample_count = 0

    # public_projects = get_projects_close_cursor({'private' : False, 'delete': False})
    public_projects = list(collection_handle.find({'private' : False, 'delete': False}))
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


def check_datetime(projects):
    errors = 0
    for project in projects:
        try:
            change_to_standard_date(project['date'])
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


def change_to_standard_date(date):
    date = dateutil.parser.parse(date)
    date = date.strftime(f'%Y-%m-%dT%H:%M:%S')
    return date


def change_database_dates(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    logging.debug('Starting to update timestamps...')
    projects = list(collection_handle.find({'delete': False}))
    # projects = get_projects_close_cursor({'delete': False})

    for project in projects:
        recently_updated = change_to_standard_date(project['date'])
        date_created = change_to_standard_date(project['date_created'])
        new_values = {"$set" : {'date' : recently_updated,
                                'date_created' : date_created}}
        query = {'_id' : project['_id'],
                    'delete': False}
        collection_handle.update(query, new_values)

        # if "previous_versions" in project:
        #     updated_versions = project.previous_versions.view()
        #     #for version in json.loads(project['previous_versions'][0]):
        #     #    re_up = change_to_standard_date(version['date'])
        #     #    version['date'] = re_up
        #     #    updated_versions.append(version)
        #     another_update = {'date':recently_updated, 'link':str(project['linkid'])}
        #     updated_versions.append(another_update)
        #
        #     # Update the previous_versions field with the updated versions
        #     collection_handle.update(
        #         {'_id': project['_id']},
        #         {'$set': {'previous_versions': updated_versions}}
        #     )

    response = redirect('/data-qc')
    logging.info('Updated timestamps')
    return response


def update_sample_counts(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    logging.debug('Starting to update sample count for each project...')
    projects = get_projects_close_cursor({'delete': False})

    for project in projects:
        # Update current project sample count
        sample_count = len(project['runs'])
        new_values = {"$set": {'sample_count': sample_count}}
        query = {'_id': project['_id'], 'delete': False}
        collection_handle.update(query, new_values)


    response = redirect('/data-qc')
    logging.info('Updated sample counts for each project and their previous versions')
    return response


def index(request):
    t_sa = time.time()

    # Base query for non-deleted projects
    base_query = {'delete': False}
    projection = {'runs': 0}  # Exclude runs field from all queries

    # Get public projects (including featured) in one query
    public_query = {**base_query, 'private': False}
    public_projects = list(collection_handle.find(public_query, projection))

    # Extract featured projects from public projects
    featured_projects = [proj for proj in public_projects if proj.get('featured')]

    # Process project links
    for proj in public_projects:
        prepare_project_linkid(proj)

    # Handle private projects for authenticated users
    if request.user.is_authenticated:
        private_query = {
            **base_query,
            'private': True,
            '$or': [
                {'project_members': request.user.username},
                {'project_members': request.user.email}
            ]
        }
        private_projects = list(collection_handle.find(private_query, projection))

        for proj in private_projects:
            prepare_project_linkid(proj)
    else:
        private_projects = []

    site_stats = get_latest_site_statistics()
    logging.info(f"Retrieved info for index page in {time.time() - t_sa} seconds")
    return render(request, "pages/index.html", {
        'public_projects': public_projects,
        'private_projects': private_projects,
        'featured_projects': featured_projects,
        'site_stats': site_stats
    })


def profile(request, message_to_user=None):

    username = request.user.username
    try:
        useremail = request.user.email
    except:
        # not logged in
        # print(request.user)
        ## if user is anonymous, then need to login
        useremail = ""
        ## redirect to login page
        return redirect('account_login')


    # prevent an absent/null email from matching on anything
    if not useremail:
        useremail = username
    projects = list(collection_handle.find({"$or": [{"project_members": username}, {"project_members": useremail}] , 'delete': False}))
    # projects = get_projects_close_cursor({"$or": [{"project_members": username}, {"project_members": useremail}] , 'delete': False})

    for proj in projects:
        prepare_project_linkid(proj)

    prefs = get_user_preferences(request.user)
    form = UserPreferencesForm(prefs)
    if (prefs.pop('welcomeMessage', None)):
        if (message_to_user == None):
            message_to_user = ""
        message_to_user = message_to_user + "Email notification preferences can now be set on your profile page."


    messages.add_message(request, messages.INFO, message_to_user)
    return render(request, "pages/profile.html", {'projects': projects, 'SITE_TITLE':settings.SITE_TITLE, 'preferences': prefs})


def login(request):
    return render(request, "pages/login.html")


def reference_genome_from_project(samples):
    reference_genomes = list()
    for sample, features in samples.items():
        for feature in features:
            # Check if Reference_version exists before accessing
            if 'Reference_version' in feature:
                reference_genomes.append(feature['Reference_version'])
            else:
                # If not found, add a placeholder or default value
                print(f"Warning: Missing Reference_version in feature from sample {sample}")
                reference_genomes.append('Unknown')

    # Handle case with no reference genomes found
    if not reference_genomes:
        return 'Unknown'

    # Handle multiple reference genomes
    if len(set(reference_genomes)) > 1:
        return 'Multiple'

    # Return the single reference genome
    return reference_genomes[0]

def reference_genome_from_sample(sample_data):
    reference_genomes = list()
    for feature in sample_data:
        reference_genomes.append(feature['Reference_version'])
    if len(set(reference_genomes)) > 1:
        reference_genome = 'Multiple'
    else:
        reference_genome = reference_genomes[0]
    return reference_genome


def is_user_a_project_member(project, request):
    try:
        current_user_email = request.user.email
        current_user_username = request.user.username
        if not current_user_email:
            current_user_email = current_user_username
    except:
        current_user_email = 0
        current_user_username = 0

    if current_user_username in project['project_members']:
        return True
    if current_user_email in project['project_members']:
        return True
    return False


def set_project_edit_OK_flag(project, request):
    if (is_user_a_project_member(project, request)):
        project['current_user_may_edit'] = True
    else:
        project['current_user_may_edit'] = False


def create_aggregate_df(project, samples):
    """
    creates the aggregate dataframe for figures:
    """
    t_sa = time.time()

    dfl = []
    for _, dlist in samples.items():
        dfl.append(pd.DataFrame(dlist))
    aggregate = pd.concat(dfl)
    aggregate.columns = [col.replace(' ', "_") for col in aggregate.columns]
    proj_id = str(project['_id'])
    if not os.path.exists(os.path.join('tmp', proj_id)):
        os.system(f'mkdir -p tmp/{proj_id}')
    aggregate_save_fp = os.path.join('tmp', proj_id, f'{proj_id}_aggregated_df.csv')
    aggregate.to_csv(aggregate_save_fp)
    t_sb = time.time()
    diff = t_sb - t_sa
    logging.info(f"Iteratively build project dataframe from samples in {diff} seconds")

    return aggregate, aggregate_save_fp

def previous_versions(project):
    """
    Gets a list of previous versions via UUID
    """
    res = []
    msg = None
    # print(project['_id'])
    logging.error(f"Getting previous versions for project {project['_id']}")
    ### Accessing a previous version of a project.
    ## looking for the old link in the previous project. Will output something if
    ## we are trying to access an older project
    #cursor = collection_handle.find(
    #    {'current': True, 'previous_versions.linkid' : str(project['_id'])}, {'date': 1, 'previous_versions':1}).sort('date', -1)
    #data = list(cursor)
    fields = ['date', 'previous_versions', 'AC_version', 'AA_version', 'AP_version']
    cursor = collection_handle.find(
        {'current': True, 'previous_versions.linkid': str(project['_id'])},
        {field: 1 for field in fields}
    ).sort('date', -1)
    data = []
    for doc in cursor:
        logging.error(f" RES FOR  {project['_id']}")
        for field in ['AC_version', 'AA_version', 'AP_version']:
            if field not in doc:
                doc[field] = 'NA'
                logging.error(f"Field {field} not found in document, setting to 'NA'")
            else:
                logging.error(f"Field {field} found in document, setting to 'NA'")
                
        data.append(doc)
    
    cursor.close()
    if len(data) == 1:
        res = data[0]['previous_versions']
        res.append({'date': data[0]['date'],
                    'linkid' : str(data[0]['_id']),
                    'AC_version': data[0].get('AC_version', 'NA'),
                    'AA_version': data[0].get('AA_version', 'NA'),
                    'ASP_version': data[0].get('ASP_version', 'NA')})
        res.reverse()
        msg = f"Viewing an older version of the project. View latest version <a href = '/project/{str(data[0]['_id'])}'>here</a>"


    else:
        ## accessing current version, getting list of previous versions
        if "previous_versions" in project:
            res = project['previous_versions']
        # add current main version to the list

        res.append({'date': project['date'],
                    'linkid':str(project['linkid']),
                    'AC_version': project.get('AC_version', 'NA'),
                    'AA_version': project.get('AA_version', 'NA'),
                    'ASP_version': project.get('ASP_version', 'NA')
                    })
        res.reverse()

    return res, msg

def project_page(request, project_name, message=''):
    """
    Render Project Page

    will append sample_data, ref genome, feature_list to project json in the database
    for faster querying in the future.
    """
    t_i = time.time()
    ## leaving this bit of code here
    ### this part will delete the metadata_stored field for a project
    ### is is only run IF we need to reset a project and reload the data

    # project = get_one_project(project_name) ## 0 loops
    # query = {'_id' : project['_id'],
    #                 'delete': False}
    # val = {'$unset':{'metadata_stored':""}}
    # collection_handle.update(query, val)
    # logging.warning('delete complete')

    ## if flag is unfinished, render a loading page:

    project = validate_project(get_one_project(project_name), project_name)
    if 'FINISHED?' in project and project['FINISHED?'] == False:
        return render(request, "pages/loading.html", {"project_name":project_name})

    if project['private'] and not is_user_a_project_member(project, request):
        return redirect('/accounts/login?next=/project/' + project_name)

    # if we got here by an OLD project id (prior to edits) then we want to redirect to the new one
    if not project_name == str(project['linkid']):
        return redirect('project_page', project_name=project['linkid'])

    prev_versions, prev_ver_msg = previous_versions(project)
    viewing_old_project = False
    if prev_ver_msg:
        messages.error(request, mark_safe(prev_ver_msg))
        viewing_old_project = True

    ## if the project being loaded is stored in another project's previous versions, then load the previous_versions list of the latest project,
    ## and give a message saying: this is not the latest version, and link to latest version.

    if 'metadata_stored' not in project:
        #dict_keys(['_id', 'creator', 'project_name', 'description', 'tarfile', 'date_created', 'date', 'private', 'delete', 'project_members', 'runs', 'Oncogenes', 'Classification', 'project_downloads', 'linkid'])
        set_project_edit_OK_flag(project, request) ## 0 loops
        samples = project['runs'].copy()
        features_list = replace_space_to_underscore(samples) # 1 loop
        reference_genome = reference_genome_from_project(samples) # 1 over sample nested with 1 over features O(S^f)
        sample_data = sample_data_from_feature_list(features_list) # O(S)
        aggregate, aggregate_save_fp = create_aggregate_df(project, samples)

        logging.debug(f'aggregate shape: {aggregate.shape}')
        new_values = {"$set" : {'sample_data' : sample_data,
                                'reference_genome' : reference_genome,
                                'aggregate_df' : aggregate_save_fp,
                                'metadata_stored': 'Yes'}}
        query = {'_id' : project['_id'],
                    'delete': False}

        logging.debug('Inserting Now')
        collection_handle.update(query, new_values)
        logging.debug('Insert complete')

    elif 'metadata_stored' in project:
        logging.info('Already have the lists in DB')
        set_project_edit_OK_flag(project, request) ## 0 loops
        samples = project['runs']
        # features_list = project['features_list']
        reference_genome = project['reference_genome']
        sample_data = project['sample_data']
        aggregate_df_fp = project['aggregate_df']
        if not os.path.exists(aggregate_df_fp):
            ## create the aggregate df if it doesn't exist already.
            aggregate, aggregate_df_fp = create_aggregate_df(project, samples)
        else:
            aggregate = pd.read_csv(aggregate_df_fp)

    stackedbar_plot = stacked_bar.StackedBarChart(aggregate, fa_cmap)
    pc_fig = piechart.pie_chart(aggregate, fa_cmap)
    t_f = time.time()
    diff = t_f - t_i
    logging.info(f"Generated the project page for '{project['project_name']}' with views.py in {diff} seconds")

    # check for an error when project was created, but don't override a message that was already sent in
    if not message:
        extraction_error = None
        project_error_file_path = f"tmp/{project_name}/project_extraction_errors.txt"
        alt_project_error_file_path = f"tmp/{project['project_name']}/project_extraction_errors.txt"
        if os.path.isfile(project_error_file_path):
            extraction_error = project_error_file_path
        elif os.path.isfile(alt_project_error_file_path):
            extraction_error = alt_project_error_file_path

        if extraction_error:
            over_a_month_old = time.time() - os.path.getmtime(extraction_error) > (30 * 24 * 60 * 60)
            if over_a_month_old:
                # if its been ignored for a month, rename the file and stop sending warnings
                os.rename(extraction_error, "_"+extraction_error)
            else:
                message = 'There was a problem extracting the results from the AmpliconAggregator .tar.gz file for this project.  Please notifiy the administrator so that they can help resolve the problem.'

    ## download & view statistics
    views, downloads = session_visit(request, project)
    return render(request, "pages/project.html", {'project': project,
                                                  'sample_data': sample_data,
                                                  'message':message,
                                                  'reference_genome': reference_genome,
                                                  'stackedbar_graph': stackedbar_plot,
                                                  'piechart': pc_fig,
                                                  'prev_versions' : prev_versions,
                                                  'prev_versions_length' : len(prev_versions),
                                                  "proj_id":str(project['linkid']),
                                                  'viewing_old_project': viewing_old_project,
                                                  'views' : views,
                                                  'downloads' : downloads})


def upload_file_to_s3(file_path_and_location_local, file_path_and_name_in_bucket):
    session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
    s3client = session.client('s3')
    logging.info(f'==== XXX STARTING upload of {file_path_and_location_local} to s3://{settings.S3_DOWNLOADS_BUCKET}/{settings.S3_DOWNLOADS_BUCKET_PATH}{file_path_and_name_in_bucket}')
    s3client.upload_file(f'{file_path_and_location_local}', settings.S3_DOWNLOADS_BUCKET,
                         f'{settings.S3_DOWNLOADS_BUCKET_PATH}{file_path_and_name_in_bucket}')
    logging.info('==== XXX uploaded to bucket ')


def check_if_db_field_exists(project, field):
    try:
        if project[field]:
            return True
    except:
        return False


def find_one(pattern, path):
    for root, dirs, files in os.walk(path):
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                return os.path.join(root, name)

    return None


def project_download(request, project_name):
    project = get_one_project(project_name)
    if check_if_db_field_exists(project, 'project_downloads'):
        project_download_data = project['project_downloads']
        if isinstance(project_download_data, int):
            temp_data = project_download_data
            project_download_data = dict()
            project_download_data[get_date_short()] = temp_data
        elif get_date_short() in project_download_data:
            project_download_data[get_date_short()] += 1
        else:
            project_download_data[get_date_short()] = 1
    else:
        project_download_data = dict()
        project_download_data[get_date_short()] = 1

    query = {'_id': ObjectId(project_name)}
    new_val = { "$set": {'project_downloads': project_download_data} }
    collection_handle.update_one(query, new_val)
    collection_handle.update_one(query, {'$inc':{'downloads':1}})
    # get the 'real_project_name' since we might have gotten  here with either the name or the project id passed in
    try:
        real_project_name = project['project_name']
    except:
        real_project_name = ""

    project_data_path = f"tmp/{project_name}"

    if settings.USE_S3_DOWNLOADS:
        logging.debug("Download with USE_S3_DOWNLOADS True")
        if '_id' in project:
            project_linkid = project['_id']
        elif 'linkid' in project:
            project_linkid = project['linkid']
        else:
            logging.error("Could not create linkid for project!")
            message = f"Project {project_name} is unavailable or deleted."
            messages.error(request, message)
            return redirect(request.META['HTTP_REFERER'])

        s3_file_location = f'{settings.S3_DOWNLOADS_BUCKET_PATH}{project_linkid}/{project_linkid}.tar.gz'
        logging.info(f'==== XXX STARTING download for {s3_file_location} for project {real_project_name}')

        if not settings.AWS_PROFILE_NAME:
            settings.AWS_PROFILE_NAME = 'default'

        session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
        s3client = session.client('s3')

        logging.info("BUCKET "+ settings.S3_DOWNLOADS_BUCKET)
        logging.info("FILELOC " + s3_file_location)
        logging.info("PROFILE " + settings.AWS_PROFILE_NAME)

        try:
            s3client.head_object(Bucket=settings.S3_DOWNLOADS_BUCKET, Key=s3_file_location)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404":
                # The object does not exist.
                # so we need to get a local file from mongo and push that to S3
                logging.debug("===== XXX PROJECT FILE NOT IN S3 --  GET IT IN THERE")
                tar_id = project['tarfile']
                tar_file_wrapper = FileWrapper(fs_handle.get(ObjectId(tar_id)), blksize=32728)

                isExist = os.path.exists(f'{project_data_path}')
                if not isExist:
                    # Create a new directory because it does not exist
                    os.makedirs(f'{project_data_path}')

                output = open(f'{project_data_path}/{project_linkid}.tar.gz', "wb")
                for chunk in tar_file_wrapper:
                    output.write(chunk)
                output.close()
                upload_file_to_s3(f'{project_data_path}/{project_linkid}.tar.gz', s3_file_location)
                logging.info('==== XXX upload to bucket complete, move on to get one time url')

            else:
                # Something else has gone wrong.
                raise
        else:
            # The object does exist.
            logging.debug('==== XXX found it in bucket, move on to get one time url')

        # we should have uploaded the file if it was not already there
        # get a one-time-use url and redirect the response
        expiration=600
        # good for seconds, can move this to settings later
        presigned_url = s3client.generate_presigned_url('get_object', Params = {'Bucket': settings.S3_DOWNLOADS_BUCKET, 'Key': s3_file_location}, ExpiresIn = expiration)
        return HttpResponseRedirect(presigned_url)

    ###### the following is used when S3 is not used for download
    chunk_size = 8192
    logging.info('==== XXX file DOES NOT EXIST must make it first and upload to S3 ')
    try:
        file_location = find_one('*.tar.gz', project_data_path)
        response = StreamingHttpResponse(
        FileWrapper(
            open(file_location, "rb"),
            chunk_size,
            )
        )
        filename = file_location.split('/')[-1]
        response['Content-Type'] = f'application/tar+gzip'
        response['Content-Disposition'] = f'attachment; filename={filename}'
        # clear_tmp()
        return response
    except:
        message = f"Project {project_name} is unavailable or deleted."
        messages.error(request, message)
        return redirect(request.META['HTTP_REFERER'])

    #except:
       # raise Http404()

def igv_features_creation(locations):
    """
    Locations should look like: ["'chr11:56595156-58875237'", " 'chr11:66684707-68055335'", " 'chr11:69975662-70290667'"]

    """
    features = []

    ## for locuses, the structure should be like this:
        ## each key is a chromosome
        ## each value is a constructed focal range, including chr_num, chr_min, chr_max
    locuses = {}
    for location in locations:
        if not location:
            continue
        parsed = location.replace(":", ",").replace("'", "").replace("-", ",").replace(" ", '').split(",")
        chrom = parsed[0]
        start = int(parsed[1])
        end = int(parsed[2])
        features.append({
            'chr':chrom,
            'start':start,
            'end':end,
        })
        if chrom in locuses:
            if start < locuses[chrom]['min']:
                locuses[chrom]['min'] = start
            if end > locuses[chrom]['max']:
                locuses[chrom]['max'] = end
        else:
            locuses[chrom] = {
                'min':start,
                'max':end,

                }

    ## reconstruct locuses
    for key in locuses.keys():
        chr_min = int(locuses[key]['min'])
        chr_max = int(locuses[key]['max'])
        chr_num = key.replace('chr', '')
        locuses[key] = f"{chr_num}:{(int(chr_min)):,}-{(int(chr_max)):,}"

    return features, locuses


def get_sample_metadata(sample_data):
    try:
        sample_metadata_id = sample_data[0]['Sample_metadata_JSON']
        sample_metadata = fs_handle.get(ObjectId(sample_metadata_id)).read()
        sample_metadata = json.loads(sample_metadata.decode())
        # Add metadata from the `extra_metadata_from_csv` field
        extra_metadata = sample_data[0].get('extra_metadata_from_csv', {})
        if isinstance(extra_metadata, dict):
            sample_metadata.update(extra_metadata)
    except Exception as e:
        # logging.exception(e)
        sample_metadata = defaultdict(str)

    return sample_metadata

def sample_metadata_download(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    sample_metadata_id = sample_data[0]['Sample_metadata_JSON']
    extra_metadata = sample_data[0].get('extra_metadata_from_csv', {})
    try:
        sample_metadata = fs_handle.get(ObjectId(sample_metadata_id)).read()
        ##combining
        combination = json.dumps({**json.loads(sample_metadata), **extra_metadata}, indent=2).encode('utf-8')
        response = HttpResponse(combination)
        response['Content-Type'] = 'application/json'
        response['Content-Disposition'] = f'attachment; filename={sample_name}.json'
        # clear_tmp()
        return response

    except Exception as e:
        logging.exception(e)
        return HttpResponse()

def add_metadata(request, project_id):
    return render(request, 'pages/add_metadata.html', {'project_id': project_id})


# @cache_page(600) # 10 minutes
def sample_page(request, project_name, sample_name):
    logging.info(f"Loading sample page for {sample_name}")
    project, sample_data = get_one_sample(project_name, sample_name)
    project_linkid = project['_id']
    if project['private'] and not is_user_a_project_member(project, request):
        return redirect('/accounts/login')
    sample_metadata = get_sample_metadata(sample_data)
    reference_genome = reference_genome_from_sample(sample_data)
    sample_data_processed = preprocess_sample_data(replace_space_to_underscore(sample_data))
    filter_plots = not request.GET.get('display_all_chr')
    all_locuses = []
    igv_tracks = []
    download_png = []
    reference_version = []

    # Check if ec3D visualization is available
    ec3d_available = check_ec3d_available(project['project_name'], sample_name)

    if sample_data_processed[0]['AA_amplicon_number'] == None:
        plot = sample_plot.plot(db_handle, sample_data_processed, sample_name, project_name, filter_plots=filter_plots)

    else:
        plot = sample_plot.plot(db_handle, sample_data_processed, sample_name, project_name, filter_plots=filter_plots)
        for feature in sample_data_processed:
            reference_version.append(feature['Reference_version'])
            download_png.append({
                'aa_amplicon_number': feature['AA_amplicon_number'],
                'download_link': f"//{request.get_host()}/project/{project_linkid}/sample/{sample_name}/feature/{feature['Feature_ID']}/download/png/{feature['AA_PNG_file']}".replace(" ", "_")
            })

            roi_features, locus = igv_features_creation(feature['Location'])
            all_locuses.append(locus)
            # print("Converted location list {} to IGV formatted string {}".format(str(feature['Location']), locus))
            track = {
                'name': feature['Feature_ID'],
                # 'type': "seg",
                # 'url' : f"http://{request.get_host()}/project/{project_linkid}/sample/{sample_name}/feature/{feature['Feature_ID']}/download/{feature['Feature_BED_file']}".replace(" ", "%"),
                # 'indexed':False,
                'color': "rgba(94,255,1,0.25)",
                'features': roi_features,
            }

            igv_tracks.append(track)

            ## use safe encoding
            ## when we embed the django template, we can separate filters, and there's one that's "safe", and will
            ## have the IGV button in the features table
            ## https://docs.djangoproject.com/en/4.1/ref/templates/builtins/#safe
    return render(request, "pages/sample.html",
                  {'project': project,
                   'project_name': project_name,
                   'project_linkid': project_linkid,
                   'sample_data': sample_data_processed,
                   'sample_metadata': sample_metadata,
                   'reference_genome': reference_genome,
                   'sample_name': sample_name,
                   'graph': plot,
                   'igv_tracks': json.dumps(igv_tracks),
                   'locuses': json.dumps(all_locuses),
                   'download_links': json.dumps(download_png),
                   'reference_versions': json.dumps(reference_version),
                   'ec3d_available': ec3d_available,  # New context variable
        }
    )

# Custom JSON encoder to handle any remaining ObjectId
class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)
        return super().default(o)


def create_zip_response(zip_source_dir, filename):
    """
    Create a zip file from a directory and return it as an HTTP response.
    Cleans up both the zip file and the source directory.

    Args:
        zip_source_dir: Directory to zip
        filename: Name of the zip file (without .zip extension)

    Returns:
        HttpResponse with the zip file
    """
    logging.debug("Creating sample download zip file from directory " + zip_source_dir)
    zip_path = f"{filename}.zip"

    try:
        # Create the zip archive
        shutil.make_archive(filename, 'zip', zip_source_dir)

        # Create the response
        with open(zip_path, 'rb') as zip_file:
            response = HttpResponse(zip_file)
            response['Content-Type'] = 'application/x-zip-compressed'
            response['Content-Disposition'] = f'attachment; filename={filename}.zip'

        return response

    finally:
        # Clean up both the zip file and the source directory
        if os.path.exists(zip_path):
            os.remove(zip_path)

        # Clean up the source directory
        if os.path.exists(zip_source_dir):
            try:
                shutil.rmtree(zip_source_dir)
                logging.debug(f"Cleaned up temporary directory: {zip_source_dir}")
            except Exception as e:
                logging.error(f"Failed to clean up directory {zip_source_dir}: {e}")


def process_sample_data(project, sample_name, sample_data, output_dir=None):
    """
    Process sample data and save it to the specified directory or a temporary directory.

    Args:
        project: The project containing the sample
        sample_name: Name of the sample
        sample_data: Sample data to process
        output_dir: Directory to save processed data (or None for a temp directory)

    Returns:
        tuple: (sample_data_path, updated_data) where
            sample_data_path is the directory containing the processed sample data
            updated_data is the processed feature data
    """
    # Track downloads
    if check_if_db_field_exists(project, 'sample_downloads'):
        sample_download_data = project['sample_downloads']
        if isinstance(sample_download_data, int):
            temp_data = sample_download_data
            sample_download_data = dict()
            sample_download_data[get_date_short()] = temp_data
        elif get_date_short() in sample_download_data:
            sample_download_data[get_date_short()] += 1
        else:
            sample_download_data[get_date_short()] = 1
    else:
        sample_download_data = dict()
        sample_download_data[get_date_short()] = 1

    query = {'_id': ObjectId(project['_id'])}
    new_val = {"$set": {'sample_downloads': sample_download_data}}
    collection_handle.update_one(query, new_val)

    # Create directory for files
    if output_dir is None:
        # For single sample download, use default path
        sample_data_path = f"tmp/{project['_id']}/{sample_name}"
    else:
        # For batch download, use provided path
        sample_data_path = output_dir

    os.makedirs(sample_data_path, exist_ok=True)

    # Create new specialized directories
    bed_files_dir = f"{sample_data_path}/{sample_name}_classification_bed_files"
    sashimi_plots_dir = f"{sample_data_path}/{sample_name}_sashimi_plots"

    os.makedirs(bed_files_dir, exist_ok=True)
    os.makedirs(sashimi_plots_dir, exist_ok=True)

    # Get and save sample metadata
    try:
        sample_metadata = get_sample_metadata(sample_data)
        with open(f'{sample_data_path}/{sample_name}_sample_metadata.json', 'w') as metadata_file:
            json.dump(sample_metadata, metadata_file, indent=2)
        metadata_file_path = f"{sample_name}_sample_metadata.json"
    except Exception as e:
        logging.exception(e)
        metadata_file_path = "Not Provided"

    # Process sample data
    sample_data_processed = preprocess_sample_data(replace_space_to_underscore(sample_data))
    updated_data = []
    cnv_file_processed = False

    # Process feature files (existing logic from sample_download)
    for feature in sample_data_processed:
        # Create a copy of the feature to update paths
        updated_feature = feature.copy()

        # Update the Sample_metadata_JSON to reference the new file
        if 'Sample_metadata_JSON' in updated_feature:
            updated_feature['Sample_metadata_JSON'] = metadata_file_path

        # Set up file system
        feature_id = feature['Feature_ID']
        amplicon_number = feature['AA_amplicon_number']

        # Updates paths to use the new directory structure
        bed_file_path = f"{sample_name}_classification_bed_files/{feature_id}.bed"
        pdf_file_path = f"{sample_name}_sashimi_plots/{sample_name}_amplicon{amplicon_number}.pdf"
        png_file_path = f"{sample_name}_sashimi_plots/{sample_name}_amplicon{amplicon_number}.png"
        cnv_file_path = f"{sample_name}_CNV_CALLS.bed"

        # Get object ids
        if feature['Feature_BED_file'] != 'Not Provided':
            bed_id = feature['Feature_BED_file']
            # Update the path in the feature copy
            updated_feature['Feature_BED_file'] = bed_file_path
        else:
            bed_id = False

        # CNV file is at the sample level
        if feature.get('CNV_BED_file', 'Not Provided') != 'Not Provided':
            cnv_id = feature['CNV_BED_file']
            # All features reference the same CNV file at the sample level
            updated_feature['CNV_BED_file'] = cnv_file_path
        else:
            cnv_id = False

        if feature.get('AA_PDF_file', 'Not Provided') != 'Not Provided':
            pdf_id = feature['AA_PDF_file']
            # Update the path to use amplicon number in the filename
            updated_feature['AA_PDF_file'] = pdf_file_path
        else:
            pdf_id = False

        if feature.get('AA_PNG_file', 'Not Provided') != 'Not Provided':
            png_id = feature['AA_PNG_file']
            # Update the path to use amplicon number in the filename
            updated_feature['AA_PNG_file'] = png_file_path
        else:
            png_id = False

        if feature.get('AA_directory', 'Not Provided') != 'Not Provided':
            aa_directory_id = feature['AA_directory']
            # Update the path in the feature copy
            updated_feature['AA_directory'] = f"aa_directory.tar.gz"
        else:
            aa_directory_id = False

        if feature.get('cnvkit_directory', 'Not Provided') != 'Not Provided':
            cnvkit_directory_id = feature['cnvkit_directory']
            # Update the path in the feature copy
            updated_feature['cnvkit_directory'] = f"cnvkit_directory.tar.gz"
        else:
            cnvkit_directory_id = False

        # Add the updated feature to our list
        updated_data.append(updated_feature)

        # Get files from gridfs
        if bed_id is not None and bed_id:
            if not ObjectId.is_valid(bed_id):
                logging.debug(
                    "Sample: " + sample_name + ", Feature: " + feature_id + ", BED_ID is ->" + str(bed_id) + " <-")
                break

            bed_file = fs_handle.get(ObjectId(bed_id)).read()
            with open(f'{bed_files_dir}/{feature_id}.bed', "wb+") as bed_file_tmp:
                bed_file_tmp.write(bed_file)

        # Only process the CNV file once for the whole sample
        if cnv_id and not cnv_file_processed:
            cnv_file = fs_handle.get(ObjectId(cnv_id)).read()
            with open(f'{sample_data_path}/{sample_name}_CNV_CALLS.bed', "wb+") as cnv_file_tmp:
                cnv_file_tmp.write(cnv_file)
            cnv_file_processed = True

        if pdf_id:
            pdf_file = fs_handle.get(ObjectId(pdf_id)).read()
            # Save the PDF in the sashimi plots directory
            with open(f'{sashimi_plots_dir}/{sample_name}_amplicon{amplicon_number}.pdf', "wb+") as pdf_file_tmp:
                pdf_file_tmp.write(pdf_file)

        if png_id:
            png_file = fs_handle.get(ObjectId(png_id)).read()
            # Save the PNG in the sashimi plots directory
            with open(f'{sashimi_plots_dir}/{sample_name}_amplicon{amplicon_number}.png', "wb+") as png_file_tmp:
                png_file_tmp.write(png_file)

        if aa_directory_id:
            if not os.path.exists(f'{sample_data_path}/aa_directory.tar.gz'):
                aa_directory_file = fs_handle.get(ObjectId(aa_directory_id)).read()
                with open(f'{sample_data_path}/aa_directory.tar.gz', "wb+") as aa_directory_tmp:
                    aa_directory_tmp.write(aa_directory_file)

        if cnvkit_directory_id:
            if not os.path.exists(f'{sample_data_path}/cnvkit_directory.tar.gz'):
                cnvkit_directory_file = fs_handle.get(ObjectId(cnvkit_directory_id)).read()
                with open(f'{sample_data_path}/cnvkit_directory.tar.gz', "wb+") as cnvkit_directory_tmp:
                    cnvkit_directory_tmp.write(cnvkit_directory_file)

    # Generate JSON file using the updated data
    with open(f'{sample_data_path}/{sample_name}_result_data.json', 'w') as json_file:
        json.dump(updated_data, json_file, indent=2, cls=JSONEncoder)

    # Generate TSV file using the updated data
    with open(f'{sample_data_path}/{sample_name}_result_data.tsv', 'w') as tsv_file:
        # Define the column order for the first four columns
        ordered_columns = ['Sample_name', 'AA_amplicon_number', 'Feature_ID', 'Classification']

        # Get all column names from the data
        all_columns = set()
        for feature in updated_data:
            all_columns.update(feature.keys())

        # Sort remaining columns alphabetically
        remaining_columns = sorted(list(all_columns - set(ordered_columns)))

        # Final column order
        columns = ordered_columns + remaining_columns

        # Write header
        tsv_file.write('\t'.join(columns) + '\n')

        # Write data rows
        for feature in updated_data:
            row = []
            for col in columns:
                val = feature.get(col, '')
                # Convert ObjectId to string if needed
                if isinstance(val, ObjectId):
                    val = str(val)
                row.append(str(val))
            tsv_file.write('\t'.join(row) + '\n')

    return sample_data_path, updated_data


def sample_download(request, project_name, sample_name):
    """
    Download a single sample's data.
    """
    project, sample_data = get_one_sample(project_name, sample_name)

    # Process the sample data
    sample_data_path, _ = process_sample_data(project, sample_name, sample_data)

    # Create and return the response
    return create_zip_response(sample_data_path, sample_name)


@login_required(login_url='/accounts/login/')
def batch_sample_download(request):
    """
    Download multiple samples organized by project.
    """
    if request.method != 'POST':
        alert_message = "Invalid request method. Please use the selection checkboxes to choose samples."
        return redirect('search_page', alert_message=alert_message)

    samples = request.POST.getlist('samples')

    if not samples:
        alert_message = "No samples were selected. Please select at least one sample to download."
        return redirect('search_page', alert_message=alert_message)

    if len(samples) > 1000:
        alert_message = "Too many samples selected. Please download relevant projects directly."
        return redirect('search_page', alert_message=alert_message)

    # Create a temporary directory for the batch
    batch_id = uuid.uuid4()
    batch_dir = f"tmp/batch_{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    # Group samples by project
    projects_and_samples = {}

    for sample_str in samples:
        try:
            project_id, sample_name = sample_str.split(':')

            # Skip if no access
            project = get_one_project(project_id)
            if project['private'] and not is_user_a_project_member(project, request):
                continue

            # Add to our grouping
            if project_id not in projects_and_samples:
                projects_and_samples[project_id] = {
                    'project': project,
                    'samples': []
                }

            projects_and_samples[project_id]['samples'].append(sample_name)

        except (ValueError, Exception) as e:
            logging.exception(f"Error processing sample string {sample_str}: {e}")
            continue

    try:
        # Process each project's samples
        for project_id, project_info in projects_and_samples.items():
            project = project_info['project']
            project_dir = f"{batch_dir}/{project['project_name']}"
            os.makedirs(project_dir, exist_ok=True)

            # Process each sample in the project
            for sample_name in project_info['samples']:
                try:
                    # Get sample data
                    _, sample_data = get_one_sample(project_id, sample_name)
                    if not sample_data:
                        continue

                    # Process the sample
                    sample_dir = f"{project_dir}/{sample_name}"
                    process_sample_data(project, sample_name, sample_data, sample_dir)

                except Exception as e:
                    logging.exception(f"Error processing sample {sample_name}: {e}")
                    continue

        # Create the zip file with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"batch_samples_{timestamp}"

        # Return the response
        return create_zip_response(batch_dir, zip_filename)

    finally:
        # Clean up the temporary directory
        if os.path.exists(batch_dir):
            shutil.rmtree(batch_dir)


def feature_page(request, project_name, sample_name, feature_name):
    project, sample_data, feature = get_one_feature(project_name,sample_name, feature_name)
    feature_data = replace_space_to_underscore(feature)
    return render(request, "pages/feature.html", {'project': project, 'sample_name': sample_name, 'feature_name': feature_name, 'feature' : feature_data})


def feature_download(request, project_name, sample_name, feature_name, feature_id):
    bed_file = fs_handle.get(ObjectId(feature_id)).read()
    response = HttpResponse(bed_file, content_type='application/caper.bed+csv')
    response['Content-Disposition'] = f'attachment; filename="{feature_name}.bed"'
    return response


def pdf_download(request, project_name, sample_name, feature_name, feature_id):
    img_file = fs_handle.get(ObjectId(feature_id)).read()
    response = HttpResponse(img_file, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{feature_name}.pdf"'
    # response = FileResponse(img_file)
    return response


def png_download(request, project_name, sample_name, feature_name, feature_id):
    img_file = fs_handle.get(ObjectId(feature_id)).read()
    response = HttpResponse(img_file, content_type='image/png')
    response['Content-Disposition'] = f'inline; filename="{feature_name}.png"'
    return response


#
# actually the old gene search page function, deprecated and replaced
#
# def class_search_page(request):
#     genequery = request.GET.get("genequery")
#     genequery = genequery.upper()
#     gen_query = {'$regex': genequery }
#
#     classquery = request.GET.get("classquery")
#     classquery = classquery.upper()
#     class_query = {'$regex': classquery}
#
#     # Gene Search
#     if request.user.is_authenticated:
#         username = request.user.username
#         useremail = request.user.email
#         private_projects = list(collection_handle.find({'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}], 'Oncogenes' : gen_query, 'Classification' : class_query}))
#     else:
#         private_projects = []
#
#     public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'Classification' : class_query}))
#
#     for proj in private_projects:
#         prepare_project_linkid(proj)
#     for proj in public_projects:
#         prepare_project_linkid(proj)
#
#     sample_data = []
#     for project in public_projects:
#         project_name = project['project_name']
#         features = project['runs']
#         features_list = replace_space_to_underscore(features)
#         data = sample_data_from_feature_list(features_list)
#         for sample in data:
#             sample['project_name'] = project_name
#             if genequery in sample['Oncogenes']:
#                 sample_data.append(sample)
#
#     return render(request, "pages/gene_search.html", {'public_projects': public_projects, 'private_projects' : private_projects, 'sample_data': sample_data, 'query': query})
#

def gene_search_page(request):
    genequery = request.GET.get("genequery")
    if genequery:
        genequery = genequery.upper()
        gen_query = {'$regex': genequery}
    else:
        genequery = ""
        gen_query = {'$regex': ''}

    logging.debug("Performing gene search")

    classquery = request.GET.get("classquery", "")
    if classquery:
        classquery = classquery.upper()
        class_query = {'$regex': classquery}
    else:
        class_query = {'$regex': ''}

    # Get the combined cancer/tissue field
    metadata_cancer_tissue = request.GET.get("metadata_cancer_tissue", "")

    # Gene Search
    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email
        query_obj = {'private': True, "$or": [{"project_members": username}, {"project_members": useremail}],
                     'Oncogenes': gen_query, 'delete': False}

        private_projects = list(collection_handle.find(query_obj))
        # private_projects = get_projects_close_cursor(query_obj)
    else:
        private_projects = []

    public_projects = list(collection_handle.find({'private': False, 'Oncogenes': gen_query, 'delete': False}))
    # public_projects = get_projects_close_cursor({'private' : False, 'Oncogenes' : gen_query, 'delete': False})

    for proj in private_projects:
        prepare_project_linkid(proj)
    for proj in public_projects:
        prepare_project_linkid(proj)

    def collect_class_data(projects):
        sample_data = []
        for project in projects:
            project_name = project['project_name']
            project_linkid = project['_id']
            features = project['runs']
            features_list = replace_space_to_underscore(features)
            data = sample_data_from_feature_list(features_list)

            for sample in data:
                sample['project_name'] = project_name
                sample['project_linkid'] = project_linkid

                # Gene and classification checks
                gene_match = (genequery in sample['Oncogenes'] or len(genequery) == 0)
                upperclass = list(map(str.upper, sample['Classifications']))
                class_match = (classquery in upperclass or len(classquery) == 0)

                # Cancer type or tissue of origin check
                cancer_tissue_match = True  # Default to True if no filter
                if metadata_cancer_tissue:
                    cancer_type = sample.get('Cancer_type', '').lower()
                    tissue_origin = sample.get('Tissue_of_origin', '').lower()
                    cancer_tissue_match = (
                            metadata_cancer_tissue.lower() in cancer_type or
                            metadata_cancer_tissue.lower() in tissue_origin
                    )

                # Only add the sample if all filters match
                if gene_match and class_match and cancer_tissue_match:
                    sample_data.append(sample)

        return sample_data

    public_sample_data = collect_class_data(public_projects)
    private_sample_data = collect_class_data(private_projects)

    # for display on the results page
    if len(classquery) == 0:
        classquery = "all amplicon types"

    return render(request, "pages/gene_search.html",
                  {'public_projects': public_projects, 'private_projects': private_projects,
                   'public_sample_data': public_sample_data, 'private_sample_data': private_sample_data,
                   'gene_query': genequery, 'class_query': classquery,
                   'query_info': {
                       "Project Name": request.GET.get("project_name", ""),
                       "Sample Name": request.GET.get("metadata_sample_name", ""),
                       "Gene": genequery,
                       "Classification": classquery,
                       "Sample Type": request.GET.get("metadata_sample_type", ""),
                       "Cancer Type or Tissue": metadata_cancer_tissue
                   }})


# def gene_search_download(request, project_name):
#     project = get_one_project(project_name)
#     samples = project['runs']
#     for sample in samples:
#         if len(samples[sample]) > 0:
#             for feature in samples[sample]:
#                 # set up file system
#                 feature_id = feature['Feature_ID']
#                 feature_data_path = f"tmp/{project_name}/{feature['Sample_name']}/{feature_id}"
#                 os.makedirs(feature_data_path, exist_ok=True)
#                 # get object ids
#                 bed_id = feature['Feature_BED_file']
#                 cnv_id = feature['CNV_BED_file']
#                 pdf_id = feature['AA_PDF_file']
#                 png_id = feature['AA_PNG_file']
#
#                 # get files from gridfs
#                 bed_file = fs_handle.get(ObjectId(bed_id)).read()
#                 cnv_file = fs_handle.get(ObjectId(cnv_id)).read()
#                 pdf_file = fs_handle.get(ObjectId(pdf_id)).read()
#                 png_file = fs_handle.get(ObjectId(png_id)).read()
#
#                 # send files to tmp file system
#                 with open(f'{feature_data_path}/{feature_id}.bed', "wb+") as bed_file_tmp:
#                     bed_file_tmp.write(bed_file)
#                 with open(f'{feature_data_path}/{feature_id}_CNV.bed', "wb+") as cnv_file_tmp:
#                     cnv_file_tmp.write(cnv_file)
#                 with open(f'{feature_data_path}/{feature_id}.pdf', "wb+") as pdf_file_tmp:
#                     pdf_file_tmp.write(pdf_file)
#                 with open(f'{feature_data_path}/{feature_id}.png', "wb+") as png_file_tmp:
#                     png_file_tmp.write(png_file)
#
#     project_data_path = f"tmp/{project_name}/"
#     shutil.make_archive(f'{project_name}', 'zip', project_data_path)
#     zip_file_path = f"{project_name}.zip"
#     with open(zip_file_path, 'rb') as zip_file:
#         response = HttpResponse(zip_file)
#         response['Content-Type'] = 'application/x-zip-compressed'
#         response['Content-Disposition'] = f'attachment; filename={project_name}.zip'
#     os.remove(f'{project_name}.zip')
#     return response


def get_current_user(request):
    current_user = request.user.username
    try:
        if current_user.email:
            current_user = request.user.email
        else:
            current_user = request.user.username
    except:
        current_user = request.user.username

    return current_user


def project_delete(request, project_name):
    project = get_one_project(project_name)
    deleter = get_current_user(request)
    if check_project_exists(project_name) and is_user_a_project_member(project, request):
        current_runs = project['runs']
        query = {'_id': project['_id']}
        #query = {'project_name': project_name}
        new_val = { "$set": {'delete' : True, 'delete_user': deleter, 'delete_date': get_date()} }
        collection_handle.update_one(query, new_val)
        delete_project_from_site_statistics(project, project['private'])
        return redirect('profile')
    else:
        return HttpResponse("Project does not exist")
    # return redirect('profile')

def project_update(request, project_name):
    """
    Updates the 'current' field for a project that has been updated
    current = False when a project is edited.
    update_date will be changed after project has been updated.
    """
    project = get_one_project(project_name)
    if check_project_exists(project_name) and is_user_a_project_member(project, request):
        query = {'_id': project['_id']}
        ## 2 new fields: current, and update_date, $set will add a new field with the specified value.
        new_val = { "$set": {'current' : False, 'update_date': get_date()} }
        collection_handle.update_one(query, new_val)

        return redirect('profile')
    else:
        return HttpResponse("Project does not exist")


def download_file(url, save_path):
    # Send a GET request to the URL
    response = requests.get(url)

    # Raise an error for bad status codes
    response.raise_for_status()

    # Write the content to the specified file location
    with open(save_path, 'wb') as file:
        file.write(response.content)

    print(f"File downloaded successfully and saved to {save_path}")


def edit_project_page(request, project_name):
    if request.method == "GET":
        project = get_one_project(project_name)
    if request.method == "POST":
        try:
            metadata_file = request.FILES.get("metadataFile")
        except Exception as e:
            print(f'Failed to get the metadata file from the form')
            print(e)
        project = get_one_project(project_name)
        old_alias_name = None
        if 'alias_name' in project:
            old_alias_name = project['alias_name']
            print(f'THE OLD ALIAS NAME SHOULD BE: {old_alias_name}')
        # no edits for non-project members
        if not is_user_a_project_member(project, request):
            return HttpResponse("Project does not exist")
        form = UpdateForm(request.POST, request.FILES)
        ## give the new project the old project alias.
        if form.data['alias'] == '':
            if old_alias_name:
                mutable_data = form.data.copy()  # Make a mutable copy of the form's data
                mutable_data['alias'] = old_alias_name  # Set the alias to the new value
                form.data = mutable_data
                ## update old project so its alias is set to None, and the alias is set to the new project
                query = {'_id': ObjectId(project_name)}
                new_val = { "$set": {'alias_name' : None}}
                collection_handle.update_one(query, new_val)
        ## new project information is stored in form_dict
        form_dict = form_to_dict(form)
        form_dict['project_members'] = create_user_list(form_dict['project_members'], get_current_user(request))
        # lets notify users (if their preferences request it) if project membership has changed
        new_membership = form_dict['project_members']
        old_membership = project['project_members']
        old_privacy = project['private']
        new_privacy = form_dict['private']
        
        try:
            notify_users_of_project_membership_change(request.user, old_membership, new_membership, project['project_name'], project['_id'])
        except:
            print("Failed to notify users of project membership change")
            #error_message = "Failed to notify users of project membership change. Please check your email settings."

        ## check multi files, send files to GP and run aggregator there:
        file_fps = []
        temp_proj_id = uuid.uuid4().hex ## to be changed
        try:
            files = request.FILES.getlist('document')
            project_data_path = f"tmp/{temp_proj_id}" ## to change
            for file in files:
                fs = FileSystemStorage(location = project_data_path)
                saved = fs.save(file.name, file)
                print(f'file: {file.name} is saved')
                fp = os.path.join(project_data_path, file.name)
                file_fps.append(file.name)
                file.close()

            ## download old project file here and run it through aggregator
            ## build download URL
            url = f'http://127.0.0.1:8000/project/{project["linkid"]}/download'
            download_path = project_data_path+'/download.tar.gz'
            try:
                ## try to download old project file
                print(f"PREVIOUS FILE FPS LIST: {file_fps}")
                ### if replace project, don't download old project
                try:
                    if request.POST['replace_project'] == 'on':
                        print('Replacing project with new uploaded file')
                except:
                    download = download_file(url, download_path)
                    file_fps.append(os.path.join('download.tar.gz'))
                # print(f"AFTERS FILE FPS LIST: {file_fps}")
                # print(f'aggregating on: {file_fps}')
                temp_directory = os.path.join('./tmp/', str(temp_proj_id))
                agg = Aggregator(file_fps, temp_directory, project_data_path, 'No', "", 'python3', uuid=str(temp_proj_id))
                if not agg.completed:
                    ## redirect to edit page if aggregator fails
                    alert_message = "Edit project failed. Please ensure all uploaded samples have the same reference genome and are valid AmplionSuite results."
                    return render(request, 'pages/edit_project.html',
                              {'project': project,
                               'run': form,
                               'alert_message': alert_message,
                               'all_alias' :get_all_alias()})
                ## after running aggregator, replace the requests file with the aggregated file:
                with open(agg.aggregated_filename, 'rb') as f:
                    uploaded_file = SimpleUploadedFile(
                    name=os.path.basename(agg.aggregated_filename),
                    content=f.read(),
                    content_type='application/gzip'
                    )
                    request.FILES['document'] = uploaded_file
                f.close()
            except:
                ## download failed, don't run aggregator
                print(f'download failed ... ')
        except:
            print('no file uploaded')
        try:
            request_file = request.FILES['document']
        except:
            request_file = None

        if request_file is not None:
            ## save all files, run through aggregator.
            # mark the current project as updated
            update_project = project_update(request, project_name)
            # mark current project as deleted as well
            delete_project = project_delete(request, project_name)

            ## get list of previous versions before this and insert it along with the _create project function .
            new_prev_versions = []
            if 'previous_versions' in project:
                new_prev_versions = project['previous_versions']

            ## update for current
            new_prev_versions.append(
                {
                    'date':str(project['date']),
                    'linkid':str(project['linkid'])
                }
            )

            views = project['views']
            downloads = project['downloads']
            # create a new one with the new form
            extra_metadata_file_fp = save_metadata_file(request, project_data_path)
            ## get extra metadata from csv first (if exists in old project), add it to the new proj
            new_id = _create_project(form, request, extra_metadata_file_fp, previous_versions = new_prev_versions, previous_views = [views, downloads])
            if new_id is not None:
                # go to the new project
                return redirect('project_page', project_name=new_id.inserted_id)
            else:
                alert_message = "The input file was not a valid aggregation. Please see site documentation."
                return render(request, 'pages/edit_project.html',
                              {'project': project,
                               'run': form,
                               'alert_message': alert_message,
                               'all_alias' :json.dumps(get_all_alias())})
        # JTL 081823 Not sure what these next 4 lines are about?  An earlier plan to change the project file?
        # leaving them alone for now but they smell like dead code
        if 'file' in form_dict:
            runs = samples_to_dict(form_dict['file'])
        else:
            runs = 0

        if check_project_exists(project_name):
            new_project_name = form_dict['project_name']

            logging.info(f"project name: {project_name}  change to {new_project_name}")
            current_runs = project['runs']

            if runs != 0:
                current_runs.update(runs)
            query = {'_id': ObjectId(project_name)}
            try:
                alias_name = form_dict['alias']
                # print(alias_name)
            except:
                print('no alias to be found')

            ## try to get metadata file:

            if metadata_file:
                current_runs = process_metadata_no_request(current_runs, metadata_file=metadata_file)
            new_val = { "$set": {'project_name':new_project_name, 'runs' : current_runs,
                                 'description': form_dict['description'], 'date': get_date(),
                                 'private': form_dict['private'],
                                 'project_members': form_dict['project_members'],
                                 'publication_link': form_dict['publication_link'],
                                 'Oncogenes': get_project_oncogenes(current_runs),
                                 'alias_name' : alias_name}}
            
            # After form_dict is created and before new_val is defined
            for version_field in ['ASP_version', 'AA_version', 'AC_version']:
                value = form.cleaned_data.get(version_field, 'NA').strip()
                if value and value != 'NA':
                    # Add to new_val to be saved in mongo
                    new_val.setdefault('$set', {})[version_field] = value
            
            if form.is_valid():
                collection_handle.update_one(query, new_val)
                edit_proj_privacy(project, old_privacy, new_privacy)
                logging.debug("Updated collection_handle with new data")
                return redirect('project_page', project_name=project_name)
            else:
                raise Http404()

        else:
            return HttpResponse("Project does not exist")
    else:
        # get method handling
        project = get_one_project(project_name)
        prev_versions, prev_ver_msg = previous_versions(project)
        if prev_ver_msg:
            messages.error(request, "Redirected to latest version, editing of old versions not allowed. ")
            return redirect('project_page', project_name = prev_versions[0]['linkid'])

        # split up the project members and remove the empties
        members = project['project_members']
        try:
            publication_link = project['publication_link']
        except KeyError:
            publication_link = None
        members = [i for i in members if i]
        memberString = ', '.join(members)

        
        AAVersion=project.get('AA_version', 'NA')
        ACVersion=project.get('AC_version', 'NA')
        ASPVersion=project.get('ASP_version', 'NA')
        
        form = UpdateForm(initial={"project_name": project['project_name'],
                                   "description": project['description'],
                                   "private":project['private'],
                                   "project_members": memberString,
                                   "publication_link": publication_link,  
                                   "AA_version": AAVersion,
                                   "ASP_version": ASPVersion,
                                   "AC_version": ACVersion })

    return render(request, "pages/edit_project.html",
                  {'project': project,
                   'run': form,
                   'all_alias' :json.dumps(get_all_alias())})



def clear_tmp(folder = 'tmp/'):
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logging.exception('Failed to delete %s. Reason: %s' % (file_path, e))


def update_notification_preferences(request):

    if request.method == "POST":
        form = UserPreferencesForm(request.POST)
        form_dict = form_to_dict(form)
        update_user_preferences(request.user, form_dict)

    return profile(request, message_to_user="User preferences updated.")

# only allow users designated as staff to see this, otherwise redirect to nonexistant page to
# deny that this might even be a valid URL
@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_featured_projects(request):
    if not  request.user.is_staff:
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
    # public_projects = get_projects_close_cursor({'private': False, 'delete': False})
    for proj in public_projects:
        prepare_project_linkid(proj)

    return render(request, 'pages/admin_featured_projects.html', {'public_projects': public_projects})

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_version_details(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    try:
        #details = [{"name":"version","value":"test"},{"name":"creator","value":"someone"},{"name": "date", "value":"whenever" }]
        details = []
        comment_char="#"
        sep="="
        with open("version.txt", 'r') as version_file:
            for line in version_file:
                l = line.strip()
                if l and not l.startswith(comment_char):
                    key_value = l.split(sep)
                    key = key_value[0].strip()
                    value = sep.join(key_value[1:]).strip().strip('"')
                    details.append({"name": key,  "value": value})
    except:
        details = [{"name":"version","value":"unknown"},{"name":"creator","value":"unknown"},{"name": "date", "value":"unknown" }]

    env_to_skip = ['DB_URI_SECRET', "GOOGLE_SECRET", "GLOBUS_SECRET"]
    env=[]
    for key, value in os.environ.items():
        if not ("SECRET" in key) and not key in env_to_skip:
            env.append({"name": key, "value": value})

    try:
        gitcmd = 'export GIT_DISCOVERY_ACROSS_FILESYSTEM=1;git config --global --add safe.directory /srv;git status;echo \"Commit id:\"; git rev-parse HEAD'
        git_result = subprocess.check_output(gitcmd, shell=True)
        git_result = git_result.decode("UTF-8")\
        #.replace("\n", "<br/>")
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
            if not(( "SECRET" in line_enc.upper()) or ( "mongodb" in line_enc)):
                settings_result = settings_result + line_enc + "\n"

    except:
        settings_result="An error occurred getting the contents of settings.py."

    #settings_result = "Get settings call failed."

    return render(request, 'pages/admin_version_details.html', {'details': details, 'env':env, 'git': git_result, 'django_settings': settings_result})

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_sendemail(request):
    if not  request.user.is_staff:
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

        #send_mail(subject = subject, message = body, from_email = settings.EMAIL_HOST_USER_SECRET, recipient_list = [settings.RECIPIENT_ADDRESS])
        email = EmailMessage(
            subject,
            html_message,
            settings.EMAIL_HOST_USER,
            [to ],
            [cc],
            reply_to=[settings.EMAIL_HOST_USER]
        )
        email.content_subtype = "html"
        email.send(fail_silently=False)

        message_to_user= "Email sent"


    return render(request, 'pages/admin_sendemail.html', {'message_to_user': message_to_user, 'user': request.user, 'SITE_TITLE': settings.SITE_TITLE })



@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def admin_stats(request):
    if not  request.user.is_staff:
        return redirect('/accounts/logout')

    # Get all user data
    User = get_user_model()
    users = User.objects.all()

    # Get public and private project data
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    # public_projects = get_projects_close_cursor({'private': False, 'delete': False})
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
                new_val = { "$set": {'project_downloads': project_download_data} }
                collection_handle.update_one(query, new_val)
        if check_if_db_field_exists(proj, 'sample_downloads'):
            sample_download_data = proj['sample_downloads']
            if isinstance(sample_download_data, int):
                if sample_download_data > 0:
                    temp_data = sample_download_data
                    sample_download_data = dict()
                    sample_download_data[get_date_short()] = temp_data
                    proj_id = proj['_id']
                    query = {'_id': ObjectId(proj_id)}
                    new_val = { "$set": {'sample_downloads': sample_download_data} }
                    collection_handle.update_one(query, new_val)
    # Calculate stats
    # total_downloads = [project['project_downloads'] for project in public_projects]

    for project in public_projects:
        if 'project_downloads' in project:
            project['project_downloads_sum'] = sum(project['project_downloads'].values())
        else:
            project['project_downloads_sum'] = 0

        if 'sample_downloads' in project:
            project['sample_downloads_sum'] = sum(project['sample_downloads'].values())
        else:
            project['sample_downloads_sum'] = 0

    repo_stats = get_latest_site_statistics()

    return render(request, 'pages/admin_stats.html', {'public_projects': public_projects, 'users': users, 'site_stats':repo_stats })

# @user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def user_stats_download(request):
    # user = authenticate(username=os.getenv('ADMIN_USER_SECRET'),password=os.getenv('ADMIN_PASSWORD_SECRET'))
    # if not user.is_staff:
    #     return redirect('/accounts/logout')

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
        user_dict = {'username':user.username,'email':user.email,'date_joined':user.date_joined,'last_login':user.last_login}
        user_data.append(user_dict)

    writer = csv.writer(response)
    keys = ['username','email','date_joined','last_login']
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

# @user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def project_stats_download(request):
    # user = authenticate(username=os.getenv('ADMIN_USER_SECRET'),password=os.getenv('ADMIN_PASSWORD_SECRET'))
    # if not user.is_staff:
    #     return redirect('/accounts/logout')

    # Get public and private project data

    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    # public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    # public_projects = get_projects_close_cursor({'private': False, 'delete': False})
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
    keys = ['project_name','description','project_members','date_created','project_downloads','project_downloads_sum','sample_downloads','sample_downloads_sum']
    writer.writerow(keys)
    for dictionary in public_projects:
        output = {k: dictionary.get(k, None) for k in keys}
        writer.writerow(output.values())
    return response

# extract_project_files is meant to be called in a seperate thread to reduce the wait
# for users as they create the project
def extract_project_files(tarfile, file_location, project_data_path, project_id, extra_metadata_filepath):
    t_sa = time.time()
    logging.debug("Extracting files from tar")
    try:
        with tarfile.open(file_location, "r:gz") as tar_file:
            tar_file.extractall(path=project_data_path)

        # get run.json
        run_path = f'{project_data_path}/results/run.json'
        with open(run_path, 'r') as run_json:
           runs = samples_to_dict(run_json)
        # get cnv, image, bed files
        for sample, features in runs.items():
            logging.debug(f"Extracting {str(len(features))} features from {features[0]['Sample name']}")
            for feature in features:
                # logging.debug(feature['Sample name'])
                if len(feature) > 0:
                    # get paths
                    key_names = ['Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file', 'Sample metadata JSON',
                                 'AA directory', 'cnvkit directory']
                    for k in key_names:
                        try:
                            path_var = feature[k]
                            with open(f'{project_data_path}/results/{path_var}', "rb") as file_var:
                                id_var = fs_handle.put(file_var)
                        except:
                            id_var = "Not Provided"
                        feature[k] = id_var

        # Now update the project with the updated runs
        project = get_one_project(project_id)
        query = {'_id': ObjectId(project_id)}
        if extra_metadata_filepath:
            runs = process_metadata_no_request(replace_underscore_keys(runs), file_path=extra_metadata_filepath)

        new_val = {"$set": {'runs': runs,
                            'Oncogenes': get_project_oncogenes(runs)}}
        
        # logging.error("project is "+ str(project))
        
        get_tool_versions(project, runs)
        version_keys = ['AA_version', 'AC_version', 'ASP_version']
        tool_versions = {k: project[k] for k in version_keys if k in project}
        
        
        new_val["$set"].update(tool_versions)

        collection_handle.update_one(query, new_val)
        t_sb = time.time()
        diff = t_sb - t_sa


        finish_flag = {
            "$set" : {
                'FINISHED?' : True
            }
        }
        collection_handle.update_one(query, finish_flag)
        logging.info(f"Finished extracting from tar in {str(diff)} seconds")

    except Exception as anError:
        logging.error("Error occurred extracting project tarfile results into "+ project_data_path)
        logging.error(type(anError))  # the exception type
        logging.error(anError.args)  # arguments stored in .args
        logging.error(anError)
        # print error to file called project_extraction_errors.txt that we can
        # see and let owner know to contact an admin
        with open(project_data_path + '/project_extraction_errors.txt', 'a') as fh:
            print("Error occurred extracting project tarfile results into " + project_data_path, file = fh)
            print(type(anError), file = fh)  # the exception type
            print(anError.args, file = fh )  # arguments stored in .args
            print(anError, file=fh)

    finish_flag = f"{project_data_path}/results/finished_project_creation.txt"
    with open(finish_flag, 'w') as finish_flag_file:
        finish_flag_file.write("FINISHED")
    finish_flag_file.close()


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
                key_names = ['Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file', 'AA directory', 'cnvkit directory']
                for k in key_names:
                    try:
                        fs_handle.delete(ObjectId(sample[k]))

                    except:
                        # DO NOTHING, its not there
                        id_var = "Not Provided"
    except:
        logging.exception('Problem deleting sample files from Mongo.')
        error_message="Problem deleting sample files from Mongo."

    # delete project tar and files from mongo and local disk
    #    - assume all feature and sample files are in this dir
    try:
        fs_handle.delete(ObjectId(project['tarfile']))
    except KeyError:
        logging.exception(f'Problem deleting project tar file from mongo. { project["project_name"]}')
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
            s3client.delete_object(Bucket=settings.S3_DOWNLOADS_BUCKET,Key=s3_file_path)
        except:
            logging.exception(f'Problem deleting tar file from S3. {s3_file_path}')
            error_message = error_message+" Problem deleting tar file from S3. "
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



# only allow users designated as staff to see this, otherwise redirect to nonexistant page to
# deny that this might even be a valid URL
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
            solo_projects = list(collection_handle.find({ 'current': True, 'project_members': [username] }))
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
            solo_projects = list(collection_handle.find({'project_members': [username]}))
            
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
            member_projects=[]
                
    
    return render(request, 'pages/admin_delete_user.html',
                      {'username': username,
                          'solo_projects': solo_projects, 
                       'member_projects': member_projects ,
                       'error_message': error_message})




# only allow users designated as staff to see this, otherwise redirect to nonexistant page to
# deny that this might even be a valid URL
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


    deleted_projects = list(collection_handle.find({'delete': True, 'current' : True}))
    # deleted_projects = get_projects_close_cursor({'delete': True, 'current' : True})
    for proj in deleted_projects:
        prepare_project_linkid(proj)
        try:
            tar_file_len = fs_handle.get(ObjectId(proj['tarfile'])).length
            proj['tar_file_len'] = sizeof_fmt(tar_file_len)
            if proj['delete_date']:
                dt = datetime.datetime.strptime(proj['delete_date'], f"%Y-%m-%dT%H:%M:%S")
                proj['delete_date'] = (dt.strftime(f'%B %d, %Y %I:%M:%S %p %Z'))
        except:
            #ignore missing date
            logging.warning(proj['project_name'] + " missing date")

    return render(request, 'pages/admin_delete_project.html', {'deleted_projects': deleted_projects, 'error_message':error_message})


def sizeof_fmt(num, suffix="B"):
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


def create_project(request):
    if request.method == "POST":
        ## preprocess request
        # request = preprocess(request)

        form = RunForm(request.POST)
        if not form.is_valid():
            raise Http404()
        ## check multi files, send files to GP and run aggregator there:
        file_fps = []
        temp_proj_id = uuid.uuid4().hex ## to be changed
        files = request.FILES.getlist('document')
        project_data_path = f"tmp/{temp_proj_id}" ## to change
        extra_metadata_file_fp = save_metadata_file(request, project_data_path)
        for file in files:
            fs = FileSystemStorage(location = project_data_path)
            saved = fs.save(file.name, file)
            print(f'file: {file.name} is saved')
            fp = os.path.join(project_data_path, file.name)
            file_fps.append(file.name)
            file.close()

        temp_directory = os.path.join('./tmp/', str(temp_proj_id))

        print(AmpliconSuiteAggregator.__file__)
        if hasattr(AmpliconSuiteAggregator, '__version__'):
            print(f"AmpliconSuiteAggregator version: {AmpliconSuiteAggregator.__version__}")
        elif hasattr(Aggregator, '__version__'):
            print(f"Aggregator version: {Aggregator.__version__}")
        else:
            print("No version attribute found for Aggregator or AmpliconSuiteAggregator.")

        agg = Aggregator(file_fps, temp_directory, project_data_path, 'No', "", 'python3', uuid=str(temp_proj_id))
        if not agg.completed:
            ## redirect to edit page if aggregator fails
            alert_message = "Create project failed. Please ensure all uploaded samples have the same reference genome and are valid AmplionSuite results."
            return render(request, 'pages/create_project.html',
                        {'run': form,
                        'alert_message': alert_message,
                        'all_alias':json.dumps(get_all_alias())})
        ## after running aggregator, replace the requests file with the aggregated file:
        with open(agg.aggregated_filename, 'rb') as f:
            uploaded_file = SimpleUploadedFile(
            name=os.path.basename(agg.aggregated_filename),
            content=f.read(),
            content_type='application/gzip'
            )
            request.FILES['document'] = uploaded_file
        f.close()

        # return render(request, 'pages/loading.html')
        new_id = _create_project(form, request, extra_metadata_file_fp)
        if new_id is not None:
            return redirect('project_page', project_name=new_id.inserted_id)
        else:
            alert_message = "The input file was not a valid aggregation. Please see site documentation."

            return render(request, 'pages/create_project.html',
                          {'run': form,
                           'alert_message': alert_message,
                           'all_alias' : json.dumps(get_all_alias())})
    else:
        form = RunForm()
    return render(request, 'pages/create_project.html', {'run' : form,
                                                         'all_alias' : json.dumps(get_all_alias())})


def _create_project(form, request, extra_metadata_file_fp = None, previous_versions = [], previous_views = [0, 0], agg_fp = None):
    """
    Creates the project
    """

    form_dict = form_to_dict(form)
    project_name = form_dict['project_name']
    publication_link = form_dict['publication_link']
    user = get_current_user(request)
    # file download
    request_file = request.FILES['document'] if 'document' in request.FILES else None
    logging.debug("request_file var:" + str(request.FILES['document'].name))
    ## try to get metadata file
    project, tmp_id = create_project_helper(form, user, request_file, previous_versions = previous_versions, previous_views = previous_views)
    if project is None or tmp_id is None:
        return None

    project_data_path = f"tmp/{tmp_id}"
    new_id = collection_handle.insert_one(project)
    add_project_to_site_statistics(project, project['private'])
    # move the project location to a new name using the UUID to prevent name collisions
    new_project_data_path = f"tmp/{new_id.inserted_id}"
    os.rename(project_data_path, new_project_data_path)
    project_data_path = new_project_data_path
    file_location = f'{project_data_path}/{request_file.name}'
    logging.debug("file stats: " + str(os.stat(file_location).st_size))

    # extract the files async also
    extract_thread = Thread(target=extract_project_files,
                            args=(tarfile, file_location, project_data_path, new_id.inserted_id, extra_metadata_file_fp))
    extract_thread.start()

    if settings.USE_S3_DOWNLOADS:
        # load the zip asynch to S3 for later use
        file_location = f'{project_data_path}/{request_file.name}'

        s3_thread = Thread(target=upload_file_to_s3, args=(
        f'{project_data_path}/{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
        s3_thread.start()
    return new_id


## make a create_project_helper for project creation code
def create_project_helper(form, user, request_file, save = True, tmp_id = uuid.uuid4().hex, from_api = False, previous_versions = [], previous_views = [0, 0]):
    """
    Creates a project dictionary from

    """
    form_dict = form_to_dict(form)
    project_name = form_dict['project_name']
    publication_link = form_dict['publication_link']
    project = dict()
    # download_file(project_name, form_dict['file'])
    # runs = samples_to_dict(form_dict['file'])

    # file download

    if request_file:
        project_data_path = f"tmp/{tmp_id}"
        # create a new instance of FileSystemStorage
        if save:
            fs = FileSystemStorage(location=project_data_path)
            file = fs.save(request_file.name, request_file)
            #file_exists = os.path.exists(project_data_path+ "/" + request_file.name)
            #if settings.USE_S3_DOWNLOADS and file_exists:
            #    # we need to upload it to S3, we use the same path as here in the bucket to keep things simple
            #    session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
            #    s3client = session.client('s3')
            #    print(f'==== XXX STARTING uploaded to {project_data_path}/{request_file.name}')
            #    s3client.upload_file(f'{project_data_path}/{request_file.name}', settings.S3_DOWNLOADS_BUCKET, f'{settings.S3_DOWNLOADS_BUCKET_PATH}{project_data_path}/{request_file.name}')
            #    print('==== XXX uploaded to bucket')

    # extract contents of file
    if from_api:
        file_location = request_file.name
    else:
        file_location = f'{project_data_path}/{request_file.name}'

    # extract only run.json now because we will need it for project creation.
    # defer the rest to another thread to keep this faster
    # with tarfile.open(file_location, "r:gz") as tar_file:
    #     #tar_file.extractall(path=project_data_path)
    #     files_i_want = ['./results/run.json']
    #     tar_file.extractall(members=[x for x in tar_file.getmembers() if x.name in files_i_want],
    #                         path=project_data_path)

    ti = time.time()
    failed = False
    # print(f'{file_location}')
    logging.debug("FILE LOCATION EXISTS: " + str(os.path.exists(file_location)))
    logging.debug("PROJECT LOCATION EXISTS: " + str(os.path.exists(project_data_path)))
    with tarfile.open(file_location, 'r') as tar:
        try:
            # run_location = [run for run in tar.getnames() if 'run.json' in run]
            tar.extract('results/run.json', path=project_data_path)
        except:
            logging.error(str(file_location) + " had an issue. could not place ./results/run.json into " + project_data_path)
            failed = True
            # return None, None

    if failed:
        logging.debug("Deleting " + str(file_location))
        # os.remove(file_location)
        return None, None

    logging.debug(str(time.time() - ti) + " seconds for extraction of run.json")

    with open(file_location, "rb") as tar_file:
        project_tar_id = fs_handle.put(tar_file)


    #get run.json
    run_path = f'{project_data_path}/results/run.json'

    # Create a file at the run.json path to
    # serve as a flag to tell whether or not the file extraction process has finished.
    # if not finished and the user tries to go to the project, then render a loading screen.

    finish_flag = f"{project_data_path}/results/finished_project_creation.txt"
    with open(finish_flag, 'w') as finish_flag_file:
        finish_flag_file.write("NOT_FINISHED")
    finish_flag_file.close()


    with open(run_path, 'r') as run_json:
        runs = samples_to_dict(run_json)

    print('creating project now')
    current_user = user
    project['creator'] = current_user
    project['project_name'] = form_dict['project_name']
    project['publication_link'] = form_dict['publication_link']
    project['description'] = form_dict['description']
    project['tarfile'] = project_tar_id
    project['date_created'] = get_date()
    project['date'] = get_date()
    project['private'] = form_dict['private']
    project['delete'] = False
    project['current'] = True
    project['previous_versions'] = previous_versions
    project['update_date'] = get_date()
    user_list = create_user_list(form_dict['project_members'], current_user)
    project['project_members'] = user_list
    project['runs'] = replace_underscore_keys(runs)
    project['Oncogenes'] = get_project_oncogenes(runs)
    project['Classification'] = get_project_classifications(runs)
    project['FINISHED?'] = False
    project['views'] = previous_views[0]
    project['downloads'] = previous_views[1]
    project['alias_name'] = form_dict['alias']
    project['sample_count'] = len(runs)
    
    # iterate over project['runs'] and get the unique values across all runs
    # of AA_version, AC_version and 'AS-P_version'. Then add them to the project dict
    #substututing ASP_version for AS-P_version
    get_tool_versions(project, runs)

    return project, tmp_id


def get_tool_versions(project, runs):
    def get_existing_versions(key):
        val = project.get(key)
        if val and isinstance(val, str) and val != 'NA':
            return set(map(str.strip, val.split(',')))
        return set()

    aa_versions = get_existing_versions('AA_version')
    ac_versions = get_existing_versions('AC_version')
    asp_versions = get_existing_versions('ASP_version')
    
    for sample, features in runs.items():
        for feature in features:
            if 'AA version' in feature:
                aa_versions.add(feature['AA version'])
            if 'AC version' in feature:
                ac_versions.add(feature['AC version'])
            if 'AS-p version' in feature:
                asp_versions.add(feature['AS-p version'])
    project['AA_version'] = process_version_set(aa_versions)
    project['AC_version'] = process_version_set(ac_versions)
    project['ASP_version'] = process_version_set(asp_versions)


def process_version_set(version_set):
    """Return 'NA' if only None, the value if one, or comma-separated if multiple."""
    version_list = [v for v in version_set if v is not None]
    if not version_list:
        return 'NA'
    elif len(version_list) == 1:
        return str(version_list[0])
    else:
        return ', '.join(str(v) for v in version_list)


class FileUploadView(APIView):
    parser_class = (MultiPartParser,)
    permission_classes = []

    def get(self, request):
        logging.debug('Hello')
        return Response({'response':'success'})

    def post(self, request, format= None):
        '''
        Post API
        '''
        file_serializer = FileSerializer(data = request.data)

        if file_serializer.is_valid():
            file_serializer.save()
            form = RunForm(request.POST)
            form_dict = form_to_dict(form)
            proj_name = form_dict['project_name']
            request_file = request.FILES['file']
            # extract contents of file
            current_user = request.POST['project_members'][0]
            logging.info(f'Creating project for user {current_user}')
            if 'MULTIPART' in proj_name:
                _, api_id, final_file, actual_proj_name = proj_name.split('__')
            else:
                api_id = uuid.uuid4().hex

            if not os.path.exists(os.path.join('tmp', api_id)):
                os.system(f'mkdir -p tmp/{api_id}')
            os.system(f'mv tmp/{request_file.name} tmp/{api_id}/{request_file.name}')

            if 'MULTIPART' in proj_name:
                if str(final_file) in str(request_file.name):
                    ## we've reached the last file in the multifile upload, time to zip them up together
                    print('reached the final file, creating reconstructed zip now. ')
                    os.system(f'cat ./tmp/{api_id}/POST* > ./tmp/{api_id}/reconstructed.tar.gz')
                    file = open(f'./tmp/{api_id}/reconstructed.tar.gz', 'rb')
                    print('removing POST files now')
                    os.system(f'rm -f ./tmp/{api_id}/POST*')

                    helper_thread = Thread(target=self.api_helper, args=(form, current_user, file , api_id, actual_proj_name, True))
                    helper_thread.start()
            else:
                ## no multipart, just run the api helper:
                file = open(f'./tmp/{api_id}/{request_file.name}')
                actual_proj_name = request_file.name.split('.')[0]
                helper_thread = Thread(target=self.api_helper, args=(form, current_user,file, api_id, actual_proj_name))
                helper_thread.start()

            print('hanging up now')
            if 'MULTIPART':
                return Response({'Message': 'Successfully uploaded. Project creation will take more than 2 mins. Upload may time-out.'}, status=status.HTTP_201_CREATED)
            else:
                return Response(file_serializer.data, status=status.HTTP_201_CREATED)
        else:
            return Response(file_serializer.errors, status=status.HTTP_400_BAD_REQUEST)



    def api_helper(self, form, current_user, request_file, api_id,actual_proj_name, multifile = False):
        """
        Helper function for API, to be run asynchronously
        """
        logging.info('starting api helper')
        project, tmp_id = create_project_helper(form, current_user, request_file, save = False, tmp_id = api_id, from_api = True)
        if multifile:
            project['project_name'] = actual_proj_name
        logging.info('the project is here: ')
        new_id = collection_handle.insert_one(project)
        logging.info(str(new_id))
        project_data_path = f"tmp/{api_id}"
        # move the project location to a new name using the UUID to prevent name collisions
        # new_project_data_path = f"tmp/{new_id.inserted_id}"
        # os.rename(project_data_path, new_project_data_path)
        # project_data_path = new_project_data_path

        file_location = f'{request_file.name}'
        logging.debug('the project is here: ')
        logging.debug(str(file_location))

            # extract the files async also
        extract_thread = Thread(target=extract_project_files, args=(tarfile, file_location, project_data_path, new_id.inserted_id))
        extract_thread.start()

        if settings.USE_S3_DOWNLOADS:
            # load the zip asynch to S3 for later use
            file_location = f'{project_data_path}/{request_file.name}'

            s3_thread = Thread(target=upload_file_to_s3, args=(f'{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
            s3_thread.start()


def robots(request):
    """
    View for robots.txt, will read the file from static root (depending on server), and show robots file.
    """
    robots_txt = open(f'{settings.STATIC_ROOT}/robots.txt', 'r').read()
    return HttpResponse(robots_txt, content_type="text/plain")


# redirect to visualizer upon project selection
def coamplification_graph(request):
    if request.method == 'POST':
        # get list of selected projects
        selected_projects = request.POST.getlist('selected_projects')
        # store in session
        request.session['selected_projects'] = selected_projects
        return redirect('visualizer')  # Redirect to the visualizer page

    # Get projects the same way profile.html does
    username = request.user.username
    try:
        useremail = request.user.email
    except:
        # not logged in
        useremail = ""
        # For unauthenticated users, just get public projects
        public_projects = get_projects_close_cursor({'private': False, 'delete': False})

        # Filter out mm10, Unknown, and Multiple reference genome projects
        filtered_projects = []
        for proj in public_projects:
            prepare_project_linkid(proj)
            # Check reference genome for this project
            if 'runs' in proj and proj['runs']:
                ref_genome = reference_genome_from_project(proj['runs'])
                if ref_genome not in ['mm10', 'Unknown', 'Multiple']:
                    # Add reference genome to project object
                    proj['reference_genome'] = ref_genome
                    filtered_projects.append(proj)
            else:
                # Skip projects without runs info
                continue

        return render(request, 'pages/coamplification_graph.html', {'all_projects': filtered_projects})

    # Prevent an absent/null email from matching on anything
    if not useremail:
        useremail = username

    # Get all projects user has access to
    all_projects = get_projects_close_cursor({"$or": [
        # Projects where user is a member
        {"project_members": username},
        {"project_members": useremail},
        # Public projects
        {"private": False}
    ], 'delete': False})

    # Filter out mm10, Unknown, and Multiple reference genome projects
    filtered_projects = []
    for proj in all_projects:
        prepare_project_linkid(proj)
        # Check reference genome for this project
        if 'runs' in proj and proj['runs']:
            ref_genome = reference_genome_from_project(proj['runs'])
            if ref_genome not in ['mm10', 'Unknown', 'Multiple']:
                # Add reference genome to project object
                proj['reference_genome'] = ref_genome
                filtered_projects.append(proj)
        else:
            # Skip projects without runs info
            continue

    return render(request, 'pages/coamplification_graph.html', {'all_projects': filtered_projects})


# concatenate projects specified by project_list into a single data frame
def concat_projects(project_list):
    start_time = time.time()

    # Pre-allocate lists for efficiency
    all_samples = []
    sample_count = 0
    samples_per_project = {}

    for project_name in project_list:
        project_start = time.time()
        project = validate_project(get_one_project(project_name), project_name)

        # Get reference genome for this project - do this once per project
        ref_genome = reference_genome_from_project(project['runs'])

        # Get the project ID once
        project_id = project['_id']

        # Process all samples for this project in one batch
        project_samples = []
        samples_per_project[project_name] = [0, 0]

        for sample_data in project['runs'].values():
            # Convert to DataFrame once for each sample
            sample_df = pd.DataFrame(sample_data)

            # Add project_id and Reference_version columns efficiently
            sample_df['project_id'] = project_id

            # Only set Reference_version if it's missing
            if 'Reference_version' not in sample_df.columns:
                sample_df['Reference_version'] = ref_genome

            project_samples.append(sample_df)
            sample_count += 1
            samples_per_project[project_name][0] += 1
            if 'Classification' in sample_df.columns:
                ecdna_count = int(sample_df['Classification'].astype(str).str.lower().eq('ecdna').sum())
                samples_per_project[project_name][1] += ecdna_count

        # Batch concatenate all samples for this project
        if project_samples:
            project_df = pd.concat(project_samples, ignore_index=True)
            all_samples.append(project_df)

        project_time = time.time() - project_start
        logging.debug(
            f"Processed project {project_name} with {len(project_samples)} samples in {project_time:.3f} seconds")

    # If no valid projects, return empty DataFrame
    if not all_samples:
        logging.debug("No samples found in selected projects")
        return pd.DataFrame(), {}

    # Final concatenation of all project DataFrames
    concat_start = time.time()
    df = pd.concat(all_samples, ignore_index=True)
    concat_time = time.time() - concat_start

    total_time = time.time() - start_time
    logging.debug(
        f"Concatenated {sample_count} samples from {len(project_list)} projects into DataFrame with {len(df)} rows in {total_time:.3f} seconds")
    logging.debug(f"Final concatenation took {concat_time:.3f} seconds")

    return df, samples_per_project


def visualizer(request):
    selected_projects = request.session.get('selected_projects', [])

    # If no projects selected, redirect back
    if not selected_projects:
        messages.error(request, "No projects selected for visualization.")
        return redirect('coamplification_graph')

    # combine selected projects
    CONCAT_START = time.time()
    projects_df, projects_info = concat_projects(selected_projects)
    CONCAT_END = time.time()

    # If no data, redirect back
    if projects_df.empty:
        messages.error(request, "No valid data found in selected projects.")
        return redirect('coamplification_graph')

    # construct graph and load into neo4j
    load_graph(projects_df)
    IMPORT_END = time.time()

    # Get reference genomes information for display
    ref_genomes = projects_df[
        'Reference_version'].unique().tolist() if 'Reference_version' in projects_df.columns else ["Unknown"]

    return render(request, 'pages/visualizer.html', {
        'test_size': len(projects_df),
        'diff': CONCAT_END - CONCAT_START,
        'import_time': IMPORT_END - CONCAT_END,
        'reference_genomes': ref_genomes,
        'projects_stats': projects_info
    })


def fetch_graph(request, gene_name):
    min_weight = request.GET.get('min_weight')
    min_samples = request.GET.get('min_samples')
    oncogenes = request.GET.get('oncogenes', 'false').lower() == 'true'
    all_edges = request.GET.get('all_edges', 'false').lower() == 'true'

    try:
        nodes, edges = fetch_subgraph(gene_name, min_weight, min_samples, oncogenes, all_edges)
        # print(f"\nNumber of nodes: {len(nodes)}\nNumber of edges: {len(edges)}\n")
        return JsonResponse({
            'nodes': nodes,
            'edges': edges
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def search_results(request):
    """Handles user queries and renders search results."""

    if request.method == "POST":
        # Extract search parameters from POST request
        gene_search = request.POST.get("genequery", "").upper()
        project_name = request.POST.get("project_name", "").upper()
        classifications = request.POST.get("classquery", "").upper()
        sample_name = request.POST.get("metadata_sample_name", "").upper()
        sample_type = request.POST.get("metadata_sample_type", "").upper()

        # Get the combined cancer/tissue field
        cancer_tissue = request.POST.get("metadata_cancer_tissue", "").upper()

        # We'll set both parameters to the same value for the backend search
        cancer_type = cancer_tissue
        tissue_origin = ""  # Leave empty to avoid duplicate filtering

        extra_metadata = request.POST.get('metadata_extra', "").upper()

        # Store user query for persistence in the form
        user_query = {
            "genequery": gene_search,
            "project_name": project_name,
            "classquery": classifications,
            "metadata_sample_name": sample_name,
            "metadata_sample_type": sample_type,
            "metadata_cancer_tissue": cancer_tissue,  # New field
            'extra_metadata': extra_metadata
        }

        # Debugging logs
        logging.info(f'Search terms: Gene={gene_search}, Project={project_name}, Class={classifications}, '
                     f'Sample Name={sample_name}, Sample Type={sample_type}, Cancer/Tissue={cancer_tissue},'
                     f' Extra Metadata={extra_metadata}')

        # Run the search function
        search_results = perform_search(
            genequery=gene_search,
            project_name=project_name,
            classquery=classifications,
            metadata_sample_name=sample_name,
            metadata_sample_type=sample_type,
            metadata_cancer_type=cancer_tissue,  # Use the combined term
            metadata_tissue_origin=tissue_origin,  # Leave this empty
            extra_metadata=extra_metadata,
            user=request.user
        )

        # Count the number of matches for each category
        public_projects_count = len(search_results["public_projects"])
        private_projects_count = len(search_results["private_projects"])
        public_samples_count = len(search_results["public_sample_data"])
        private_samples_count = len(search_results["private_sample_data"])

        query_info = {
            "Gene Name": gene_search,
            "Project Name": project_name,
            "Classification": classifications,
            "Sample Name": sample_name,
            "Sample Type": sample_type,
            "Cancer Type or Tissue": cancer_tissue,  # Combined field in display
        }

        return render(request, "pages/gene_search.html", {
            "query_info": {k: v for k, v in query_info.items() if v},  # Filters out empty values
            "user_query": user_query,  # Pass user query to the template
            "public_projects": search_results["public_projects"],
            "private_projects": search_results["private_projects"],
            "public_sample_data": search_results["public_sample_data"],
            "private_sample_data": search_results["private_sample_data"],
            "public_projects_count": public_projects_count,
            "private_projects_count": private_projects_count,
            "public_samples_count": public_samples_count,
            "private_samples_count": private_samples_count,
        })

    else:
        return redirect("gene_search_page")  # Redirect if accessed incorrectly


def ec3d_visualization(request, sample_name):
    """
    Serve ec3D visualization HTML files for specific samples.
    """
    # Construct the file path
    ec3d_filename = f"{sample_name}_5k_ec3d.html"
    ec3d_path = os.path.join(settings.STATIC_ROOT or 'static', 'ec3d', ec3d_filename)

    # Check if file exists
    if not os.path.exists(ec3d_path):
        raise Http404(f"ec3D visualization not found for sample {sample_name}")

    # Read and serve the HTML file
    try:
        with open(ec3d_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HttpResponse(html_content, content_type='text/html')
    except Exception as e:
        logging.error(f"Error serving ec3D file for {sample_name}: {e}")
        raise Http404("Error loading ec3D visualization")


def check_ec3d_available(project_name, sample_name):
    """
    Check if ec3D visualization is available for a given project and sample.
    Returns True if the project name starts with 'ec3D' and the HTML file exists.
    """
    # Check if project name starts with 'ec3D'
    if not project_name.lower().startswith('ec3d'):
        logging.debug(f"{project_name} is not an ec3D project")
        return False

    # Check if ec3D HTML file exists
    ec3d_filename = f"{sample_name}_5k_ec3d.html"
    ec3d_path = os.path.join(settings.STATIC_ROOT or 'static', 'ec3d', ec3d_filename)
    logging.debug(f"ec3d_path is {ec3d_path}")

    return os.path.exists(ec3d_path)