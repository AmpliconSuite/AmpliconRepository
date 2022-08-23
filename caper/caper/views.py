from asyncore import file_wrapper
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.views.generic import TemplateView 
from pymongo import MongoClient
from django.conf import settings
import pymongo
import json
from .models import Run
from .forms import RunForm
from .utils import get_db_handle, get_collection_handle, create_run_display
from django.forms.models import model_to_dict

db_handle, mongo_client = get_db_handle('caper', 'localhost', '27017')
collection_handle = get_collection_handle(db_handle,'projects')

# collection_handle = get_collection_handle(db_handle, 'runs')

# run_collection = db['run']
# file = open('/Users/forrestkim/Documents/GitHub/caper/caper/static/json/run1_result_data.json')
# file_json = json.load(file)
# file.close()
# file_document = run_collection.insert_one(file_json[1])

# single_run = run_collection.find_one()
# id = single_run['_id']
# sample_name = single_run['Sample name']
# amplicon = single_run['AA amplicon number']
# oncogenes = single_run['Oncogenes']
# ref_version = single_run['Reference version']

# def single_run(request):
#     html = f"<html><body>One Run = ObjectID: {id}; Sample Name: {sample_name}; Amplicon number: {amplicon}; Oncogenes: {oncogenes}; Reference version: {ref_version}</body></html>"
#     return HttpResponse(html)

def index(request):
    username = request.user.id
    projects = list(collection_handle.find({},{ 'user' : username}))
    return render(request, "pages/index.html")

def profile(request):
    return render(request, "pages/profile.html")

def create_project(request):
    return render(request, 'pages/profile.html')

def run_list(request):
    if request.method == "POST":
        name = request.POST.get('project-name', False)
        project = collection_handle.find_one({'project_name': name})
        runs = project['runs']
        run_list = []
        for run in runs:
            for sample in runs[run]:
                for key in list(sample.keys()):
                    newkey = key.replace(" ", "_")
                    sample[newkey] = sample.pop(key)
                run_list.append(sample)
        return render(request, 'pages/run_list.html', {'runs' : run_list, 'project': name}) 
    return render(request, 'pages/run_list.html') 

def run_upload(request):
    if request.method == "POST":
        form = RunForm(request.POST, request.FILES)
        run = form.save(commit=False)
        form_dict = model_to_dict(run)
        name = form_dict['project_name']
        file = form_dict['file']
        file_json = json.load(file)
        
        username = request.user.id
        project = dict()
    
        runs = dict()
        sample = file_json[0]
        sample_name = sample['Sample name']
        runs[sample_name] = file_json
        
        if collection_handle.count_documents({ 'project_name': name }, limit = 1):
            project = collection_handle.find_one({'project_name': name})
            print(project)
            current_runs = project['runs']
            current_runs.update(runs)
            print(current_runs)
            query = {'project_name': name}
            new_val = { "$set": {'runs' : current_runs } }
            if form.is_valid():
                collection_handle.update_one(query, new_val)
                # run_list = []
                # for run in runs:
                #     for sample in runs[run]:
                #         for key in list(sample.keys()):
                #             newkey = key.replace(" ", "_")
                #             sample[newkey] = sample.pop(key)
                #         run_list.append(sample)
                return redirect('run_list')
        else:
            project['user'] = username
            project['project_name'] = name
            project['runs'] = runs
            print(project)
            if form.is_valid():
                collection_handle.insert_one(project)
                # runs = project['runs']
                # run_list = []
                # for run in runs:
                #     for sample in runs[run]:
                #         for key in list(sample.keys()):
                #             newkey = key.replace(" ", "_")
                #             sample[newkey] = sample.pop(key)
                #         run_list.append(sample)
                return redirect('run_list')
    else:
        form = RunForm()
    return render(request, 'pages/run_upload.html', {'run' : form}) 