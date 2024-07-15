#! /usr/bin/env python3 

# imports
from collections import OrderedDict
import os
import pandas as pd
import shutil
import ast
from pymongo import MongoClient
import os
import gridfs

import datetime
from datetime import timedelta
current_date = "%s" % datetime.date.today()
past_date = "%s" % (datetime.date.today() - timedelta(days=7))

# Pull the usage statistics from the database
server_base = os.getenv('AMPLICON_ENV')
server_port = os.getenv('AMPLICON_ENV_PORT')

def get_db_handle(db_name, host):
    client = MongoClient(host)
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle, collection_name):
    return db_handle[collection_name]

db_handle, mongo_client = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI_SECRET'])

collection_handle = get_collection_handle(db_handle,'projects')
fs_handle = gridfs.GridFS(db_handle)

if server_base == 'dev':
    full_url = 'https://dev.ampliconrepository.org'
elif server_base == 'prod':
    full_url = 'https://ampliconrepository.org'
else:
    full_url = f'http://localhost:{server_port}'

# Get all data
project_list = list(collection_handle.find({'private' : False, 'delete': False}))

projects_with_downloads = [project for project in project_list if len(project['sample_downloads']) > 0]

projects_df = pd.DataFrame(projects_with_downloads)

sample_downloads_list = projects_df['sample_downloads'].to_list()
normal_values = []
for sample in sample_downloads_list:
    for i in sample.values():
        if i < 100:
            normal_values.append(i)
avg_value = int(sum(normal_values)/len(normal_values))

def abnormal_downloads(downloads):
    for i in downloads.values():
        if i > 100:
            return 1
    else: 
        return 0
    
projects_df['abnormal_downloads'] = projects_df['sample_downloads'].apply(abnormal_downloads)

abnormal_projects = projects_df[projects_df['abnormal_downloads'] == 1]
abnormal_projects = abnormal_projects[['_id', 'project_name','sample_downloads']]

def fix_downloads(downloads):
    for k,v in downloads.items():
        if v > 100:
            downloads[k] = avg_value
    return downloads
            
            
abnormal_projects['sample_downloads'] = abnormal_projects['sample_downloads'].apply(fix_downloads)

def update_db(project):
    new_values = {"$set" : {'sample_downloads': project['sample_downloads']}}
    query = {'_id' : project['_id'], 'delete': False}
    collection_handle.update_one(query, new_values)
    print(f'updated {project["project_name"]}')

abnormal_projects.apply(update_db, axis=1)