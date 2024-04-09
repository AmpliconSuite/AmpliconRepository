import logging

import pandas as pd
from bson import ObjectId
from pymongo import MongoClient,ReadPreference
from allauth.account.adapter import DefaultAccountAdapter
from django import forms
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
import gridfs

import os


def get_db_handle(db_name, host, read_preference=ReadPreference.SECONDARY_PREFERRED
                  ):
    client = MongoClient(host, read_preference=read_preference
                        )
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]

def create_run_display(project):
    runs = project['runs']
    run_list = []
    for run in runs:
        for sample in runs[run]:
            for key in list(sample.keys()):
                newkey = key.replace(" ", "_")
                sample[newkey] = sample.pop(key)
            run_list.append(sample)
    return run_list

# since we use email and/or username to control project visibility,
# we don't want a new, unknown user to come in and register an account
# where the 'username' matches an existing account's email address.
# We also don't want an email address that matches an existing username
class CustomAccountAdapter(DefaultAccountAdapter):
    def clean_username(self, username, *args, **kwargs):
        User = get_user_model()
        users = User.objects.filter(email=username)

        if len(users) >= 1 :
            raise forms.ValidationError(f"{username} has already been registered to an account.")
        return super().clean_username(username)

    def clean_email(self, email):
        User = get_user_model()
        users = User.objects.filter(username=email)

        if len(users) >= 1:
            raise forms.ValidationError(f"{email} has already been registered to an account.")
        return super().clean_email(email)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """
        Invoked just after a user successfully authenticates via a
        social provider, but before the login is actually processed
        (and before the pre_social_login signal is emitted).

        We're trying to solve different use cases:
        - social account already exists, just go on
        - social account has no email or email is unknown, just go on
        - social account's email exists, link social account to existing user
        """

        # Ignore existing social accounts, just do this stuff for new ones
        if sociallogin.is_existing:
            return

        # some social logins don't have an email address, e.g. facebook accounts
        # with mobile numbers only, but allauth takes care of this case so just
        # ignore it
        if 'email' not in sociallogin.account.extra_data:
            return

        # check if given email address already exists.
        # Note: __iexact is used to ignore cases
        try:
            email = sociallogin.account.extra_data['email'].lower()
            email_address = EmailAddress.objects.get(email__iexact=email)

        # if it does not, let allauth take care of this new social account
        except EmailAddress.DoesNotExist:
            return

        # if it does, connect this new social login to the existing user
        user = email_address.user
        sociallogin.connect(request, user)



db_handle, mongo_client = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI_SECRET'])
db_handle_primary, mongo_client_primary = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI_SECRET'], read_preference=ReadPreference.PRIMARY)




collection_handle = get_collection_handle(db_handle,'projects')
collection_handle_primary = get_collection_handle(db_handle_primary,'projects')

fs_handle = gridfs.GridFS(db_handle)


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

        locations = [i.replace("'", "").strip() for i in feature['Location']]
        feature['Location'] = locations
        oncogenes = [i.replace("'", "").strip() for i in feature['Oncogenes']]
        feature['Oncogenes'] = oncogenes

    # print(sample_data[0])
    return sample_data


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


def sample_data_from_feature_list(features_list):
    """
    extracts sample data from a list of features
    """
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


def prepare_project_linkid(project):
    project['linkid'] = project['_id']


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
