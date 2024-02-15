#! /usr/bin/env python3 

# imports
from collections import OrderedDict
import os
import pandas as pd
from mailjet_rest import Client
import ast
import shutil
from pymongo import MongoClient
import os

# Set the date range to weekly
import datetime
from datetime import timedelta
current_date = "%s" % datetime.date.today()
past_date = "%s" % (datetime.date.today() - timedelta(days=7))
# current_date = "2023-08-16"
# past_date = "2023-08-09"

# Pull the usage statistics from the database
server_base = os.getenv('AMPLICON_ENV')
server_port = os.getenv('AMPLICON_ENV_PORT')

if server_base == 'dev':
    full_url = 'https://dev.ampliconrepository.org'
elif server_base == 'prod':
    full_url = 'https://ampliconrepository.org'
else:
    full_url = f'http://localhost:{server_port}'

# Get all data
user_df = pd.read_csv(f"{full_url}/admin-stats/download/user/")
projects_df = pd.read_csv(f"{full_url}/admin-stats/download/project/")

# fix data type issue
def literal_return(val):
    try:
        return ast.literal_eval(val)
    except ValueError:
        return val
projects_df['project_downloads'] = projects_df['project_downloads'].apply(literal_return)
projects_df['sample_downloads'] = projects_df['sample_downloads'].apply(literal_return)

# create downloads columns
def get_downloads_all_time(downloads):
    if type(downloads) == dict:
        total = downloads.values()
        return sum(total)
    else:
        return None

def get_downloads_past_week(downloads):
    if type(downloads) == dict:
        download_dates = [date for date in downloads.keys() if date > past_date]
        
def get_downloads_past_week(downloads):
    if type(downloads) == dict:
        download_dates = [date for date in downloads.keys() if date > past_date]
        if len(download_dates) > 0:
            total = [downloads[i] for i in download_dates]
            return sum(total)
        else:
            return 0
    else:
        return None

def get_recent(date):
    if str(date) > past_date and str(date) != 'nan':
        return 1
    else:
        return 0

projects_df['Project Downloads, all time'] = projects_df['project_downloads'].apply(get_downloads_all_time)
projects_df['Sample Downloads, all time'] = projects_df['sample_downloads'].apply(get_downloads_all_time)
projects_df['Project Downloads, this week'] = projects_df['project_downloads'].apply(get_downloads_past_week)
projects_df['Sample Downloads, this week'] = projects_df['sample_downloads'].apply(get_downloads_past_week)
projects_df['New projects, this week'] = projects_df['date_created'].apply(get_recent)

# get report details
with open('version.txt', 'r') as v:
    version = v.readline().strip('\n').replace('Version=','')

report_df = pd.DataFrame([('Start Date',past_date),('End Date',current_date),('Version',version)])

# get total users
total_users = pd.DataFrame([('Total Users, all time',user_df.count().max())])

# get login data
user_df['New users, this week'] = user_df['date_joined'].apply(get_recent)
user_df['Returning users, this week'] = user_df['last_login'].apply(get_recent)

user_logins = user_df[['New users, this week','Returning users, this week']].sum().to_frame()
user_logins = user_logins.reset_index()
user_logins.columns = [0, 1]

# get new users
new_users = user_df[user_df['New users, this week'] == 1]
new_users = new_users[['username','email']]

# get project downloads
project_downloads = projects_df[['Project Downloads, all time','Project Downloads, this week']].sum().astype('int').to_frame()
project_downloads = project_downloads.reset_index()
project_downloads.columns = [0, 1]

# get sample downloads
sample_downloads = projects_df[['Sample Downloads, all time','Sample Downloads, this week']].sum().astype('int').to_frame()
sample_downloads = sample_downloads.reset_index()
sample_downloads.columns = [0, 1]

# get new projects
new_projects = projects_df[projects_df['New projects, this week'] == 1]
new_projects = new_projects[['project_name','description']]

# get disk usage
path = "/"
stat = shutil.disk_usage(path)
disk_usage = pd.DataFrame([('Disk usage percentage',f'{stat.used/stat.total*100:.2f}%'),('Used/Total Size',f'{stat.used/1000000000:.2f}/{stat.total/1000000000:.2f} GB')])

# get db stats
def get_db_handle(db_name, host):
    client = MongoClient(host)
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]

db_handle, mongo_client = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI'])
collection_handle = get_collection_handle(db_handle,'projects')
stats = db_handle.command('dbstats', freeStorage=0)
#db_percent = stats['dataSize']/stats['fs']*100
#db_used = stats['dataSize']/1000000000
db_total = stats['storageSize']/1000000000
#database_usage = pd.DataFrame([('Database usage percentage',f'{db_percent:.2f}%'),('Used/Total Size',f'{db_used:.2f}/{db_total:.2f} GB')])
database_usage = pd.DataFrame([('Total Size',f'{db_total:.2f} GB')])

# convert data to html
def df_to_table(title, df, header=False):
    html = f'''<h2>{title}</h2>
    {df.to_html(index=False, header=header)}
    '''
    return html

report_details_html = df_to_table('Report details', report_df, False)
total_users_html = df_to_table('Total users', total_users, False)
logins_html = df_to_table('User login statistics', user_logins, False)
new_users_html = df_to_table('New users, this week', new_users, True)
project_downloads_html = df_to_table('Project downloads', project_downloads, False)
sample_downloads_html = df_to_table('Sample downloads', sample_downloads, False)
new_projects_html = df_to_table('Projects created, this week', new_projects, True)
disk_usage_html = df_to_table('Disk usage', disk_usage, False)
database_usage_html = df_to_table('DB usage', database_usage, False)

# Create a full html page from newly styled tables
report_html = []
report_html.append(
    '''<html><head>
    <style>
    .column {float: left; width: 50%;} 
    .row:after {content: "";display: table;clear: both;}
    table{border: 1px solid black; padding: 5px; border-collapse: collapse; margin-bottom: 2px;}
    th{border: 1px solid black; padding: 5px; border-collapse: collapse;}
    td{border: 1px solid black; padding: 5px; border-collapse: collapse;}
    h2{margin-top: 10px;}
    </style>
    </head>
    <div class="row">
    <div class="column">''')

report_html.append(report_details_html)
report_html.append(total_users_html)
report_html.append(logins_html)
report_html.append(new_users_html)
report_html.append('</div><div class="column">')
report_html.append(project_downloads_html)
report_html.append(sample_downloads_html)
report_html.append(new_projects_html)
report_html.append(disk_usage_html)
report_html.append(database_usage_html)
report_html.append('</div></div>')
report_html.append('</html>')
string = " ".join(report_html) # combine all html renders

# Send out email to desired recipients
## Define server and login information
server_login = os.getenv('MAILJET_API')
server_key = os.getenv('MAILJET_SECRETKEY')
server_from = os.getenv('MAILJET_EMAIL')
server_to = ['gp-dev@broadinstitute.org','jensluebeck@ucsd.edu']

html = f"<h2>AmpliconRepository Server Report ({full_url}), <br>week ending {current_date}</h2><br>{string}"
subject = f'Amplicon Repository {server_base} Server ({full_url}) User Statistics: {past_date} to {current_date}'

mailjet = Client(auth=(server_login, server_key), version='v3.1')
data = {'Messages': [{
                    "From": {
                        "Email": server_from,
                        "Name": "AmpliconRepository"
                    },
                    "To": [
                        {
                                "Email": recipient,
                                "Name": "Recipient"
                        }
                    ],
                    "Subject": subject,
                    "TextPart": None,
                    "HTMLPart": html} for recipient in server_to]
        }
result = mailjet.send.create(data=data)
print(result.status_code)
print(result.json())
