import logging
import os
import gc
import tracemalloc

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

logging.getLogger("pymongo").setLevel(logging.WARNING)

from bson.objectid import ObjectId

from django.http import HttpResponse, StreamingHttpResponse, HttpResponseRedirect, Http404, JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

from django.contrib.auth.models import User

## API framework packages
from rest_framework.response import Response

from .user_preferences import update_user_preferences, get_user_preferences, notify_users_of_project_membership_change
from .site_stats import get_latest_site_statistics, add_project_to_site_statistics, delete_project_from_site_statistics, edit_proj_privacy

from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser
from rest_framework import status
from pathlib import Path

# Import admin views from separate module
from .views_admin import (
    admin_featured_projects, admin_version_details, admin_sendemail, admin_stats,
    admin_permanent_delete_project, admin_delete_user, admin_delete_project,
    user_stats_download, site_stats_regenerate, project_stats_download, sizeof_fmt,
    fix_schema, data_qc
)

# Import API views from separate module
from .views_apis import FileUploadView, ProjectFileAddView

# from django.views.generic import TemplateView
# from pymongo import MongoClient
from django.conf import settings
from django.db.models import Q

# from .models import File
from .forms import RunForm, UpdateForm, UserPreferencesForm

# Import utils functions
from .utils import (
    collection_handle, collection_handle_primary, fs_handle,
    get_one_project, get_one_sample, get_one_deleted_project,
    prepare_project_linkid, check_if_db_field_exists,
    get_date, get_date_short, previous_versions, form_to_dict,
    replace_space_to_underscore, sample_data_from_feature_list,
    get_all_alias, get_projects_close_cursor, create_user_list,
    preprocess_sample_data, validate_project, replace_underscore_keys,
    get_latest_project_version, flatten
)

from .extra_metadata import *

# imports for coamp graph
from .neo4j_utils import load_graph, fetch_subgraph

import subprocess
import shutil
import caper.sample_plot as sample_plot
import caper.StackedBarChart as stacked_bar
import caper.project_pie_chart as piechart
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import TemporaryUploadedFile

from wsgiref.util import FileWrapper
import boto3, botocore, fnmatch, uuid, datetime, time
from threading import Thread
import dateutil.parser

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





def get_one_feature(project_name, sample_name, feature_name):
    project, sample, _, _ = get_one_sample(project_name, sample_name)
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
        collection_handle.update_one(query, new_values)

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
        collection_handle.update_one(query, new_values)


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
        test = get_extra_metadata_from_project(proj)
        proj['sample_metadata_available'] = has_sample_metadata(proj)

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
    try:
        is_admin = getattr(request.user, 'is_staff', False)
    except Exception:
        is_admin = False
    if is_user_a_project_member(project, request) or (is_admin and not project.get('private', True)):
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

    project = validate_project(get_one_project(project_name), project_name)
    if 'FINISHED?' in project and project['FINISHED?'] == False:
        return render(request, "pages/loading.html", {"project_name":project_name})

    # Check if this is an empty project
    is_empty_project = 'EMPTY?' in project and project['EMPTY?'] == True

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

    set_project_edit_OK_flag(project, request)
    
    # Check if user is actually a project member (for subscription checkbox visibility)
    is_project_member = is_user_a_project_member(project, request)

    # For empty projects, set defaults
    if is_empty_project:
        samples = {}
        reference_genome = 'N/A'
        sample_data = []
        aggregate = None
        stacked_bar_plot = None
        pc_fig = None
    # For regular projects, process as before
    elif 'metadata_stored' not in project:
        samples = project['runs'].copy()
        features_list = replace_space_to_underscore(samples)
        reference_genome = reference_genome_from_project(samples)
        sample_data = sample_data_from_feature_list(features_list)
        aggregate, aggregate_save_fp = create_aggregate_df(project, samples)

        logging.debug(f'aggregate shape: {aggregate.shape}')
        new_values = {"$set" : {'sample_data' : sample_data,
                                'reference_genome' : reference_genome,
                                'aggregate_df' : aggregate_save_fp,
                                'metadata_stored': 'Yes'}}
        query = {'_id' : project['_id'], 'delete': False}

        logging.debug('Inserting Now')
        collection_handle.update_one(query, new_values)
        logging.debug('Insert complete')

        stackedbar_plot = stacked_bar.StackedBarChart(aggregate, fa_cmap)
        pc_fig = piechart.pie_chart(aggregate, fa_cmap)
    elif 'metadata_stored' in project:
        logging.info('Already have the lists in DB')
        samples = project['runs']
        reference_genome = project['reference_genome']
        sample_data = project['sample_data']
        aggregate_df_fp = project['aggregate_df']
        if not os.path.exists(aggregate_df_fp):
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
        if "warning" in project:
            message = project["warning"]

    ## download & view statistics
    views, downloads = session_visit(request, project)

    return render(request, "pages/project.html", {
        'project': project,
        'sample_data': sample_data,
        'reference_genome': reference_genome,
        'stackedbar_graph': stackedbar_plot,
        'piechart': pc_fig,
        'prev_versions': prev_versions,
        'prev_versions_length': len(prev_versions),
        'views': views,
        'downloads': downloads,
        'is_project_member': is_project_member,  # Pass actual membership status for subscription UI
        'proj_id': project_name,
        'viewing_old_project': viewing_old_project,
        'is_empty_project': is_empty_project,  # Pass this flag to the template
    })



def upload_file_to_s3(file_path_and_location_local, file_path_and_name_in_bucket):
    session = boto3.Session(profile_name=settings.AWS_PROFILE_NAME)
    s3client = session.client('s3')
    logging.info(f'==== XXX STARTING upload of {file_path_and_location_local} to s3://{settings.S3_DOWNLOADS_BUCKET}/{settings.S3_DOWNLOADS_BUCKET_PATH}{file_path_and_name_in_bucket}')
    s3client.upload_file(f'{file_path_and_location_local}', settings.S3_DOWNLOADS_BUCKET,
                         f'{settings.S3_DOWNLOADS_BUCKET_PATH}{file_path_and_name_in_bucket}')
    logging.info('==== XXX uploaded to bucket ')



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
                # recreate the path without the {settings.S3_DOWNLOADS_BUCKET} which the upload_file_to_s3 adds as well
                s3_file_location_bucketless = f'{project_linkid}/{project_linkid}.tar.gz'
                upload_file_to_s3(f'{project_data_path}/{project_linkid}.tar.gz', s3_file_location_bucketless)
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
    except Exception as e:
        # logging.exception(e)
        sample_metadata = defaultdict(str)
    try:
        # Add metadata from the `extra_metadata_from_csv` field
        extra_metadata = sample_data[0].get('extra_metadata_from_csv', {})
        if isinstance(extra_metadata, dict):
            sample_metadata.update(extra_metadata)
    except Exception as e:
        logging.exception(e)
        #sample_metadata = defaultdict(str)

    return sample_metadata

def sample_metadata_download(request, project_name, sample_name):
    project, sample_data, _, _ = get_one_sample(project_name, sample_name)
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
    project, sample_data, prev_sample, next_sample = get_one_sample(project_name, sample_name)
    project_linkid = project['_id']
    if project['private'] and not is_user_a_project_member(project, request):
        return redirect('/accounts/login')
    
    # Extract sample names from prev_sample and next_sample
    prev_sample_name = None
    if prev_sample and len(prev_sample) > 0:
        prev_sample_name = prev_sample[0].get('Sample_name')
    
    next_sample_name = None
    if next_sample and len(next_sample) > 0:
        next_sample_name = next_sample[0].get('Sample_name')
    
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
                   'sample_metadata': dict(sample_metadata),
                   'reference_genome': reference_genome,
                   'sample_name': sample_name,
                   'prev_sample': prev_sample_name,
                   'next_sample': next_sample_name,
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
    project, sample_data, _, _ = get_one_sample(project_name, sample_name)

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
                    _, sample_data, _, _ = get_one_sample(project_id, sample_name)
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



def get_current_user(request):
    current_user = request.user.username
    try:
        if current_user.email:
            current_user = current_user.email
        else:
            current_user = current_user.username
    except:
        current_user = request.user.username

    return current_user


def project_delete(request, project_name):
    project = get_one_project(project_name)
    deleter = get_current_user(request)
    is_admin = getattr(request.user, 'is_staff', False)
    allowed = is_user_a_project_member(project, request) or (is_admin and not project.get('private', True))
    if check_project_exists(project_name) and allowed:
        query = {'_id': project['_id']}
        #query = {'project_name': project_name}
        new_val = { "$set": {'delete' : True, 'delete_user': deleter, 'delete_date': get_date()} }
        collection_handle.update_one(query, new_val)
        delete_project_from_site_statistics(project, project['private'])
        
        # Clean up large objects
        del project
        gc.collect()
        
        return redirect('profile')
    else:
        # Clean up even on error path
        del project
        gc.collect()
        return HttpResponse("Project does not exist")


def project_update(request, project_name):
    """
    Updates the 'current' field for a project that has been updated
    current = False when a project is edited.
    update_date will be changed after project has been updated.
    """
    project = get_one_project_sans_runs(project_name)
    is_admin = getattr(request.user, 'is_staff', False)
    allowed = is_user_a_project_member(project, request) or (is_admin and not project.get('private', True))
    if check_project_exists(project_name) and allowed:
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



def remove_samples_from_runs(runs, samples_to_remove):
    """
    Removes specified samples from the runs dictionary.

    Args:
        runs: Dictionary where keys are sample identifiers and values are arrays of feature objects
        samples_to_remove: List of sample identifiers to remove

    Returns:
        Updated runs dictionary with specified samples removed
    """
    # Create a copy of the runs dictionary to avoid modifying the original
    updated_runs = runs.copy()

    # Find keys to remove by matching Sample_name in the feature objects
    keys_to_remove = []
    for key, features in updated_runs.items():
        # Check if the array is not empty
        if features and len(features) > 0:
            # Get the sample name from the first feature
            sample_name = features[0].get('Sample_name')
            if sample_name in samples_to_remove:
                keys_to_remove.append(key)

    # Remove the identified keys
    for key in keys_to_remove:
        del updated_runs[key]

    return updated_runs

def edit_project_page(request, project_name):
    if request.method == "GET":
        project = get_one_project(project_name)
        is_admin = getattr(request.user, 'is_staff', False)
        if not (is_user_a_project_member(project, request) or (is_admin and not project.get('private', True))):
            return HttpResponse("Project does not exist")
    if request.method == "POST":
        tracemalloc.start()
        start_snapshot = tracemalloc.take_snapshot()
        try:
            metadata_file = request.FILES.get("metadataFile")
        except Exception as e:
            print(f'Failed to get the metadata file from the form')
            print(e)

        samples_to_remove = request.POST.getlist('samples_to_remove')

        project = get_one_project(project_name)
        old_alias_name = None
        if 'alias_name' in project:
            old_alias_name = project['alias_name']
            print(f'THE OLD ALIAS NAME SHOULD BE: {old_alias_name}')
        # no edits for non-project members unless admin on a public project
        is_admin = getattr(request.user, 'is_staff', False)
        if not (is_user_a_project_member(project, request) or (is_admin and not project.get('private', True))):
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
        # Build project member list. Avoid auto-adding admins editing public projects when they are not members.
        is_member = is_user_a_project_member(project, request)
        is_public = not project.get('private', True)
        add_self = True
        if getattr(request.user, 'is_staff', False) and is_public and not is_member:
            add_self = False
        form_dict['project_members'] = create_user_list(form_dict['project_members'], get_current_user(request), add_current_user=add_self)
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

        ## check multi files,  run aggregator :
        file_fps = []
        temp_proj_id = uuid.uuid4().hex ## to be changed
        agg = None  # Initialize to None for cleanup
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
            url = f'http://localhost:8000/project/{project["linkid"]}/download'
            download_path = project_data_path+'/download.tar.gz'

            if samples_to_remove and len(samples_to_remove) > 0:
                # strip them from the current project runs before aggregation
                try:
                    os.makedirs(project_data_path, exist_ok=True)
                except Exception as e:
                    logging.error(f'Failed to make directory {project_data_path}')
                    logging.error(e)

                stripped_tar = remove_samples_from_tar(project, samples_to_remove, download_path, url)
                file_fps.append(os.path.basename(stripped_tar) )

            try:
                ## try to download old project file
                print(f"PREVIOUS FILE FPS LIST: {file_fps}")
                ### if replace project, don't download old project
                try:
                    if request.POST['replace_project'] == 'on':
                        print('Replacing project with new uploaded file')
                except:
                    # when removing samples we want the stripped tar file to be aggregated
                    # pulling the old one would have the stripped samples still in it
                    if not (samples_to_remove and len(samples_to_remove) > 0):
                        try:
                            download_file(url, download_path)
                            file_fps.append(os.path.join('download.tar.gz'))
                        except Exception as e:
                            logging.error(f'Failed to download the file: {e}')
                # print(f"AFTERS FILE FPS LIST: {file_fps}")
                # print(f'aggregating on: {file_fps}')
                temp_directory = os.path.join('./tmp/', str(temp_proj_id))
                agg = Aggregator(file_fps, temp_directory, project_data_path, 'No', "", 'python3', uuid=str(temp_proj_id))

                if not agg.completed:
                    ## redirect to edit page if aggregator fails

                    if os.path.exists(temp_directory):
                        shutil.rmtree(temp_directory)
                    # Clean up before returning
                    alert_message = "Edit project failed. Please ensure all uploaded samples have the same reference genome and are valid AmpliconSuite results."

                    # Clean up before returning
                    del agg
                    return render(request, 'pages/edit_project.html',
                              {'project': project,
                               'run': form,
                               'alert_message': alert_message,
                               'all_alias' :get_all_alias()})
                ## after running aggregator, replace the requests file with the aggregated file:

                with open(agg.aggregated_filename, 'rb') as f:
                    temp_file = TemporaryUploadedFile(
                        name=os.path.basename(agg.aggregated_filename),
                        content_type='application/gzip',
                        size=os.path.getsize(agg.aggregated_filename),
                        charset=None
                    )
                    # Copy file in chunks
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        temp_file.write(chunk)
                    temp_file.seek(0)
                    # Need to flush to ensure all data is written to the temp file
                    temp_file.file.flush()
                    request.FILES['document'] = temp_file

                # Explicitly delete aggregator object after use to free memory
                del agg
                agg = None
            except:
                ## download failed, don't run aggregator
                print(f'download failed ... ')
                if agg is not None:
                    del agg
        except:
            print('no file uploaded')
            if agg is not None:
                del agg
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
            # Preserve subscribers from the old project version
            old_subscribers = project.get('subscribers', [])
            # create a new one with the new form
            extra_metadata_file_fp = save_metadata_file(request, project_data_path)
            ## get extra metadata from csv first (if exists in old project), add it to the new proj
            old_extra_metadata = get_extra_metadata_from_project(project)

            # Clear the large project dict before creating new version
            project_id_for_redirect = None
            new_id = _create_project(form, request, extra_metadata_file_fp, old_extra_metadata=old_extra_metadata,  previous_versions = new_prev_versions, previous_views = [views, downloads], old_subscribers = old_subscribers)

            # can't delete it, if its being used in the other thread for metadata extraction
            if os.path.exists(temp_directory) and not extra_metadata_file_fp:
                shutil.rmtree(temp_directory)

            if new_id is not None:
                project_id_for_redirect = new_id.inserted_id
                # Notify subscribers about the project update
                try:
                    from .user_preferences import notify_subscribers_of_project_update
                    # Get the new project to determine sample count - use projection to limit data
                    new_project = collection_handle.find_one(
                        {'_id': ObjectId(str(project_id_for_redirect))},
                        {'sample_count': 1, 'runs': 1}
                    )
                    new_sample_count = new_project.get('sample_count', len(new_project.get('runs', {})))
                    notify_subscribers_of_project_update(project, project_id_for_redirect, new_sample_count)
                    # Clean up the limited new_project dict
                    del new_project
                except Exception as e:
                    logging.error(f"Failed to notify subscribers of project update: {str(e)}")

                # Explicitly clear large objects before redirect
                del project
                del old_extra_metadata
                del new_prev_versions
                end_snapshot2 = tracemalloc.take_snapshot()
                top_stats2 = end_snapshot2.compare_to(start_snapshot, 'lineno')
                logging.error("[3 -- Memory usage differences at end of edit_project_page]")
                for stat in top_stats2[:10]:
                    logging.error(stat)

                # go to the new project
                return redirect('project_page', project_name=project_id_for_redirect)
            else:
                alert_message = "The input file was not a valid aggregation. Please see site documentation."
                return render(request, 'pages/edit_project.html',
                              {'project': project,
                               'run': form,
                               'alert_message': alert_message,
                               'all_alias' :json.dumps(get_all_alias())})
        
        
        if 'file' in form_dict:
            runs = samples_to_dict(form_dict['file'])
        else:
            runs = 0

        if check_project_exists(project_name):
            new_project_name = form_dict['project_name']

            logging.info(f"project name: {project_name}  change to {new_project_name}")
            # Create a deep copy to avoid holding reference to original
            current_runs = dict(project['runs'])

            if runs != 0:
                current_runs.update(runs)
            query = {'_id': ObjectId(project_name)}
            try:
                alias_name = form_dict['alias']
                # print(alias_name)
            except:
                print('no alias to be found')

            old_extra_metadata = get_extra_metadata_from_project(project)
            current_runs = process_metadata_no_request(current_runs, metadata_file=metadata_file, old_extra_metadata = old_extra_metadata)

            # Initialize sample_data from existing project
            sample_data = project.get('sample_data', None)

            if project.get('sample_data',False) and samples_to_remove and len(samples_to_remove) > 0:
                # Create a copy to avoid modifying the original
                sample_data = list(project['sample_data'])
                current_runs = remove_samples_from_runs(current_runs, samples_to_remove)
                # Use list comprehension instead of modifying in place
                sample_data = [s for s in sample_data if s['Sample_name'] not in samples_to_remove]
            else:
                sample_data = project.get('sample_data', [])


            new_val = { "$set": {'project_name':new_project_name, 'runs' : current_runs,
                                 'description': form_dict['description'], 'date': get_date(),
                                 'private': form_dict['private'],
                                 'sample_data': sample_data,
                                 'project_members': form_dict['project_members'],
                                 'publication_link': form_dict['publication_link'],
                                 'Oncogenes': get_project_oncogenes(current_runs),
                                 'alias_name' : alias_name}}

            if project.get('sample_data', False) and samples_to_remove and len(samples_to_remove) > 0:
                new_val["$unset"] = {'metadata_stored': ""}

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

                # Clean up large objects before redirect
                del project
                del current_runs
                del old_extra_metadata

                end_snapshot2 = tracemalloc.take_snapshot()
                top_stats2 = end_snapshot2.compare_to(start_snapshot, 'lineno')
                print("[2 -- Memory usage differences at end of edit_project_page]")
                for stat in top_stats2[:10]:
                    print(stat)


                return redirect('project_page', project_name=project_name)
            else:
                raise Http404()

        else:
            return HttpResponse("Project does not exist")
    else:
        # get method handling
        project = get_one_project(project_name)

        sample_names = set()
        for features in project.get('runs', {}).values():
            if features and isinstance(features, list):
                for feature in features:
                    if isinstance(feature, dict) and 'Sample_name' in feature:
                        sample_names.add(feature['Sample_name'])
                        break  # All features in a list have the same sample name
        sample_names = sorted(sample_names)

        is_empty_project = 'EMPTY?' in project and project['EMPTY?'] == True
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
                     'sample_names': sample_names,
                   'all_alias' :json.dumps(get_all_alias()),
                   "is_empty_project": is_empty_project,
                   })



def  remove_samples_from_tar(project, samples_to_remove, download_path, url):
    # remove the sample data for the samples removed
    # from the project zip file. They will be in a directory in the tar file called
    # results/other_files/<SAMPLE_NAME>_classification/
    project_name = project['project_name']

    try:
        download_file(url, download_path)
    except Exception as e:
        logging.error(f'Failed to download the file: {e}')
        return None

    if os.path.exists(download_path):
        parent_dir = os.path.abspath(os.path.dirname(download_path))
        # Create a temporary directory to extract the tar file
        temp_extract_dir = f'{parent_dir}/extracted'
        os.makedirs(temp_extract_dir, exist_ok=True)

        # Extract the tar file
        with tarfile.open(download_path, 'r:gz') as tar:
            tar.extractall(path=temp_extract_dir)

        # Remove the sample directories
        for sample in samples_to_remove:
            sample_dir = os.path.join(temp_extract_dir, 'results', 'other_files', f'{sample}_classification')
            if os.path.exists(sample_dir):
                shutil.rmtree(sample_dir)

            # Also remove other directories left behind in other_files
            sample_dir2 = os.path.join(temp_extract_dir, 'results', 'AA_outputs', f'{sample}_AA_results')
            if os.path.exists(sample_dir2):
                shutil.rmtree(sample_dir2)

            sample_dir3 = os.path.join(temp_extract_dir, 'results', 'AA_outputs','extracted_from_zips', f'{sample}_AA_results')
            if os.path.exists(sample_dir3):
                shutil.rmtree(sample_dir3)

        # Create a new tar file without the removed samples

        new_project_tar_fp = f'{parent_dir}/{project_name}_stripped.tar.gz'
        with tarfile.open(new_project_tar_fp, 'w:gz') as tar:
            tar.add(os.path.join(temp_extract_dir, 'results'), arcname='results')

        # Clean up the temporary extraction directory
        shutil.rmtree(temp_extract_dir)
        os.remove(download_path)
        return new_project_tar_fp


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

# extract_project_files is meant to be called in a seperate thread to reduce the wait
# for users as they create the project

def extract_project_files(tarfile, file_location, project_data_path, project_id, extra_metadata_filepath, old_extra_metadata, samples_to_remove):
    tracemalloc.start()
    start_snapshot = tracemalloc.take_snapshot()
    t_sa = time.time()
    logging.info("Extracting files from tar...")
    try:
        with tarfile.open(file_location, "r:gz") as tar_file:
            tar_file.extractall(path=project_data_path)
        logging.info("Tar file extracted.")

        # get run.json
        run_path = f'{project_data_path}/results/run.json'
        with open(run_path, 'r') as run_json:
           runs = samples_to_dict(run_json)

        if samples_to_remove:
            runs = remove_samples_from_runs(runs, samples_to_remove)

        logging.info("Processing and uploading individual files to GridFS...")
        feature_count = 0
        total_features = sum(len(features) for features in runs.values())

        # get cnv, image, bed files
        for sample, features in runs.items():
            for feature in features:
                feature_count += 1
                if feature_count % 100 == 0:
                    logging.info(f"Processing feature {feature_count}/{total_features}...")
                    # Force garbage collection every 100 features to free memory
                    gc.collect()

                if len(feature) > 0:
                    # get paths
                    key_names = ['Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file', 'Sample metadata JSON',
                                 'AA directory', 'cnvkit directory']
                    for k in key_names:
                        try:
                            path_var = feature[k]
                            with open(f'{project_data_path}/results/{path_var}', "rb") as file_var:
                                id_var = fs_handle.put(file_var)
                            # Explicitly delete the file data reference
                            del path_var
                        except:
                            id_var = "Not Provided"
                        feature[k] = id_var

        logging.info("All features processed. Updating project in database...")

        # Now update the project with the updated runs
        project = get_one_project(project_id)
        query = {'_id': ObjectId(project_id)}
        if extra_metadata_filepath:
            runs = process_metadata_no_request(replace_underscore_keys(runs), file_path=extra_metadata_filepath, old_extra_metadata = old_extra_metadata)
            parent_dir = os.path.dirname(extra_metadata_filepath)

            if os.path.exists(parent_dir):
                shutil.rmtree(parent_dir)
        else:
            runs = process_metadata_no_request(replace_underscore_keys(runs), old_extra_metadata = old_extra_metadata)

        new_val = {"$set": {'runs': runs,
                            'Oncogenes': get_project_oncogenes(runs)}}

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
        logging.info(f"Finished extracting from tar and updating database in {str(diff)} seconds")

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

    end_snapshot4 = tracemalloc.take_snapshot()
    top_stats4 = end_snapshot4.compare_to(start_snapshot, 'lineno')
    logging.error("\n\n[4 -- Memory usage differences at end of extract_project_files]")
    for stat in top_stats4[:10]:
        logging.error(stat)

    finish_flag = f"{project_data_path}/results/finished_project_creation.txt"
    with open(finish_flag, 'w') as finish_flag_file:
        finish_flag_file.write("FINISHED")
    finish_flag_file.close()



def sizeof_fmt(num, suffix="B"):
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"

@login_required(login_url='/accounts/login/')
def regenerate_project_key(request, project_name):
    """
    Regenerates a private key for a project.
    Only project owners/members or admins can regenerate keys.
    Returns the new key as JSON.
    """
    # Check if this is a POST request
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    # Get the project
    project = get_one_project(project_name)
    if not project:
        return JsonResponse({"error": "Project not found"}, status=404)

    # Check if user is authorized (project member or admin)
    if not (is_user_a_project_member(project, request) or request.user.is_staff):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    # Generate a new UUID key
    new_key = str(uuid.uuid4())

    # Update the project with the new key
    query = {'_id': project['_id']}
    new_val = {"$set": {'privateKey': new_key}}
    collection_handle.update_one(query, new_val)

    # Return the new key
    return JsonResponse({"key": new_key})


@login_required
def toggle_project_subscription(request, project_name):
    """
    Toggle a user's subscription to a project.
    Non-members can subscribe to receive notifications about project changes.
    Returns JSON with subscription status.
    """
    # Check if this is a POST request
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    # Get the project
    project = get_one_project(project_name)
    if not project:
        return JsonResponse({"error": "Project not found"}, status=404)

    # Get current user
    current_user_email = request.user.email if request.user.email else request.user.username
    current_user_username = request.user.username

    # Check if user is a project member (members cannot subscribe/unsubscribe)
    is_member = current_user_username in project.get('project_members', []) or \
                current_user_email in project.get('project_members', [])

    if is_member:
        return JsonResponse({
            "error": "Project members are automatically notified of changes",
            "is_subscribed": True,
            "is_member": True
        }, status=400)

    # Initialize subscribers array if it doesn't exist
    subscribers = project.get('subscribers', [])

    # Toggle subscription
    if current_user_email in subscribers:
        # Unsubscribe
        subscribers.remove(current_user_email)
        is_subscribed = False
        message = "Successfully unsubscribed from project updates"
    else:
        # Subscribe
        subscribers.append(current_user_email)
        is_subscribed = True
        message = "Successfully subscribed to project updates"

    # Update the project with new subscribers list
    query = {'_id': project['_id']}
    new_val = {"$set": {'subscribers': subscribers}}
    collection_handle.update_one(query, new_val)

    # Return the subscription status
    return JsonResponse({
        "is_subscribed": is_subscribed,
        "is_member": False,
        "message": message
    })


def create_empty_project(request):
    """
    Creates an empty project with minimal information.
    Only requires project name and description.
    Can only be private, not public.
    """
    if request.method == "POST":
        # Extract minimal required fields
        project_name = request.POST.get('project_name', '').strip()
        description = request.POST.get('description', '').strip()
        alias = request.POST.get('alias', '').strip()
        publication_link = request.POST.get('publication_link', '').strip()

        # Get project members if provided
        project_members_raw = request.POST.get('project_members', '')

        # Validate inputs
        if not project_name:
            messages.error(request, "Project name is required")
            return redirect('profile')

        # Get current user info
        creator = get_current_user(request)

        # Process project members (ensure creator is included)
        project_members = create_user_list(project_members_raw, creator)

        # Set up timestamps
        current_date = get_date()

        # Create minimal project document
        project = {
            'creator': creator,
            'project_name': project_name,
            'description': description,
            'date_created': current_date,
            'date': current_date,
            'private': True,  # Empty projects can only be private
            'delete': False,
            'project_members': project_members,
            'runs': {},  # Empty runs dictionary
            'Oncogenes': [],
            'Classification': [],
            'linkid': ObjectId(),  # Generate a new linkid
            'current': True,
            'empty': True,  # Flag to identify empty projects
            'FINISHED?': True,  # Mark as finished since there's no extraction needed
            'EMPTY?': True, # Mark as empty
            'sample_count': 0,
            'metadata_stored': True  # Prevent reprocessing attempts
        }
        if publication_link:
            project['publication_link'] = publication_link
        # Add alias if provided
        if alias:
            project['alias'] = alias

        # Insert the project into MongoDB
        result = collection_handle.insert_one(project)
        project_id = str(result.inserted_id)

        # Update project with its own ID
        collection_handle.update_one(
            {'_id': ObjectId(project_id)},
            {"$set": {'linkid': project_id}}
        )

        # Create empty project directory structure
        project_data_path = f"tmp/{project_id}"
        os.makedirs(project_data_path, exist_ok=True)

        messages.success(request, f"Empty project '{project_name}' created successfully. You can now add files to it.")

        # Return to project page with alias if available
        if alias:
            return redirect('project_page', project_name=f"{project_id}")
        else:
            return redirect('project_page', project_name=project_id)

    # GET request - render the same form used for creating regular projects
    return render(request, "pages/create_project.html", {'all_alias': get_all_alias()})


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
            if os.path.exists(temp_directory):
                shutil.rmtree(temp_directory)
            ## redirect to edit page if aggregator fails
            alert_message = "Create project failed. Please ensure all uploaded samples have the same reference genome and are valid AmpliconSuite results."
            return render(request, 'pages/create_project.html',
                        {'run': form,
                        'alert_message': alert_message,
                        'all_alias':json.dumps(get_all_alias())})
        ## after running aggregator, replace the requests file with the aggregated file:
        logging.error(f"Aggregated filename: {agg.aggregated_filename}")

        with open(agg.aggregated_filename, 'rb') as f:
            temp_file = TemporaryUploadedFile(
                name=os.path.basename(agg.aggregated_filename),
                content_type='application/gzip',
                size=os.path.getsize(agg.aggregated_filename),
                charset=None
            )
            # Copy file in chunks
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                temp_file.write(chunk)
            temp_file.seek(0)
            request.FILES['document'] = temp_file
        f.close()

        # return render(request, 'pages/loading.html')
        new_id = _create_project(form, request, extra_metadata_file_fp)

        # can't delete it, if its being used in the other thread for metadata extraction
        if os.path.exists(temp_directory) and not extra_metadata_file_fp:
            shutil.rmtree(temp_directory)

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


def _create_project(form, request, extra_metadata_file_fp = None, old_extra_metadata = None,  previous_versions = [], previous_views = [0, 0], old_subscribers = None, agg_fp = None):
    """
    Creates the project
    """
    # it could be a new file was provided at the same time as sampels to be
    # removed from the project. That can't be done until runs is populated in the project
    samples_to_remove = request.POST.getlist('samples_to_remove')

    form_dict = form_to_dict(form)
    project_name = form_dict['project_name']
    publication_link = form_dict['publication_link']
    user = get_current_user(request)
    # file download
    request_file = request.FILES['document'] if 'document' in request.FILES else None
    logging.debug("request_file var:" + str(request.FILES['document'].name))
    ## try to get metadata file
    project, tmp_id = create_project_helper(form, user, request_file, previous_versions = previous_versions, previous_views = previous_views, old_subscribers = old_subscribers)
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
        args=(tarfile, file_location, project_data_path, new_id.inserted_id, extra_metadata_file_fp, old_extra_metadata, samples_to_remove))

    extract_thread.start()

    if settings.USE_S3_DOWNLOADS:
        # load the zip asynch to S3 for later use
        file_location = f'{project_data_path}/{request_file.name}'

        s3_thread = Thread(target=upload_file_to_s3, args=(
        f'{project_data_path}/{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
        s3_thread.start()
    return new_id


## make a create_project_helper for project creation code
def create_project_helper(form, user, request_file, save = True, tmp_id = uuid.uuid4().hex, from_api = False, previous_versions = [], previous_views = [0, 0], old_subscribers = None):
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
    project['EMPTY?'] = False
    project['views'] = previous_views[0]
    project['downloads'] = previous_views[1]
    project['alias_name'] = form_dict['alias']
    project['sample_count'] = len(runs)

    # Preserve subscribers from previous version if provided
    if old_subscribers is not None:
        project['subscribers'] = old_subscribers
    else:
        project['subscribers'] = []

    # iterate over project['runs'] and get the unique values across all runs
    # of AA_version, AC_version and 'AS-P_version'. Then add them to the project dict
    #substutiting ASP_version for AS-P_version
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


def robots(request):
    """
    View for robots.txt, will read the file from static root (depending on server), and show robots file.
    """
    robots_txt = open(f'{settings.STATIC_ROOT}/robots.txt', 'r').read()
    return HttpResponse(robots_txt, content_type="text/plain")


def get_reference_class(ref_genome):
    """
    Map reference genome to its compatibility class
    Returns: 'hg19' or 'hg38' or None
    """
    ref_equivalence = {
        'hg19': 'hg19',
        'GRCh37': 'hg19',
        'hg38': 'hg38',
        'GRCh38': 'hg38',
        'GRCh38_viral': 'hg38',
    }
    return ref_equivalence.get(ref_genome)


def validate_reference_compatibility(project_list):
    """
    Check that all selected projects have compatible reference genomes
    Returns: dict with 'valid' (bool) and 'error' (str) keys
    """
    if not project_list:
        return {'valid': True, 'error': ''}

    ref_classes = set()
    ref_genomes_by_class = {}

    for project_name in project_list:
        project = get_one_project(project_name)
        if not project or 'runs' not in project:
            continue

        ref_genome = reference_genome_from_project(project['runs'])
        ref_class = get_reference_class(ref_genome)

        if not ref_class:
            return {
                'valid': False,
                'error': f'Project "{project_name}" has unsupported reference genome: {ref_genome}'
            }

        ref_classes.add(ref_class)
        if ref_class not in ref_genomes_by_class:
            ref_genomes_by_class[ref_class] = []
        ref_genomes_by_class[ref_class].append(ref_genome)

    # Check if multiple reference classes selected
    if len(ref_classes) > 1:
        hg19_refs = ', '.join(set(ref_genomes_by_class.get('hg19', [])))
        hg38_refs = ', '.join(set(ref_genomes_by_class.get('hg38', [])))
        return {
            'valid': False,
            'error': f'Incompatible reference genomes selected. Please select projects from only one reference class: either hg19/GRCh37 '
                     f'({hg19_refs}) OR GRCh38/GRCh38_viral ({hg38_refs}), but not both.'
        }

    return {'valid': True, 'error': ''}


# redirect to visualizer upon project selection
def coamplification_graph(request):
    if request.method == 'POST':
        # get list of selected projects
        selected_projects = request.POST.getlist('selected_projects')

        # Validate reference genome compatibility
        if selected_projects:
            ref_validation = validate_reference_compatibility(selected_projects)
            if not ref_validation['valid']:
                messages.error(request, ref_validation['error'])
                return redirect('coamplification_graph')

        # store in session
        request.session['selected_projects'] = selected_projects
        return redirect('visualizer')

    # Get projects the same way profile.html does
    username = request.user.username
    try:
        useremail = request.user.email
    except:
        useremail = ""
        return redirect('account_login')

    if not useremail:
        useremail = username

    # Get all projects user has access to
    all_projects = get_projects_close_cursor({"$or": [
        {"project_members": username},
        {"project_members": useremail},
        {"private": False}
    ], 'delete': False})

    # Filter out mm10, Unknown, and Multiple reference genome projects
    # AND add reference_class
    filtered_projects = []
    for proj in all_projects:
        prepare_project_linkid(proj)
        if 'runs' in proj and proj['runs']:
            ref_genome = reference_genome_from_project(proj['runs'])
            if ref_genome not in ['mm10', 'Unknown', 'Multiple']:
                # Add reference genome and class to project object
                proj['reference_genome'] = ref_genome
                proj['reference_class'] = get_reference_class(ref_genome)
                filtered_projects.append(proj)

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

