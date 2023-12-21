# from asyncore import file_wrapper
# from tkinter import E
from django.http import HttpResponse, FileResponse, StreamingHttpResponse, HttpResponseRedirect, JsonResponse
from django.http import Http404
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth import get_user_model

## API framework packages
from rest_framework.response import Response
from  .serializers import FileSerializer
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, FileUploadParser
from rest_framework import status
from pathlib import Path
import csv

# from django.views.generic import TemplateView
# from pymongo import MongoClient
from django.conf import settings
# import pymongo
import json

# from .models import File
from .forms import RunForm, UpdateForm, FeaturedProjectForm, DeletedProjectForm
from .utils import get_db_handle, get_collection_handle, create_run_display
from django.forms.models import model_to_dict
import time

import os
import subprocess
import shutil
import caper.sample_plot as sample_plot
import caper.StackedBarChart as stacked_bar
import caper.project_pie_chart as piechart
from django.core.files.storage import FileSystemStorage
# from django.views.decorators.cache import cache_page
# from zipfile import ZipFile
import tarfile
import pandas as pd
# import numpy as np
#import cv2
import gridfs
# import caper
from bson.objectid import ObjectId
# from django.utils.text import slugify
# from bson.json_util import dumps
import re
# from tqdm import tqdm
from collections import defaultdict
from wsgiref.util import FileWrapper
import boto3
import botocore
from threading import Thread
import os, fnmatch
import uuid
import datetime

import time
import math
import logging
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

db_handle, mongo_client = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI'])

# SET UP HANDLE
collection_handle = get_collection_handle(db_handle,'projects')
site_statistics_handle = get_collection_handle(db_handle,'site_statistics')

fs_handle = gridfs.GridFS(db_handle)

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
    # date = today.strftime('%Y-%m-%d')
    date = today.isoformat()
    return date


def prepare_project_linkid(project):
    project['linkid'] = project['_id']


def get_one_project(project_name_or_uuid):
    try:
        project = collection_handle.find({'_id': ObjectId(project_name_or_uuid), 'delete': False})[0]
        prepare_project_linkid(project)
        return project

    except:
        project = None    

    # backstop using the name the old way
    if project is None:
        try:
            project = collection_handle.find_one({'project_name': project_name_or_uuid, 'delete': False})
            if project is not None:
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
                prepare_project_linkid(project)
                return project
        except:
            project = None

    # look for project that has been versioned by editing and giving it a new file.  This makes sure that
    # the old links work
    if project is None:
        try:
            project = collection_handle.find_one({'previous_project_ids': project_name_or_uuid, 'delete': False})
            if project is not None:
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None

    if project is None:
        logging.error(f"Project is None for {project_name_or_uuid}")

    return project


def get_one_deleted_project(project_name_or_uuid):
    try:
        project = collection_handle.find({'_id': ObjectId(project_name_or_uuid), 'delete': True})[0]
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


def get_one_sample(project_name, sample_name):
    project = validate_project(get_one_project(project_name), project_name)
    # print("ID --- ", project['_id'])
    runs = project['runs']
    for sample_num in runs.keys():
        current = runs[sample_num]
        try:
            if len(current) > 0 and current[0]['Sample_name'] == sample_name:
                sample_out = current
        except:
            # should not get here but we do sometimes for new projects, issue 194
            sample_out = None
    return project, sample_out


def get_one_feature(project_name, sample_name, feature_name):
    project, sample = get_one_sample(project_name, sample_name)
    feature = list(filter(lambda s: s['Feature_ID'] == feature_name, sample))
    return project, sample, feature


def check_project_exists(project_id):
    if collection_handle.count_documents({ '_id': ObjectId(project_id) }, limit = 1):
        return True

    elif collection_handle.count_documents({ 'project_name': project_id }, limit = 1):
        return True

    else:
        return False


def samples_to_dict(form_file):
    file_json = json.load(form_file)
    runs = dict()
    all_samples = file_json['runs']
    for key, value in all_samples.items():
        sample_name = key
        # logging.debug(f'in samples_to_dict {sample_name}')
        runs[sample_name] = value

    return runs


def form_to_dict(form):
    print('*************************')
    print('*************************')
    # print(form)
    run = form.save(commit=False)
    form_dict = model_to_dict(run)
    return form_dict


def flatten(nested, lst = True, sort = True):
    """
    recursive function to get elements in nested list
    """
    flat = []
    def helper(nested):
        for e in nested:
            if isinstance(e, list):
                helper(e)
            else:
                if e:
                    e = e.replace("'",'')
                    if len(e) > 0:
                        flat.append(e)
    helper(nested)
    
    if lst and sort:
        return list(sorted(flat))
    return flat


def sample_data_from_feature_list(features_list):
    """
    extracts sample data from a list of features
    """
    # now = datetime.datetime.now()
    df = pd.DataFrame(features_list)[['Sample_name', 'Oncogenes', 'Classification', 'Feature_ID']]
    sample_data = []
    for sample_name, indices in df.groupby(['Sample_name']).groups.items():
        sample_dict = dict()
        subset = df.iloc[indices]
        sample_dict['Sample_name'] = sample_name
        sample_dict['Oncogenes'] = sorted(set(flatten(subset['Oncogenes'].values.tolist())))
        sample_dict['Classifications'] = list(set(flatten(subset['Classification'].values.tolist())))
        if len(sample_dict['Oncogenes']) == 0 and len(sample_dict['Classifications']) == 0:
            sample_dict['Features'] = 0
        else:
            sample_dict['Features'] = len(subset['Feature_ID'])
        sample_data.append(sample_dict)
    # print(f'********** TOOK {datetime.datetime.now() - now}')
    return sample_data


def replace_space_to_underscore(runs):
    '''
    Replaces all spaces to underscores
    '''
        
    if type(runs) == dict:
        run_list = []
        for run in runs:
            for sample in runs[run]:
                for key in list(sample.keys()):
                    newkey = key.replace(" ", "_")
                    sample[newkey] = sample.pop(key)
                run_list.append(sample)
        return run_list

    else:
        run_list = []
        for sample in runs:
            run_list.append({})
            for key in list(sample.keys()):
                newkey = key.replace(" ", "_")
                run_list[-1][newkey] = sample[key]

        return run_list

def preprocess_sample_data(sample_data, copy=True, decimal_place=2):
    if copy:
        sample_data = [feature.copy() for feature in sample_data]

    # sample_data.sort(key=lambda x: (int(x['AA_amplicon_number']), x['Feature_ID']))
    for feature in sample_data:
        for key, value in feature.items():
            if type(value) == float:
                if key == 'AA_amplicon_number':
                    feature[key] = int(value)

                else:
                    feature[key] = round(value, 1)

            elif type(value) == str and value.startswith('['):
                feature[key] = ', \n'.join(value[2:-2].split("', '"))

            else:
                feature[key] = value

        locations = [i.replace("'","").strip() for i in feature['Location']]
        feature['Location'] = locations
        oncogenes = [i.replace("'","").strip() for i in feature['Oncogenes']]
        feature['Oncogenes'] = oncogenes
        
    # print(sample_data[0])
    return sample_data


def get_project_oncogenes(runs):
    oncogenes = set()
    for sample in runs:
        for feature in runs[sample]:
            if feature['Oncogenes']:
                for gene in feature['Oncogenes']:
                    if len(gene) != 0:
                        oncogene = gene.strip().replace("'",'')
                        oncogenes.add(oncogene)

    return list(oncogenes)


def get_project_classifications(runs):
    classes = set()
    for sample in runs:
        for feature in runs[sample]:
            if feature['Classification']:
                uppercase = feature['Classification'].upper()
                classes.add(uppercase)

    return list(classes)


def get_sample_oncogenes(feature_list, sample_name):
    """
    Finds the oncogenes for a given sample_name
    """
    oncogenes = set()
    for feature in feature_list:
        if feature['Sample_name'] == sample_name and feature['Oncogenes']:
            for gene in feature['Oncogenes']:
                if len(gene) != 0:
                    oncogenes.add(gene.strip().replace("'",''))

    return sorted(list(oncogenes))


def get_sample_classifications(feature_list, sample_name):
    classes = set()
    for feature in feature_list:
        if feature['Sample_name'] == sample_name:
            if feature['Classification']:
                uppercase = feature['Classification'].upper()
                classes.add(uppercase)

    return list(classes)


# @caper.context_processor
def get_files(fs_id):
    wrapper = fs_handle.get(fs_id)
    # response =  StreamingHttpResponse(FileWrapper(wrapper),content_type=file_['contentType'])
    return wrapper

def modify_date(projects):
    """
    Modifies the date to this format: 

    MM DD, YYYY HH:MM:SS AM/PM
    
    """

    for project in projects:
        try:

            dt = datetime.datetime.strptime(project['date'], f"%Y-%m-%dT%H:%M:%S.%f")
            project['date'] = (dt.strftime(f'%B %d, %Y %I:%M:%S %p %Z'))
        except Exception as e:
            logging.exception("Could not modify date for " + project['project_name'])
            logging.exception(e)

    return projects


def index(request):
    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email
        private_projects = list(collection_handle.find({ 'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}]  , 'delete': False}))
        for proj in private_projects:
            prepare_project_linkid(proj)

    else:
        private_projects = []

    # just get stats for all private
    all_private_proj_count = 0
    all_private_sample_count = 0
    all_private_projects = list(collection_handle.find({'private': True, 'delete': False}))
    for proj in all_private_projects:
        all_private_proj_count = all_private_proj_count + 1
        all_private_sample_count = all_private_sample_count + len(proj['runs'])
    # end private stats

    public_proj_count = 0
    public_sample_count = 0
    public_projects = list(collection_handle.find({'private' : False, 'delete': False}))
    for proj in public_projects:
        prepare_project_linkid(proj)
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    featured_projects = list(collection_handle.find({'private' : False, 'delete': False, 'featured': True}))
    for proj in featured_projects:
        prepare_project_linkid(proj)


    public_projects = modify_date(public_projects)
    private_projects = modify_date(private_projects)
    featured_projects = modify_date(featured_projects)

    regenerate_site_statistics()
    # get the latest set of stats
    repo_stats = site_statistics_handle.find().sort('_id',-1).limit(1).next()
    print(repo_stats)

    return render(request, "pages/index.html", {'public_projects': public_projects, 'private_projects' : private_projects, 'featured_projects': featured_projects, 'repo_stats': repo_stats})


def regenerate_site_statistics():
    print(db_handle.list_collection_names())
    # just get stats for all private
    all_private_proj_count = 0
    all_private_sample_count = 0
    all_private_projects = list(collection_handle.find({'private': True, 'delete': False}))
    for proj in all_private_projects:
        all_private_proj_count = all_private_proj_count + 1
        all_private_sample_count = all_private_sample_count + len(proj['runs'])
    # end private stats

    public_proj_count = 0
    public_sample_count = 0
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    # make it an array of name/values so we can add to it later
    repo_stats = {}
    repo_stats["public_proj_count"] = public_proj_count
    repo_stats["public_sample_count"] = public_sample_count
    repo_stats["all_private_proj_count"] = all_private_proj_count
    repo_stats["all_private_sample_count"] = all_private_sample_count
    repo_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(repo_stats)
    print(site_statistics_handle.count_documents({}))
    return repo_stats


def profile(request):
    username = request.user.username
    useremail = request.user.email
    # prevent an absent/null email from matching on anything
    if not useremail:
        useremail = username
    projects = list(collection_handle.find({  "$or": [{"project_members": username}, {"project_members": useremail}] , 'delete': False}))

    for proj in projects:
        prepare_project_linkid(proj)

    return render(request, "pages/profile.html", {'projects': projects})


def login(request):
    return render(request, "pages/login.html")


def reference_genome_from_project(samples):
    reference_genomes = list()
    for sample, features in samples.items():
        for feature in features:
            reference_genomes.append(feature['Reference_version'])
    if len(set(reference_genomes)) > 1:
        reference_genome = 'Multiple'
    else:
        reference_genome = reference_genomes[0]
    return reference_genome


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


def replace_underscore_keys(runs_from_proj_creation):
    """
    Replaces underscores in the keys from runs at proj creation step
    """
    new_run = {}
    for sample in runs_from_proj_creation.keys():
        features = []
        for feature in runs_from_proj_creation[sample]:
            new_feat = {}
            for key in feature.keys():
                new_feat[key.replace(" ", '_')] = feature[key]
            features.append(new_feat)
        new_run[sample] = features
    return new_run


def validate_project(project, project_name):
    """
    Checks the following for a project: 
    1. if keys in project[runs] all contain underscores, if not, replace them with underscores, insert into db
    """

    ## check for 1
    update = False
    for sample in project['runs'].keys():
        for feature in project['runs'][sample]:
            for key in feature.keys():
                if ' ' in key:
                    runs = replace_underscore_keys(project['runs'])
                    update = True
                    break
    if update:
        new_values = {"$set": {
            'runs': runs
        }}
        query = {'_id': project['_id'],
                    'delete': False}
        collection_handle.update(query, new_values)

    return get_one_project(project_name)


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

    project = validate_project(get_one_project(project_name), project_name)
    if project['private'] and not is_user_a_project_member(project, request):
        return HttpResponse("Project does not exist")

    # if we got here by an OLD project id (prior to edits) then we want to redirect to the new one
    if not project_name == str(project['linkid']):
        return redirect('project_page', project_name=project['linkid'])

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

    return render(request, "pages/project.html", {'project': project, 'sample_data': sample_data, 'message':message, 'reference_genome': reference_genome, 'stackedbar_graph': stackedbar_plot, 'piechart': pc_fig})


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


def find(pattern, path):
    result = []
    for root, dirs, files in os.walk(path):
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                result.append(os.path.join(root, name))
    return result

def project_download(request, project_name):
    project = get_one_project(project_name)
    
    if check_if_db_field_exists(project, 'project_downloads'):
        updated_downloads = project['project_downloads'] + 1
    else: 
        updated_downloads = 1
    
    query = {'_id': ObjectId(project_name)}
    new_val = { "$set": {'project_downloads': updated_downloads} }
    collection_handle.update_one(query, new_val)   
    # get the 'real_project_name' since we might have gotten  here with either the name or the project id passed in
    real_project_name = project['project_name']

    project_data_path = f"tmp/{project_name}"

    if settings.USE_S3_DOWNLOADS:

        project_linkid = project['_id']
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
    file_location = find('*.tar.gz', project_data_path)[0]
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
        logging.exception(e)
        sample_metadata = defaultdict(str)

    return sample_metadata

def sample_metadata_download(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    sample_metadata_id = sample_data[0]['Sample_metadata_JSON']
    try:
        sample_metadata = fs_handle.get(ObjectId(sample_metadata_id)).read()
        response = HttpResponse(sample_metadata)
        response['Content-Type'] = 'application/json'
        response['Content-Disposition'] = f'attachment; filename={sample_name}.json'
        # clear_tmp()
        return response

    except Exception as e:
        logging.exception(e)
        return HttpResponse()

# @cache_page(600) # 10 minutes
def sample_page(request, project_name, sample_name):
    logging.info(f"Loading sample page for {sample_name}")
    project, sample_data = get_one_sample(project_name, sample_name)
    project_linkid = project['_id']
    sample_metadata = get_sample_metadata(sample_data)
    reference_genome = reference_genome_from_sample(sample_data)
    sample_data_processed = preprocess_sample_data(replace_space_to_underscore(sample_data))
    filter_plots = not request.GET.get('display_all_chr')
    all_locuses = []
    igv_tracks = []
    download_png = []
    reference_version = []
    if sample_data_processed[0]['AA_amplicon_number'] == None:
        plot = sample_plot.plot(db_handle, sample_data_processed, sample_name, project_name, filter_plots=filter_plots)

    else:
        plot = sample_plot.plot(db_handle, sample_data_processed, sample_name, project_name, filter_plots=filter_plots)
        #plot, featid_to_updated_locations = sample_plot.plot(sample_data, sample_name, project_name, filter_plots=filter_plots)
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
                'name':feature['Feature_ID'],
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
    'sample_name': sample_name, 'graph': plot, 
    'igv_tracks': json.dumps(igv_tracks),
    'locuses': json.dumps(all_locuses),
    'download_links': json.dumps(download_png),
    'reference_versions': json.dumps(reference_version),
    }
    )


def sample_download(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    sample_data_processed = preprocess_sample_data(replace_space_to_underscore(sample_data))
    
    if check_if_db_field_exists(project, 'sample_downloads'):
        updated_downloads = project['sample_downloads'] + 1
    else:
        updated_downloads = 1
    
    query = {'_id': ObjectId(project_name)}
    new_val = { "$set": {'sample_downloads': updated_downloads} }
    collection_handle.update_one(query, new_val)   

    sample_data_path = f"tmp/{project_name}/{sample_name}"        

    for feature in sample_data_processed:
        # set up file system
        feature_id = feature['Feature_ID']
        feature_data_path = f"tmp/{project_name}/{sample_name}/{feature_id}"
        os.makedirs(feature_data_path, exist_ok=True)
        # get object ids
        if feature['Feature_BED_file'] != 'Not Provided':
            bed_id = feature['Feature_BED_file']
        else:
            bed_id = False
        if feature['CNV_BED_file'] != 'Not Provided':
            cnv_id = feature['CNV_BED_file']
        else:
            cnv_id = False
        if feature['AA_PDF_file'] != 'Not Provided':
            pdf_id = feature['AA_PDF_file']
        else:
            pdf_id = False
        if feature['AA_PNG_file'] != 'Not Provided':
            png_id = feature['AA_PNG_file']
        else:
            png_id = False
        if feature['AA_PNG_file'] != 'Not Provided':
            aa_directory_id = feature['AA_directory']
        else:
            aa_directory_id = False
        if feature['cnvkit_directory'] != 'Not Provided':
            cnvkit_directory_id = feature['cnvkit_directory']
        else:
            cnvkit_directory_id = False

        # get files from gridfs
        # bed_file = fs_handle.get(ObjectId(bed_id)).read()
        if bed_id is not None:
            if not ObjectId.is_valid(bed_id):
                 logging.debug("Sample: " + sample_name + ", Feature: " + feature_id + ", BED_ID is ->" + str(bed_id) + " <-")
                 break

            bed_file = fs_handle.get(ObjectId(bed_id)).read()
            with open(f'{feature_data_path}/{feature_id}.bed', "wb+") as bed_file_tmp:
                bed_file_tmp.write(bed_file)
  
        if cnv_id:
            cnv_file = fs_handle.get(ObjectId(cnv_id)).read()
        if pdf_id:
            pdf_file = fs_handle.get(ObjectId(pdf_id)).read()
        if png_id:
            png_file = fs_handle.get(ObjectId(png_id)).read()
        if aa_directory_id:
            aa_directory_file = fs_handle.get(ObjectId(aa_directory_id)).read()
        if cnvkit_directory_id:
            cnvkit_directory_file = fs_handle.get(ObjectId(cnvkit_directory_id)).read()
         
        # send files to tmp file system
#        with open(f'{feature_data_path}/{feature_id}.bed', "wb+") as bed_file_tmp:
#            bed_file_tmp.write(bed_file)
        if cnv_id:
            with open(f'{feature_data_path}/{feature_id}_CNV.bed', "wb+") as cnv_file_tmp:
                cnv_file_tmp.write(cnv_file)
        if pdf_id:
            with open(f'{feature_data_path}/{feature_id}.pdf', "wb+") as pdf_file_tmp:
                pdf_file_tmp.write(pdf_file)
        if png_id:
            with open(f'{feature_data_path}/{feature_id}.png', "wb+") as png_file_tmp:
                png_file_tmp.write(png_file)
        if aa_directory_id:
            if not os.path.exists(f'{sample_data_path}/aa_directory.tar.gz'):
                with open(f'{sample_data_path}/aa_directory.tar.gz', "wb+") as aa_directory_tmp:
                    aa_directory_tmp.write(aa_directory_file)
        if cnvkit_directory_id:
            if not os.path.exists(f'{sample_data_path}/cnvkit_directory.tar.gz'):
                with open(f'{sample_data_path}/cnvkit_directory.tar.gz', "wb+") as cnvkit_directory_tmp:
                    cnvkit_directory_tmp.write(cnvkit_directory_file)

    shutil.make_archive(f'{sample_name}', 'zip', sample_data_path)
    zip_file_path = f"{sample_name}.zip"
    with open(zip_file_path, 'rb') as zip_file:
        response = HttpResponse(zip_file)
        response['Content-Type'] = 'application/x-zip-compressed'
        response['Content-Disposition'] = f'attachment; filename={sample_name}.zip'

    os.remove(f'{sample_name}.zip')
    return response
    

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
    genequery = genequery.upper()
    gen_query = {'$regex': genequery }

    classquery = request.GET.get("classquery")
    classquery = classquery.upper()
    class_query = {'$regex': classquery}

    # Gene Search
    if request.user.is_authenticated:
        username = request.user.username
        useremail = request.user.email
        query_obj = {'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}] , 'Oncogenes' : gen_query, 'delete': False}

        private_projects = list(collection_handle.find(query_obj))
    else:
        private_projects = []
    
    public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'delete': False}))

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
                if genequery in sample['Oncogenes']:
                    upperclass =  map(str.upper, sample['Classifications'])
                    classmatch =(classquery in upperclass)
                    classempty = (len(classquery) == 0)
                    # keep the sample if we have matched on both oncogene and classification or oncogene and classification is empty
                    if classmatch or classempty:
                        sample_data.append(sample)
                elif len(genequery) == 0:
                    upperclass = map(str.upper, sample['Classifications'])
                    classmatch = (classquery in upperclass)
                    classempty = (len(classquery) == 0)
                    # keep the sample if we have matched on classification and oncogene is empty
                    if classmatch or classempty:
                        sample_data.append(sample)

        return sample_data
    
    public_sample_data = collect_class_data(public_projects)
    private_sample_data = collect_class_data(private_projects)

    # for display on the results page
    if len(classquery) == 0:
        classquery = "all amplicon types"
    return render(request, "pages/gene_search.html",
                  {'public_projects': public_projects, 'private_projects' : private_projects,
                   'public_sample_data': public_sample_data, 'private_sample_data': private_sample_data,
                   'gene_query': genequery, 'class_query': classquery})


def gene_search_download(request, project_name):
    project = get_one_project(project_name)
    samples = project['runs']
    for sample in samples:
        if len(samples[sample]) > 0:
            for feature in samples[sample]:
                # set up file system
                feature_id = feature['Feature_ID']
                feature_data_path = f"tmp/{project_name}/{feature['Sample_name']}/{feature_id}"
                os.makedirs(feature_data_path, exist_ok=True)
                # get object ids
                bed_id = feature['Feature_BED_file']
                cnv_id = feature['CNV_BED_file']
                pdf_id = feature['AA_PDF_file']
                png_id = feature['AA_PNG_file']
                
                # get files from gridfs
                bed_file = fs_handle.get(ObjectId(bed_id)).read()
                cnv_file = fs_handle.get(ObjectId(cnv_id)).read()
                pdf_file = fs_handle.get(ObjectId(pdf_id)).read()
                png_file = fs_handle.get(ObjectId(png_id)).read()
                
                # send files to tmp file system
                with open(f'{feature_data_path}/{feature_id}.bed', "wb+") as bed_file_tmp:
                    bed_file_tmp.write(bed_file)
                with open(f'{feature_data_path}/{feature_id}_CNV.bed', "wb+") as cnv_file_tmp:
                    cnv_file_tmp.write(cnv_file)
                with open(f'{feature_data_path}/{feature_id}.pdf', "wb+") as pdf_file_tmp:
                    pdf_file_tmp.write(pdf_file)
                with open(f'{feature_data_path}/{feature_id}.png', "wb+") as png_file_tmp:
                    png_file_tmp.write(png_file)

    project_data_path = f"tmp/{project_name}/"
    shutil.make_archive(f'{project_name}', 'zip', project_data_path)
    zip_file_path = f"{project_name}.zip"
    with open(zip_file_path, 'rb') as zip_file:
        response = HttpResponse(zip_file)
        response['Content-Type'] = 'application/x-zip-compressed'
        response['Content-Disposition'] = f'attachment; filename={project_name}.zip'
    os.remove(f'{project_name}.zip')
    return response


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
        return redirect('profile')
    else:
        return HttpResponse("Project does not exist")
    # return redirect('profile')


def edit_project_page(request, project_name):
    if request.method == "POST":
        project = get_one_project(project_name)
        try:
            prev_ids = project['previous_project_ids']
        except:
            prev_ids = []

        # no edits for non-project members
        if not is_user_a_project_member(project, request):
            return HttpResponse("Project does not exist")

        form = UpdateForm(request.POST, request.FILES)
        form_dict = form_to_dict(form)

        form_dict['project_members'] = create_user_list(form_dict['project_members'], get_current_user(request))

        request_file = request.FILES['document'] if 'document' in request.FILES else None
        print(f"request file is {request_file}")
        if request_file is not None:
            # mark the current project as deleted
            del_ret = project_delete(request, project_name)
            # create a new one with the new form
            a_message, new_id = _create_project(form, request)

            prev_ids.append(str(project['linkid']))
           # Create a mapping so links to the old project id still work
            query = {'_id': ObjectId(new_id.inserted_id)}
            new_val = {"$set": {'previous_project_ids': prev_ids}}

            collection_handle.update_one(query, new_val)

            return redirect('project_page', project_name=new_id.inserted_id, message=a_message)
            # go to the new project


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
            new_val = { "$set": {'project_name':new_project_name, 'runs' : current_runs, 'description': form_dict['description'], 'date': get_date(),
                                 'private': form_dict['private'], 'project_members': form_dict['project_members'],
                                 'Oncogenes': get_project_oncogenes(current_runs)} }
            if form.is_valid():
                collection_handle.update_one(query, new_val)
                return redirect('project_page', project_name=project_name)
            else:
                raise Http404()
        else:
            return HttpResponse("Project does not exist")
    else:
        project = get_one_project(project_name)
        # split up the project members and remove the empties
        members = project['project_members']
        members = [i for i in members if i]
        memberString = ', '.join(members)
        form = UpdateForm(initial={"project_name": project['project_name'],"description": project['description'],"private":project['private'],"project_members": memberString})
    return render(request, "pages/edit_project.html", {'project': project, 'run': form})

def create_user_list(string, current_user):
    # user_list = str.split(',')
    string = string + ',' + current_user
    # issue 21
    user_list = re.split(' |;|,|\t', string)
    # drop empty strings
    user_list =  [i for i in user_list if i]
    # clean whitespace
    user_list = [x.strip() for x in user_list]
    # remove duplicates
    user_list = list(set(user_list))
    return user_list

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
        collection_handle.update_one(query, new_val)



    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        prepare_project_linkid(proj)


    print(f"Proj count: {public_proj_count}      sample count: {public_sample_count}")

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

    env_to_skip = ['DB_URI', "GOOGLE_SECRET", "GLOBUS_SECRET"]
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
def admin_stats(request):
    if not  request.user.is_staff:
        return redirect('/accounts/logout')

    # Get all user data
    User = get_user_model()
    users = User.objects.all()
    
    # Get public and private project data
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        prepare_project_linkid(proj)
        
    # Calculate stats
    # total_downloads = [project['project_downloads'] for project in public_projects]

    return render(request, 'pages/admin_stats.html', {'public_projects': public_projects, 'users': users})

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def user_stats_download(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')

    # Get all user data
    User = get_user_model()
    users = User.objects.all()
    
    # Create the HttpResponse object with the appropriate CSV header.
    today = datetime.date.today()
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

@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
def project_stats_download(request):
    if not request.user.is_staff:
        return redirect('/accounts/logout')
    
    # Get public and private project data
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        prepare_project_linkid(proj)
    
    # Create the HttpResponse object with the appropriate CSV header.
    today = datetime.date.today()
    response = HttpResponse(
        content_type="text/csv",
    )
    response['Content-Disposition'] = f'attachment; filename="projects_{today}.csv"'

    writer = csv.writer(response)
    keys = ['project_name','description','project_members','date_created','project_downloads','sample_downloads']
    writer.writerow(keys)
    for dictionary in public_projects:
        output = {k: dictionary.get(k, None) for k in keys}
        writer.writerow(output.values())
    return response

# extract_project_files is meant to be called in a seperate thread to reduce the wait
# for users as they create the project
def extract_project_files(tarfile, file_location, project_data_path, project_id):
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
        get_one_project(project_id)
        query = {'_id': ObjectId(project_id)}
        new_val = {"$set": {'runs': runs,
                            'Oncogenes': get_project_oncogenes(runs)}}

        collection_handle.update_one(query, new_val)
        t_sb = time.time()
        diff = t_sb - t_sa
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

    deleted_projects = list(collection_handle.find({'delete': True}))
    for proj in deleted_projects:
        prepare_project_linkid(proj)
        try:
            tar_file_len = fs_handle.get(ObjectId(proj['tarfile'])).length
            proj['tar_file_len'] = sizeof_fmt(tar_file_len)
            if proj['delete_date']:
                dt = datetime.datetime.strptime(proj['delete_date'], f"%Y-%m-%dT%H:%M:%S.%f")
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
        form = RunForm(request.POST)
        if not form.is_valid():
            raise Http404()

        a_message, new_id = _create_project(form, request)

        return redirect('project_page', project_name=new_id.inserted_id, message=a_message)

    else:
        form = RunForm()
    return render(request, 'pages/create_project.html', {'run' : form})


def _create_project(form, request):
    form_dict = form_to_dict(form)
    project_name = form_dict['project_name']
    user = get_current_user(request)
    # file download
    request_file = request.FILES['document'] if 'document' in request.FILES else None
    project, tmp_id = create_project_helper(form, user, request_file)
    project_data_path = f"tmp/{tmp_id}"
    new_id = collection_handle.insert_one(project)
    # move the project location to a new name using the UUID to prevent name collisions
    new_project_data_path = f"tmp/{new_id.inserted_id}"
    os.rename(project_data_path, new_project_data_path)
    project_data_path = new_project_data_path
    file_location = f'{project_data_path}/{request_file.name}'
    # extract the files async also
    extract_thread = Thread(target=extract_project_files,
                            args=(tarfile, file_location, project_data_path, new_id.inserted_id))
    extract_thread.start()
    if settings.USE_S3_DOWNLOADS:
        # load the zip asynch to S3 for later use
        file_location = f'{project_data_path}/{request_file.name}'

        s3_thread = Thread(target=upload_file_to_s3, args=(
        f'{project_data_path}/{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
        s3_thread.start()
    # estimate how long the extraction could take and round up
    # CCLE was 3GB and took about 3 minutes
    try:
        file_size = os.path.getsize(file_location)
        est_min_to_extract = max(2, 1 + math.ceil(file_size / (1024 ** 3)))
    except:
        est_min_to_extract = 5
    a_message = f"Project creation is still in process.  It is estimated to take {est_min_to_extract} minutes before all samples and features are available."
    return a_message, new_id


## make a create_project_helper for project creation code 
def create_project_helper(form, user, request_file, save = True, tmp_id = uuid.uuid4().hex, from_api = False):
    """
    Creates a project dictionary from 
    
    """
    form_dict = form_to_dict(form)
    project_name = form_dict['project_name']
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
        print(file_location)
    else:
        file_location = f'{project_data_path}/{request_file.name}'
    with open(file_location, "rb") as tar_file:
        project_tar_id = fs_handle.put(tar_file)

    # extract only run.json now because we will need it for project creation.
    # defer the rest to another thread to keep this faster
    with tarfile.open(file_location, "r:gz") as tar_file:
        #tar_file.extractall(path=project_data_path)
        files_i_want = ['./results/run.json']
        tar_file.extractall(members=[x for x in tar_file.getmembers() if x.name in files_i_want],
                            path=project_data_path)
        
    #get run.json 
    run_path =  f'{project_data_path}/results/run.json'
    with open(run_path, 'r') as run_json:
        runs = samples_to_dict(run_json)
    # for filename in os.listdir(project_data_path):
    #     if os.path.isdir(f'{project_data_path}/{filename}'):

    ### Now do this in seperate thread using the extract_project_files: method
    # get cnv, image, bed files
    #for sample, features in runs.items():
    #    for feature in features:
    #        print(feature['Sample name'])
    #        if len(feature) > 0:
    #            # get paths
    #            key_names = ['Feature BED file', 'CNV BED file', 'AA PDF file', 'AA PNG file', 'Sample metadata JSON','AA directory','cnvkit directory']
    #            for k in key_names:
    #                try:
    #                    path_var = feature[k]
    #                    with open(f'{project_data_path}/results/{path_var}', "rb") as file_var:
    #                        id_var = fs_handle.put(file_var)
    #
    #                except:
    #                    id_var = "Not Provided"
    #
    #                feature[k] = id_var
    print('creating project now')
    current_user = user
    project['creator'] = current_user
    project['project_name'] = form_dict['project_name']
    project['description'] = form_dict['description']
    project['tarfile'] = project_tar_id
    project['date_created'] = get_date()
    project['date'] = get_date()
    project['private'] = form_dict['private']
    project['delete'] = False
    user_list = create_user_list(form_dict['project_members'], current_user)
    project['project_members'] = user_list
    project['runs'] = replace_underscore_keys(runs)
    project['Oncogenes'] = get_project_oncogenes(runs)
    project['Classification'] = get_project_classifications(runs)
    return project, tmp_id

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
            current_user = request.POST['project_members']
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

            s3_thread = Thread(target=upload_file_to_s3, args=(f'{project_data_path}/{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
            s3_thread.start()


