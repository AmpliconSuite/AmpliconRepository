from asyncore import file_wrapper
# from tkinter import E
from django.http import HttpResponse, FileResponse
from django.http import Http404
from django.shortcuts import render, redirect
from django.views.generic import TemplateView 
from pymongo import MongoClient
from django.conf import settings
import pymongo
import json
from .models import Run
from .forms import RunForm, UpdateForm
from .utils import get_db_handle, get_collection_handle, create_run_display
from django.forms.models import model_to_dict
import datetime
import os
import shutil
import caper.sample_plot as sample_plot
from django.core.files.storage import FileSystemStorage
from django.views.decorators.cache import cache_page
from zipfile import ZipFile
import tarfile
import pandas as pd
import numpy as np
#import cv2
import gridfs
import caper
from bson.objectid import ObjectId
from django.utils.text import slugify
from bson.json_util import dumps





# db_handle, mongo_client = get_db_handle('caper', 'mongodb://localhost:27017')
db_handle, mongo_client = get_db_handle('caper', os.environ['DB_URI'])

# db_handle, mongo_client = get_db_handle('caper', os.environ['DB_URI'])
collection_handle = get_collection_handle(db_handle,'projects')
fs_handle = gridfs.GridFS(db_handle)

def get_date():
    today = datetime.datetime.now()
    date = today.isoformat()
    return date

def get_one_project(project_name):
    return collection_handle.find_one({'project_name': project_name})

def get_one_sample(project_name,sample_name):
    project = get_one_project(project_name)
    runs = project['runs']
    for sample_num in runs.keys():
        current = runs[sample_num]
        if len(current) > 0:
            if current[0]['Sample name'] == sample_name:
                sample_out = current
    return project, sample_out


def get_one_feature(project_name,sample_name, feature_name):
    project, sample = get_one_sample(project_name,sample_name)
    feature = list(filter(lambda sample: sample['Feature ID'] == feature_name, sample))
    return project, sample, feature

# def get_all_projects(project_name, user):
#     public_projects = list(collection_handle.find({'private' : False, 'project_name' : project_name}))
#     private_projects = list(collection_handle.find({'private' : True, 'user' : user , 'project_name' : project_name}))
#     return public_projects, private_projects

def get_one_feature(project_name,sample_name, feature_name):
    project, sample = get_one_sample(project_name,sample_name)
    feature = list(filter(lambda sample: sample['Feature ID'] == feature_name, sample))
    return project, sample, feature

def check_project_exists(project_name):
    return collection_handle.count_documents({ 'project_name': project_name }, limit = 1)

def samples_to_dict(form_file):
    file_json = json.load(form_file)
    runs = dict()
    all_samples = file_json['runs']
    for key, value in all_samples.items():
        sample_name = key
        runs[sample_name] = value
    return runs

def form_to_dict(form):
    run = form.save(commit=False)
    form_dict = model_to_dict(run)
    return form_dict

def sample_data_from_feature_list(features_list):
    df = pd.DataFrame(features_list)
    df2 = df.groupby(['Sample_name']).size().reset_index(name="Features")
    sample_data = []
    for index, row in df2.iterrows():
        sample_dict = dict()
        sample_dict['Sample_name'] = row['Sample_name']
        sample_dict['Features'] = row['Features']
        sample_dict['Oncogenes'] = get_sample_oncogenes(features_list, row['Sample_name'])
        sample_dict['Classifications'] = get_sample_classifications(features_list, row['Sample_name'])
        sample_data.append(sample_dict)
    return sample_data

def replace_space_to_underscore(runs):
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

    for feature in sample_data:
        for key, value in feature.items():
            if type(value) == float:
                feature[key] = round(value, 1)
            elif type(value) == str and value.startswith('['):
                feature[key] = ', \n'.join(value[2:-2].split("', '"))
    return sample_data

def get_project_oncogenes(runs):
    oncogenes = set()
    for sample in runs:
        for feature in runs[sample]:
            if feature['Oncogenes']:
                for gene in feature['Oncogenes']:
                    if len(gene) != 0:
                        oncogene = gene.strip().replace("'",'')
                        sample_name = feature['Sample name']
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
    oncogenes = set()
    for feature in feature_list:
        if feature['Sample_name'] == sample_name:
            if feature['Oncogenes']:
                for gene in feature['Oncogenes']:
                    if len(gene) != 0:
                        oncogenes.add(gene.strip().replace("'",''))
    return list(oncogenes)

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

def index(request):
    if request.user.is_authenticated:
        user = request.user.email
        private_projects = list(collection_handle.find({ 'private' : True, 'project_members' : user }))
    else:
        private_projects = []
    public_projects = list(collection_handle.find({'private' : False}))
    return render(request, "pages/index.html", {'public_projects': public_projects, 'private_projects' : private_projects})

def profile(request):
    user = request.user.email
    projects = list(collection_handle.find({ 'project_members' : user }))
    return render(request, "pages/profile.html", {'projects': projects})

def login(request):
    return render(request, "pages/login.html")

def project_page(request, project_name):
    project = get_one_project(project_name)
    features = project['runs']
    features_list = replace_space_to_underscore(features)
    sample_data = sample_data_from_feature_list(features_list)
    # oncogenes = get_sample_oncogenes(features_list)
    return render(request, "pages/project.html", {'project': project, 'sample_data': sample_data})

def project_download(request, project_name):
    project = get_one_project(project_name)
    samples = project['runs']
    
    for sample in samples:
        if len(samples[sample]) > 0:
            for feature in samples[sample]:
                # set up file system
                feature_id = feature['Feature ID']
                feature_data_path = f"tmp/{project_name}/{feature['Sample name']}/{feature_id}"
                os.makedirs(feature_data_path, exist_ok=True)
                # get object ids
                bed_id = feature['Feature BED file'] 
                cnv_id = feature['CNV BED file']
                pdf_id = feature['AA PDF file']
                png_id = feature['AA PNG file']
                
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
    clear_tmp()
    return response

def igv_features_creation(locations):
    """
    Locations should look like: ["'chr11:56595156-58875237'", " 'chr11:66684707-68055335'", " 'chr11:69975662-70290667'"]

    """
    features = []
    for location in locations:
        parsed = location.replace(":", ",").replace("'", "").replace("-", ",").replace(" ", '').split(",")
        features.append({
            'chr':parsed[0],
            'start':int(parsed[1]),
            'end':int(parsed[2])
        })

    chr_num = locations[0].replace(":", ",").replace("'", "").replace("-", ",").replace(" ", '').split(",")[0].replace("chr", "")
    chr_min = int(locations[0].replace(":", ",").replace("'", "").replace("-", ",").replace(" ", "").split(",")[1])
    chr_max = int(locations[-1].replace(":", ",").replace("'", "").replace("-", ",").replace(" ", "").split(",")[-1])
    if chr_min > chr_max:
        locus = f"{chr_num}:{(int(chr_max)):,}-{(int(chr_min)):,}"
    else:
        locus = f"{chr_num}:{(int(chr_min)):,}-{(int(chr_max)):,}"

    return features, locus



@cache_page(600) # 10 minutes
def sample_page(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    sample_data_processed = preprocess_sample_data(replace_space_to_underscore(sample_data))
    # print(sample_data_processed[0])
    filter_plots = not request.GET.get('display_all_chr')
    plot = sample_plot.plot(sample_data, sample_name, project_name, filter_plots=filter_plots)
    igv_tracks = []
    locus_lst = []
    download_png = []
    for feature in sample_data_processed:
        download_png.append({
            'aa_amplicon_number':feature['AA_amplicon_number'],
            'download_link':f"http://{request.get_host()}/project/{project_name}/sample/{sample_name}/feature/{feature['Feature_ID']}/download/png/{feature['AA_PNG_file']}".replace(" ", "%")
        })


        roi_features, locus = igv_features_creation(feature['Location'])
        if locus != "":
            locus_lst.append(locus)
        else:
            locus_lst.append("")

        track = {
            'name':feature['Feature_ID'],
            # 'type': "seg",
            # 'url' : f"http://{request.get_host()}/project/{project_name}/sample/{sample_name}/feature/{feature['Feature_ID']}/download/{feature['Feature_BED_file']}".replace(" ", "%"),
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
    'sample_data': sample_data_processed,
    'sample_name': sample_name, 'graph': plot, 
    'igv_tracks': json.dumps(igv_tracks),
    'locuses': json.dumps(locus_lst),
    'download_links': json.dumps(download_png)
    }
    )
    
def sample_download(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    
    for feature in sample_data:
        # set up file system
        feature_id = feature['Feature ID']
        feature_data_path = f"tmp/{project_name}/{sample_name}/{feature_id}"
        os.makedirs(feature_data_path, exist_ok=True)
        # get object ids
        bed_id = feature['Feature BED file'] 
        cnv_id = feature['CNV BED file']
        pdf_id = feature['AA PDF file']
        png_id = feature['AA PNG file']
        
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

    sample_data_path = f"tmp/{project_name}/{sample_name}"        
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
    return(response)

def pdf_download(request, project_name, sample_name, feature_name, feature_id):
    img_file = fs_handle.get(ObjectId(feature_id)).read()
    response = HttpResponse(img_file, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{feature_name}.pdf"'
    # response = FileResponse(img_file)
    return(response)

def png_download(request, project_name, sample_name, feature_name, feature_id):
    img_file = fs_handle.get(ObjectId(feature_id)).read()
    response = HttpResponse(img_file, content_type='image/png')
    response['Content-Disposition'] = f'inline; filename="{feature_name}.png"'
    return(response)


#
# actually the old gene search page function, deprecated and replaced
#
def class_search_page(request):
    genequery = request.GET.get("genequery")
    genequery = genequery.upper()
    gen_query = {'$regex': genequery }

    classquery = request.GET.get("classquery")
    classquery = classquery.upper()
    class_query = {'$regex': classquery}

    # Gene Search
    if request.user.is_authenticated:
        user = request.user.email
        private_projects = list(collection_handle.find({'private' : True, 'project_members' : user , 'Oncogenes' : gen_query, 'Classification' : class_query}))
    else:
        private_projects = []
    
    public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'Classification' : class_query}))
    
    sample_data = []
    for project in public_projects:
        project_name = project['project_name']
        features = project['runs']
        features_list = replace_space_to_underscore(features)
        data = sample_data_from_feature_list(features_list)
        for sample in data:
            sample['project_name'] = project_name
            if genequery in sample['Oncogenes']:
                sample_data.append(sample)


    return render(request, "pages/gene_search.html", {'public_projects': public_projects, 'private_projects' : private_projects, 'sample_data': sample_data, 'query': query})

def gene_search_page(request):
    genequery = request.GET.get("genequery")
    genequery = genequery.upper()
    gen_query = {'$regex': genequery }

    classquery = request.GET.get("classquery")
    classquery = classquery.upper()
    class_query = {'$regex': classquery}

    # Gene Search
    if request.user.is_authenticated:
        user = request.user.email
        query_obj = {'private' : True, 'project_members' : user , 'Oncogenes' : gen_query, 'Classification': class_query}

        private_projects = list(collection_handle.find(query_obj))
    else:
        private_projects = []
    
    public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'Classification' : class_query}))
    print(public_projects)
    
    def collect_class_data(projects):
        sample_data = []
        for project in projects:

            project_name = project['project_name']
            features = project['runs']
            features_list = replace_space_to_underscore(features)
            data = sample_data_from_feature_list(features_list)
            for sample in data:
                sample['project_name'] = project_name
                print(sample)
                if genequery in sample['Oncogenes']:
                    upperclass =  map(str.upper, sample['Classifications'])
                    print(upperclass)
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
                feature_id = feature['Feature ID']
                feature_data_path = f"tmp/{project_name}/{feature['Sample name']}/{feature_id}"
                os.makedirs(feature_data_path, exist_ok=True)
                # get object ids
                bed_id = feature['Feature BED file'] 
                cnv_id = feature['CNV BED file']
                pdf_id = feature['AA PDF file']
                png_id = feature['AA PNG file']
                
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


def edit_project_page(request, project_name):
    if request.method == "POST":
        project = get_one_project(project_name)
        form = UpdateForm(request.POST, request.FILES)
        form_dict = form_to_dict(form)
        if form_dict['file']:
            runs = samples_to_dict(form_dict['file'])
        else:
            runs = 0
        if check_project_exists(project_name):
            current_runs = project['runs']
            if runs != 0:
                current_runs.update(runs)
            query = {'project_name': project_name}
            new_val = { "$set": {'runs' : current_runs, 'description': form_dict['description'], 'date': get_date(), 'private': form_dict['private'], 'project_members': form_dict['project_members'], 'Oncogenes': get_project_oncogenes(current_runs)} }
            if form.is_valid():
                collection_handle.update_one(query, new_val)
                print(f'in valid form')
                return redirect('project_page', project_name=project_name)
            else:
                raise Http404()
        else:
            return HttpResponse("Project does not exist")
    else:
        project = get_one_project(project_name)
        form = UpdateForm(initial={"description": project['description'],"private":project['private'],"project_members":project['project_members']})
    return render(request, "pages/edit_project.html", {'project': project, 'run': form})

def create_user_list(str, current_user):
    user_list = str.split(',')
    user_list = [x.strip() for x in user_list]
    if current_user in user_list:
        return user_list
    else:
        user_list.append(current_user)
        return user_list

def clear_tmp():
    folder = 'tmp/'
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (file_path, e))

def create_project(request):
    if request.method == "POST":
        form = RunForm(request.POST)
        
        # 
        form_dict = form_to_dict(form)
        project_name = form_dict['project_name']
        project = dict()        
        # download_file(project_name, form_dict['file'])
        # runs = samples_to_dict(form_dict['file'])
        
        # file download
        request_file = request.FILES['document'] if 'document' in request.FILES else None
        if request_file:
            project_data_path = f"tmp/{project_name}"
            # create a new instance of FileSystemStorage
            fs = FileSystemStorage(location=project_data_path)
            file = fs.save(request_file.name, request_file)
            
        # extract contents of file
        file_location = f'{project_data_path}/{request_file.name}'
        with tarfile.open(file_location, "r:gz") as tar_file:
            tar_file.extractall(path=project_data_path)
            
        #get run.json 
        run_path = f'{project_data_path}/results/run.json'   
        with open(run_path, 'r') as run_json:
            runs = samples_to_dict(run_json)
        # for filename in os.listdir(project_data_path):
        #     if os.path.isdir(f'{project_data_path}/{filename}'):
                
        
        # get cnv, image, bed files
        for sample, features in runs.items():
            # sample_name = features[0]['Sample name']
            for feature in features:
                if len(feature) > 0:
                    # get paths
                    bed_path = feature['Feature BED file'] 
                    cnv_path = feature['CNV BED file']
                    pdf_path = feature['AA PDF file']
                    png_path = feature['AA PNG file']
                    
                    # convert tab files to python format
                    try:
                        with open(f'{project_data_path}/results/{bed_path}', "rb") as bed_file:
                            bed_file_id = fs_handle.put(bed_file)
                    except:
                        bed_file_id = "Not Provided"
                    
                    try:
                        with open(f'{project_data_path}/results/{cnv_path}', "rb") as cnv_file:
                            cnv_file_id = fs_handle.put(cnv_file)
                    except:
                        bed_file_id = "Not Provided"
                    
                    try:
                        # convert image files to python format
                        with open(f'{project_data_path}/results/{pdf_path}', "rb") as pdf:
                            pdf_id = fs_handle.put(pdf)
                    except:
                        bed_file_id = "Not Provided"

                    try:
                        with open(f'{project_data_path}/results/{png_path}', "rb") as png:
                            png_id = fs_handle.put(png)
                    except:
                        bed_file_id = "Not Provided"
                    
                    # add files to runs dict
                    feature['Feature BED file'] = bed_file_id
                    feature['CNV BED file'] = cnv_file_id
                    feature['AA PDF file'] = pdf_id
                    feature['AA PNG file'] = png_id
        if check_project_exists(project_name):
            clear_tmp()
            return HttpResponse("Project already exists")
        else:
            current_user = request.user.email
            project['creator'] = current_user
            project['project_name'] = form_dict['project_name']
            project['description'] = form_dict['description']
            project['date_created'] = get_date()
            project['date'] = get_date()
            # project['sample_count'] = sample_count
            project['private'] = form_dict['private']
            user_list = create_user_list(form_dict['project_members'], current_user)
            project['project_members'] = user_list
            project['runs'] = runs
            project['Oncogenes'] = get_project_oncogenes(runs)
            project['Classification'] = get_project_classifications(runs)
            # print(project)
            if form.is_valid():
                collection_handle.insert_one(project)
                
                clear_tmp()
                return redirect('project_page', project_name=project_name)
            else:
                clear_tmp()
                raise Http404()
    else:
        form = RunForm()
    return render(request, 'pages/create_project.html', {'run' : form}) 
