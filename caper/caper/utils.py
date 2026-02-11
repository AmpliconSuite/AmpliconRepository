import logging
from collections import Counter

import pandas as pd
from bson import ObjectId
from pymongo import MongoClient,ReadPreference
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from allauth.account.adapter import DefaultAccountAdapter
from django import forms
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
import gridfs
import re
import os
from django.forms.models import model_to_dict
import datetime
import tarfile

# def get_db_handle(db_name, host, read_preference=ReadPreference.SECONDARY_PREFERRED
#                   ):
#     client = MongoClient(host, read_preference=read_preference
#                         )
#     db_handle = client[db_name]
#     return db_handle, client


def get_db_handle(db_name, host, read_preference=ReadPreference.SECONDARY_PREFERRED):
    try:
        client = MongoClient(
            host,
            read_preference=read_preference,
            maxPoolSize=50,
            minPoolSize=10,
            maxIdleTimeMS=300000,  # 5 minutes - keep connections alive during long operations
            connectTimeoutMS=30000,  # 30 seconds - initial connection timeout
            socketTimeoutMS=None,  # No timeout - allows long-running GridFS operations
            serverSelectionTimeoutMS=30000,  # 30 seconds - time to select a server
            waitQueueTimeoutMS=10000,  # 10 seconds - wait for available connection from pool
            retryWrites=False,
            retryReads=False,
            w='majority',
            wtimeoutMS=60000  # 60 seconds - write operation acknowledgment timeout

        )

        # Verify connection is working
        client.admin.command('ismaster')

        db_handle = client[db_name]
        return db_handle, client

    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logging.error(f"Could not connect to MongoDB: {str(e)}")
        raise


def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]


def create_run_display(project):
    """
    Creates a flattened list of samples with underscores replacing spaces in keys.
    """
    return [
        {key.replace(" ", "_"): value for key, value in sample.items()}
        for run in project['runs']
        for sample in project['runs'][run]
    ]


# since we use email and/or username to control project visibility,
# we don't want a new, unknown user to come in and register an account
# where the 'username' matches an existing account's email address.
# We also don't want an email address that matches an existing username
class CustomAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        """
        Check if new user registrations are allowed.
        Returns False if registration is disabled by admin.
        """
        from .context_processor import get_registration_disabled
        if get_registration_disabled():
            return False
        return super().is_open_for_signup(request)
    
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
    def is_open_for_signup(self, request, sociallogin):
        """
        Check if new user registrations via social login are allowed.
        Returns False if registration is disabled by admin.
        """
        from .context_processor import get_registration_disabled
        if get_registration_disabled():
            return False
        return super().is_open_for_signup(request, sociallogin)
    
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
    # Optimized: Use projection to only exclude large fields not needed for sample page
    # MongoDB doesn't allow mixing inclusion and exclusion (except for _id)
    # So we exclude only the large unnecessary fields
    projection = {
        'Oncogenes': 0,
        'Classification': 0,
        'sample_data': 0,
        'aggregate_df': 0,
        'previous_versions': 0,
        'tarfile': 0
    }
    
    # Fetch project with optimized projection
    try:
        project = collection_handle.find_one(
            {'_id': ObjectId(project_name), 'delete': False},
            projection
        )
        if project is None:
            # Try by alias
            project = collection_handle.find_one(
                {'alias_name': project_name, 'delete': False},
                projection
            )
        if project is None:
            # Try by project name
            project = collection_handle.find_one(
                {'project_name': project_name, 'delete': False},
                projection
            )
    except Exception:
        # Fallback to old method if ObjectId conversion fails
        project = collection_handle.find_one(
            {'project_name': project_name, 'delete': False},
            projection
        )
    
    project = validate_project(project, project_name)
    prepare_project_linkid(project)
    
    # print("ID --- ", project['_id'])
    runs = project['runs']
    
    # Get all sample keys as a sorted list
    sample_keys = sorted(runs.keys())
    
    sample_out = None
    prev_sample = None
    next_sample = None
    current_index = None
    
    # Find the current sample and its index
    for idx, sample_num in enumerate(sample_keys):
        current = runs[sample_num]
        try:
            if len(current) > 0 and current[0]['Sample_name'] == sample_name:
                sample_out = current
                current_index = idx
                break
        except:
            # should not get here but we do sometimes for new projects, issue 194
            pass
    
    # Get previous and next samples if current sample was found
    if current_index is not None:
        if current_index > 0:
            prev_sample_key = sample_keys[current_index - 1]
            prev_sample = runs[prev_sample_key]
        
        if current_index < len(sample_keys) - 1:
            next_sample_key = sample_keys[current_index + 1]
            next_sample = runs[next_sample_key]
    
    return project, sample_out, prev_sample, next_sample


def initialize_ecDNA_context(project):
    """
    Check for and initialize the ecDNA_context dictionary in a project.

    If the ecDNA_context dictionary already exists in the project, return immediately.
    If it doesn't exist, create a dictionary populated from ecDNA_context_calls.tsv files
    in the project's tar file and save it to the project in the database.

    Args:
        project: The project dictionary from the database

    Returns:
        None - modifies the project in the database if needed
    """
    # Check if ecDNA_context already exists
    if 'ecDNA_context' in project:
        logging.debug(f"Project {project.get('project_name', project['_id'])} already has ecDNA_context")
        return

    # Create ecDNA_context dictionary
    logging.info(f"Initializing ecDNA_context for project {project.get('project_name', project['_id'])}")
    ecDNA_context = {}

    # Check if project has a tarfile
    if 'tarfile' not in project:
        logging.warning(
            f"Project {project.get('project_name', project['_id'])} has no tarfile, storing empty ecDNA_context")
    else:
        # Get the tar file from GridFS and extract ecDNA_context_calls.tsv files
        try:
            tar_id = project['tarfile']
            tar_gridfs_file = fs_handle.get(ObjectId(tar_id))
            logging.debug(f"Retrieved tarfile from GridFS for project {project.get('project_name', project['_id'])}")

            # Open tar file and look for ecDNA_context_calls.tsv files
            with tarfile.open(fileobj=tar_gridfs_file, mode='r:gz') as tar:
                # Find all members ending with ecDNA_context_calls.tsv
                context_files = [m for m in tar.getmembers() if m.name.endswith('ecDNA_context_calls.tsv')]

                logging.info(f"Found {len(context_files)} ecDNA_context_calls.tsv file(s) in project tar")

                # Process each file
                for member in context_files:
                    try:
                        # Extract and read the file
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            content = file_obj.read().decode('utf-8')
                            lines = content.strip().split('\n')

                            logging.debug(f"Processing {member.name} with {len(lines)} line(s)")

                            # Parse each line
                            for line in lines:
                                line = line.strip()
                                if line:  # Skip empty lines
                                    parts = line.split(None, 1)  # Split on first whitespace
                                    if len(parts) >= 2:
                                        key = parts[0]
                                        value = parts[1].strip()
                                        ecDNA_context[key] = value
                                        logging.debug(f"Added ecDNA_context: {key} -> {value}")
                                    elif len(parts) == 1:
                                        # Handle case where there's only a key with no value
                                        key = parts[0]
                                        ecDNA_context[key] = ""
                                        logging.debug(f"Added ecDNA_context: {key} -> (empty)")
                    except Exception as e:
                        logging.error(f"Error processing {member.name}: {e}")

            logging.info(f"Populated ecDNA_context with {len(ecDNA_context)} entries")

        except Exception as e:
            logging.error(f"Error reading tarfile for ecDNA_context: {e}")
            logging.exception("Full traceback:")

    # Update the project in the database
    query = {'_id': project['_id'], 'delete': False}
    new_values = {"$set": {'ecDNA_context': ecDNA_context}}
    collection_handle.update_one(query, new_values)

    # Update the local project object as well
    project['ecDNA_context'] = ecDNA_context
    logging.debug(f"ecDNA_context initialized and saved for project {project.get('project_name', project['_id'])}")


def sample_data_from_feature_list(features_list):
    """
    extracts sample data from a list of features
    
    ## only these fields are returned in the sample data for search!! ##
    [['Sample_name', 'Oncogenes', 'Classification', 'Feature_ID', 'Sample_type', 'Tissue_of_origin', 'extra_metadata_from_csv']]
    """
    df = pd.DataFrame(features_list)
    # print("sample_data_from_feature_list df")
    # print(df.head())
    cols = [col for col in ['Sample_name', 'Oncogenes', 'Classification', 'Feature_ID', 'Sample_type', "Cancer_type", 'Tissue_of_origin', 'extra_metadata_from_csv'] if col in df.columns]
    df= df[cols]
    sample_data = []
    for sample_name, indices in df.groupby(['Sample_name']).groups.items():
        sample_dict = dict()
        subset = df.iloc[indices]
        sample_dict['Sample_name'] = sample_name
        sample_dict['Oncogenes'] = sorted(set(flatten(subset['Oncogenes'].values.tolist())))
        # sample_dict['Classifications'] = list(set(flatten(subset['Classification'].values.tolist())))
        classifications = flatten(subset['Classification'].values.tolist())
        sample_dict['Classifications'] = list(set(classifications))
        class_counts = Counter(classifications)
        sample_dict['Classifications_counted'] = [
            f"{c} ({count})" if count > 1 else c
            for c, count in sorted(class_counts.items())
        ]
        if len(sample_dict['Oncogenes']) == 0 and len(sample_dict['Classifications']) == 0:
            sample_dict['Features'] = 0
        else:
            sample_dict['Features'] = len(subset['Feature_ID'])
        
        # if 'extra_metadata_from_csv' in subset.columns:
        #     try:
        #         for k, v in subset['extra_metadata_from_csv']:
        #             sample_dict[k] = v
        #     except Exception as e:
        #         logging.info(subset['extra_metadata_from_csv'])
        #         logging.info(e)
        if 'Sample_type' in subset.columns:
            sample_dict['Sample_type'] = subset['Sample_type'].values[0]
        if 'Cancer_type' in subset.columns:
            sample_dict['Cancer_type'] = subset['Cancer_type'].values[0]
        if 'Tissue_of_origin' in subset.columns:
            sample_dict['Tissue_of_origin'] = subset['Tissue_of_origin'].values[0]
        sample_dict['Sample_name'] = sample_name
        sample_data.append(sample_dict)
    # print(f'********** TOOK {datetime.datetime.now() - now}')
    return sample_data



def get_all_alias():
    """
    Gets all alias names in the db
    """
    return collection_handle.distinct('alias_name')
    

def get_one_project(project_name_or_uuid):
    """
    Gets one project from name or UUID. 
    
    if name, then checks the DB for an "alias" field, then gets that project if it has one 
    
    """
    
    try:
        project = collection_handle.find({'_id': ObjectId(project_name_or_uuid), 'delete': False})[0]
        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        ## first try finding the alias name
        try:
            project = collection_handle.find({'alias_name' : project_name_or_uuid, 'delete':False})[0]
            prepare_project_linkid(project)
            return project
        except:
            project = None
            
        ## then find project via project name
        try:
            project = collection_handle.find_one({'project_name': project_name_or_uuid, 'delete': False})
            if project is not None:
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
                prepare_project_linkid(project)
                return project
        except:
            project = None


    ## Maybe we are looking for an updated project: look for it by checking for the "current = False" flag
    if project is None:
        try:
            project = collection_handle.find_one({'_id': ObjectId(project_name_or_uuid), 'current': False, 'delete': True})
            if project is not None:
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None

    if project is None:
        try:
            project = collection_handle.find_one({'project_name': project_name_or_uuid, 'current': False, 'delete': True})
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


def check_if_db_field_exists(project, field):
    try:
        if project[field]:
            return True
    except:
        return False



def get_date():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%dT%H:%M:%S.%f')
    return date


def get_date_short():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%d')
    return date


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
    # cursor = collection_handle.find(
    #    {'current': True, 'previous_versions.linkid' : str(project['_id'])}, {'date': 1, 'previous_versions':1}).sort('date', -1)
    # data = list(cursor)
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
        res.append({'date': data[0].get('date', '1999-01-01T00:00:00.000000'),
                    'linkid': str(data[0]['_id']),
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

        res.append({'date': project.get('date', '1999-01-01T00:00:00.000000'),
                    'linkid': str(project['linkid']),
                    'AC_version': project.get('AC_version', 'NA'),
                    'AA_version': project.get('AA_version', 'NA'),
                    'ASP_version': project.get('ASP_version', 'NA')
                    })
        res.reverse()

    return res, msg

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



def get_latest_project_version(project):

    doc = collection_handle.find_one(
        {'current': True, 'previous_versions.linkid': str(project['_id'])},
    )

    if doc is None:
        return project
    else:
        prepare_project_linkid(doc)
        return doc

def get_one_project_sans_runs(project_name_or_uuid):
    """
    Gets one project from name or UUID, excluding the 'runs' field to reduce memory usage.
    
    if name, then checks the DB for an "alias" field, then gets that project if it has one 
    
    This is useful when you only need project metadata without the full sample/feature data.
    """
    
    # Projection to exclude the runs field
    projection = {'runs': 0}
    
    try:
        project = collection_handle.find({'_id': ObjectId(project_name_or_uuid), 'delete': False}, projection)[0]
        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        ## first try finding the alias name
        try:
            project = collection_handle.find({'alias_name': project_name_or_uuid, 'delete': False}, projection)[0]
            prepare_project_linkid(project)
            return project
        except:
            project = None
            
        ## then find project via project name
        try:
            project = collection_handle.find_one({'project_name': project_name_or_uuid, 'delete': False}, projection)
            if project is not None:
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
                prepare_project_linkid(project)
                return project
        except:
            project = None


    ## Maybe we are looking for an updated project: look for it by checking for the "current = False" flag
    if project is None:
        try:
            project = collection_handle.find_one({'_id': ObjectId(project_name_or_uuid), 'current': False, 'delete': True}, projection)
            if project is not None:
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None

    if project is None:
        try:
            project = collection_handle.find_one({'project_name': project_name_or_uuid, 'current': False, 'delete': True}, projection)
            if project is not None:
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None



    if project is None:
        logging.error(f"Project is None for {project_name_or_uuid}")

    return project


def flatten(nested, lst=True, sort=True):
    """
    Recursively flattens a nested list and optionally sorts the result.
    Removes empty strings and single quotes from elements.

    Args:
        nested: A potentially nested list structure
        lst: Whether to return a list (if False, returns the internal working list)
        sort: Whether to sort the final result (only applies if lst=True)

    Returns:
        A flattened list of non-empty strings with quotes removed
    """

    def helper(items):
        for item in items:
            if isinstance(item, list):
                yield from helper(item)
            elif item:  # Checks for non-empty strings
                cleaned = item.replace("'", '')
                if cleaned:  # Check again after cleaning
                    yield cleaned

    result = list(helper(nested))
    return sorted(result) if lst and sort else result


def validate_project(project, project_name):
    """
    Checks the following for a project:
    1. if keys in project[runs] all contain underscores, if not, replace them with underscores, insert into db
    2. Checks if Cancer_type exists. if not, initialize to None
    """
    
    # Handle case where project is None
    if project is None:
        logging.error(f"Cannot validate project: project is None for {project_name}")
        return None

    ## check for 1 and numeric Sample_name values
    update = False
    runs = None
    for sample in project['runs'].keys():
        for feature in project['runs'][sample]:
            # Check for spaces in keys (original check)
            for key in feature.keys():
                if ' ' in key:
                    runs = replace_underscore_keys(project['runs'])
                    update = True
                    break
            
            # Check for numeric Sample_name values
            if not update and 'Sample_name' in feature:
                if isinstance(feature['Sample_name'], (int, float)):
                    runs = replace_underscore_keys(project['runs'])
                    update = True
                    break
            
            if update:
                break
        if update:
            break
    if update and runs is not None:
        new_values = {"$set": {
            'runs': runs
        }}
        query = {'_id': project['_id'],
                    'delete': False}
        collection_handle.update_one(query, new_values)

    return get_one_project(project_name)


def prepare_project_linkid(project):
    project['linkid'] = project['_id']


def replace_underscore_keys(runs_from_proj_creation):
    """
    Replaces spaces with underscores in the keys from runs at project creation step.
    Returns a new dictionary with transformed keys.
    Also ensures Sample_name field values are strings, not integers.
    """
    return {
        str(sample): [
            {
                key.replace(" ", "_"): (
                    str(value) if key in ["Sample_name", "Sample name"] else value
                )
                for key, value in feature.items()
            }
            for feature in features
        ]
        for sample, features in runs_from_proj_creation.items()
    }


def create_user_list(string, current_user, add_current_user=True):
    # user_list = str.split(',')
    if add_current_user:
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


def get_projects_close_cursor(query):
    """
    Querys the mongo database and closes the cursor after query is complete. 
    Returns a list of projects of the query with linkid set.

    A cursor is a pointer to the result set of a query in MongoDb
    https://stackoverflow.com/questions/36766956/what-is-a-cursor-in-mongodb
    """
    with collection_handle.find(query) as cursor:
        # Get projects and set linkid in one pass
        projs = []
        for proj in cursor:
            proj['linkid'] = proj['_id']
            projs.append(proj)
    cursor.close()

    return projs
