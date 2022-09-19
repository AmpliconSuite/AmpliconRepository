from asyncore import file_wrapper
from django.http import HttpResponse
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
import pandas as pd
import caper.sample_plot as sample_plot
from django.core.files.storage import FileSystemStorage
import subprocess as sp
import os


db_handle, mongo_client = get_db_handle('caper', 'localhost', '27017')
collection_handle = get_collection_handle(db_handle,'projects')

def get_date():
    today = datetime.datetime.now()
    date = today.isoformat()
    return date

def get_one_project(project_name):
    return collection_handle.find_one({'project_name': project_name})

def get_one_sample(project_name,sample_name):
    project = get_one_project(project_name)
    runs = project['runs']
    sample = runs[sample_name]
    return project, sample

def get_one_feature(project_name,sample_name, feature_name):
    project, sample = get_one_sample(project_name,sample_name)
    feature = list(filter(lambda sample: sample['Feature ID'] == feature_name, sample))
    return project, sample, feature

def check_project_exists(project_name):
    return collection_handle.count_documents({ 'project_name': project_name }, limit = 1)

def samples_to_dict(form_file):
    file_json = json.load(form_file)
    runs = dict()
    for run in file_json['runs'].values():
        if run:
            sample = run[0]
            sample_name = sample['Sample name']
            runs[sample_name] = run
    return runs

def download_file(project_name, form_file):
    project_data_path = f"project_data/{project_name}"
    fs = FileSystemStorage(location=project_data_path)
    fs.save(form_file.name, form_file)
    if form_file.name.endswith('.zip'):
        sp.run(f"cd '{os.getcwd()}/{project_data_path}'; unzip '{form_file.name}' > /dev/null", shell=True)
        files = os.listdir(project_data_path)
        for file in files:
            if file.endswith(".tar.gz"):
                sp.run(f"cd '{os.getcwd()}/{project_data_path}'; tar -xf '{file}'", shell=True)
    return open(f"{project_data_path}/run.json")

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

def index(request):
    user = request.user.id
    public_projects = list(collection_handle.find({'private' : False}))
    private_projects = list(collection_handle.find({ 'private' : True, 'user' : user }))
    return render(request, "pages/index.html", {'public_projects': public_projects, 'private_projects' : private_projects})

def profile(request):
    username = request.user.id
    projects = list(collection_handle.find({ 'user' : username}))
    return render(request, "pages/profile.html", {'projects': projects})

def login(request):
    return render(request, "pages/login.html")

def project_page(request, project_name):
    project = get_one_project(project_name)
    features = project['runs']
    features_list = replace_space_to_underscore(features)
    sample_data = sample_data_from_feature_list(features_list)
    return render(request, "pages/project.html", {'project': project, 'sample_data': sample_data})

def sample_page(request, project_name, sample_name):
    project, sample_data = get_one_sample(project_name, sample_name)
    sample_data_underscore = replace_space_to_underscore(sample_data)
    plot = sample_plot.plot(sample_data, project_name)
    return render(request, "pages/sample.html", {'project': project, 'sample_data': sample_data_underscore, 'sample_name': sample_name, 'graph': plot})

def feature_page(request, project_name, sample_name, feature_name):
    project, sample_data, feature  = get_one_feature(project_name,sample_name, feature_name)
    feature_data = replace_space_to_underscore(feature)
    return render(request, "pages/feature.html", {'project': project, 'sample_name': sample_name, 'feature_name': feature_name, 'feature' : feature_data})

def search_page(request):
    return render(request, "pages/search.html", {})

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
            new_val = { "$set": {'runs' : current_runs, 'description': form_dict['description'], 'date': get_date(), 'private': form_dict['private'], 'project_members': form_dict['project_members']} }
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
    if current_user in user_list:
        return user_list
    else:
        user_list.append(current_user)
        return user_list

def create_project(request):
    if request.method == "POST":
        form = RunForm(request.POST, request.FILES)
        form_dict = form_to_dict(form)
        project_name = form_dict['project_name']
        project = dict()
        form_file = download_file(project_name, form_dict['file'])
        runs = samples_to_dict(form_file)
        if check_project_exists(project_name):
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
            # print(project)
            if form.is_valid():
                collection_handle.insert_one(project)
                return redirect('project_page', project_name=project_name)
            else:
                raise Http404()
    else:
        form = RunForm()
    return render(request, 'pages/create_project.html', {'run' : form}) 