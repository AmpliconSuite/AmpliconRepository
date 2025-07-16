import logging

import pandas as pd
from bson import ObjectId
from pymongo import MongoClient,ReadPreference
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
import re
import os
import time

def get_db_handle(db_name, host, read_preference=ReadPreference.SECONDARY_PREFERRED):
    try:
        client = MongoClient(
            host,
            read_preference=read_preference,
            maxPoolSize=50,
            minPoolSize=10,
            maxIdleTimeMS=45000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            waitQueueTimeoutMS=2500,  # Wait max 2.5 seconds for available connection
            retryWrites=True,
            retryReads=True,
            w='majority',
            wtimeoutMS=5000
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

db_handle, mongo_client = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI_SECRET'])
db_handle_primary, mongo_client_primary = get_db_handle(os.getenv('DB_NAME', default='caper'), os.environ['DB_URI_SECRET'], read_preference=ReadPreference.PRIMARY)



collection_handle = get_collection_handle(db_handle,'projects')
collection_handle_primary = get_collection_handle(db_handle_primary,'projects')

def get_all_alias():
    """
    Gets all alias names in the db
    """
    return collection_handle.distinct('alias_name')

def get_all_projects():
    """
    Gets all alias names in the db
    """
    return collection_handle.distinct('project_name')

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

def validate_project(project, project_name):
    """
    Checks the following for a project:
    1. if keys in project[runs] all contain underscores, if not, replace them with underscores, insert into db
    2. Checks if Cancer_type exists. if not, initialize to None
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

def replace_underscore_keys(runs_from_proj_creation):
    """
    Replaces spaces with underscores in the keys from runs at project creation step.
    Returns a new dictionary with transformed keys.
    """
    return {
        sample: [
            {key.replace(" ", "_"): value for key, value in feature.items()}
            for feature in features
        ]
        for sample, features in runs_from_proj_creation.items()
    }

def prepare_project_linkid(project):
    project['linkid'] = project['_id']


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