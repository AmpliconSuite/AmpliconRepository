import logging
from collections import Counter

import pandas as pd
from bson import ObjectId
import pymongo
from pymongo import MongoClient,ReadPreference
from pymongo.errors import ConnectionFailure, PyMongoError, ServerSelectionTimeoutError
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

# One authority for what a project document is.  Every query below that used to
# spell out 'delete'/'current' by hand now names the predicate it means, so the
# resolver and the tools that must agree with it read from the same table.
from .project_status import (
    HEAD_VERSION_QUERY,
    NOT_DELETED_QUERY,
    PRIOR_VERSION_QUERY,
    DELETE_FLAG_QUERY,
    STATUS_QUERIES,
    LIVE,
    TOMBSTONE,
    combine,
    iter_lineage_references,
    iter_previous_versions,
    status_query,
)

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
            # Per-socket-operation stall limit, NOT a cap on total operation
            # time: a GridFS stream does many small reads, each of which gets a
            # fresh budget, so long downloads are unaffected as long as bytes
            # keep flowing.  This must never be None -- a stalled read with no
            # timeout blocks a gunicorn worker forever, which is what turned a
            # transient database slowdown into a 10-hour outage in Aug 2026.
            socketTimeoutMS=int(os.getenv('MONGO_SOCKET_TIMEOUT_MS', '120000')),
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
audit_log_handle = get_collection_handle(db_handle_primary,'project_audit_log')

fs_handle = gridfs.GridFS(db_handle)


def get_project_version_chain(search_term):
    """
    Given a project name (current or historical) or UUID, return:
      - a list of all project UUIDs in the version chain (strings)
      - the display name to use for the chain

    Traverses previous_versions links so that audit-log queries span all
    versions and renames of the same logical project.
    """
    uuids = set()
    display_name = search_term

    # Try to find a current project matching the term
    project = None
    try:
        project = collection_handle_primary.find_one(
            combine(NOT_DELETED_QUERY, _id=ObjectId(search_term))
        )
    except Exception:
        pass

    if project is None:
        project = collection_handle_primary.find_one(
            combine(NOT_DELETED_QUERY, alias_name=search_term)
        )
    if project is None:
        # case-insensitive name search, pick the most recent current one
        project = collection_handle_primary.find_one(
            status_query(LIVE, project_name={'$regex': f'^{re.escape(search_term)}$',
                                             '$options': 'i'})
        )
    if project is None:
        # fall back to any project (including old versions / deleted) with that name
        project = collection_handle_primary.find_one(
            {'project_name': {'$regex': f'^{re.escape(search_term)}$', '$options': 'i'}}
        )

    if project is None:
        return list(uuids), display_name

    display_name = project.get('project_name', search_term)

    # Collect UUID of this project
    uuids.add(str(project['_id']))

    # Collect all previous_versions UUIDs embedded in this document
    for pv in project.get('previous_versions', []):
        pv_id = pv.get('linkid')
        if pv_id:
            uuids.add(str(pv_id))

    # Also look forward: find any current project that lists this one in its previous_versions
    descendants = list(collection_handle_primary.find(
        {'previous_versions.linkid': str(project['_id'])},
        {'_id': 1, 'project_name': 1, 'previous_versions': 1}
    ))
    for desc in descendants:
        uuids.add(str(desc['_id']))
        display_name = desc.get('project_name', display_name)
        for pv in desc.get('previous_versions', []):
            pv_id = pv.get('linkid')
            if pv_id:
                uuids.add(str(pv_id))

    return list(uuids), display_name


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

    _sentinel_fields = {'AA_amplicon_number', 'Classification'}
    _na_strings = {'NA', 'None', 'Not Provided', ''}

    # sample_data.sort(key=lambda x: (int(x['AA_amplicon_number']), x['Feature_ID']))
    for feature in sample_data:
        for key, value in feature.items():
            if type(value) == float:
                if key == 'AA_amplicon_number':
                    feature[key] = int(value)

                else:
                    feature[key] = round(value, 1)

            elif key in _sentinel_fields and type(value) == str and value in _na_strings:
                feature[key] = None

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


def page_query_timeout():
    """Deadline (seconds) for database work behind a page render.

    ``socketTimeoutMS`` only bounds a single socket operation, so it cannot cap
    a query that is slow for any other reason.  This is a true deadline for the
    whole operation (server selection, pool checkout and every socket op), which
    is what guarantees a worker comes back instead of wedging.

    Sized for recoverability, not for trimming tail latency: normal sample
    lookups run well under a second, so this is a large multiple of healthy
    latency and should only ever fire when something is genuinely wrong.
    Tune with MONGO_PAGE_TIMEOUT_SECONDS once you have measured the real
    distribution (grep '[PERF] get_one_sample took' in the app log).
    """
    return float(os.getenv('MONGO_PAGE_TIMEOUT_SECONDS', '20'))


def _fetch_sample_slice(match, sample_name):
    """Fetch one sample's feature rows plus a compact index of every sample name.

    Asks the server for just the matching run and a ``{run key -> first
    Sample_name}`` index (a few bytes per sample), instead of transferring the
    whole ``runs`` dict.  Returns ``(rows, prev_name, next_name)``.
    """
    pipeline = [
        {'$match': match},
        {'$limit': 1},
        {'$project': {
            '_sample_index': {'$map': {
                'input': {'$objectToArray': '$runs'},
                'as': 'r',
                'in': {'k': '$$r.k',
                       's': {'$arrayElemAt': ['$$r.v.Sample_name', 0]}},
            }},
            '_matched': {'$filter': {
                'input': {'$objectToArray': '$runs'},
                'as': 'r',
                'cond': {'$eq': [{'$arrayElemAt': ['$$r.v.Sample_name', 0]},
                                 sample_name]},
            }},
        }},
    ]
    doc = next(iter(collection_handle.aggregate(pipeline)), None)
    if doc is None:
        return None, None, None

    # Sorted by run key so prev/next ordering matches the previous implementation.
    index = sorted(
        ((entry.get('k'), entry.get('s')) for entry in doc.get('_sample_index') or []),
        key=lambda kv: kv[0],
    )
    position = next(
        (i for i, (_, name) in enumerate(index) if name == sample_name), None)
    if position is None:
        return None, None, None

    matched_key = index[position][0]
    rows = next((entry.get('v') for entry in doc.get('_matched') or []
                 if entry.get('k') == matched_key), None)

    prev_name = index[position - 1][1] if position > 0 else None
    next_name = index[position + 1][1] if position < len(index) - 1 else None
    return rows, prev_name, next_name


def get_one_sample_rows(project_name, sample_name):
    """Return ``(project, sample_rows)`` without building the sample-name index.

    ``_fetch_sample_slice()`` maps over every run in the project to build the
    ``{run key -> Sample_name}`` index that drives the prev/next links on the
    sample page.  On a 2,471-sample project that is 2,471 elements assembled
    server-side per request, and it measured ~1.4s on the metadata download
    route -- which needs none of it, having no navigation to render.

    Callers that only want one sample's rows should use this.  Callers that
    render prev/next still need get_one_sample().  Falls back to the full
    lookup on any server-side failure, so a query the backing engine dislikes
    degrades to slower rather than broken.
    """
    try:
        with pymongo.timeout(page_query_timeout()):
            project = get_one_project_sans_runs(project_name)
            if project is None:
                return validate_project(None, project_name), None

            pipeline = [
                {'$match': {'_id': project['_id']}},
                {'$limit': 1},
                {'$project': {
                    '_matched': {'$filter': {
                        'input': {'$objectToArray': '$runs'},
                        'as': 'r',
                        'cond': {'$eq': [
                            {'$arrayElemAt': ['$$r.v.Sample_name', 0]},
                            sample_name]},
                    }},
                }},
            ]
            doc = next(iter(collection_handle.aggregate(pipeline)), None)
    except PyMongoError as exc:
        if getattr(exc, 'timeout', False):
            logging.warning(
                "get_one_sample_rows: timed out after %ss loading %s/%s",
                page_query_timeout(), project_name, sample_name)
            raise
        logging.warning(
            "get_one_sample_rows: server-side lookup failed for %s (%s); "
            "falling back", project_name, exc)
        project, rows, _, _ = get_one_sample(project_name, sample_name)
        return project, rows

    if doc is None:
        return project, None

    matched = doc.get('_matched') or []
    rows = matched[0].get('v') if matched else None
    if rows:
        rows = replace_space_to_underscore(rows)
    return project, rows


def _get_one_sample_full_scan(project_name, sample_name):
    """Original whole-document implementation, kept as a fallback.

    Used only if the server-side slice above fails (e.g. an aggregation operator
    the backing engine does not support), so a server quirk degrades performance
    rather than breaking sample pages outright.
    """
    project = validate_project(get_one_project(project_name), project_name)
    prepare_project_linkid(project)

    runs = project['runs']
    sample_keys = sorted(runs.keys())

    sample_out = None
    prev_sample = None
    next_sample = None
    current_index = None

    for idx, sample_num in enumerate(sample_keys):
        current = runs[sample_num]
        try:
            if len(current) > 0 and current[0]['Sample_name'] == sample_name:
                sample_out = current
                current_index = idx
                break
        except Exception:
            # should not get here but we do sometimes for new projects, issue 194
            pass

    if current_index is not None:
        if current_index > 0:
            prev_sample = runs[sample_keys[current_index - 1]]
        if current_index < len(sample_keys) - 1:
            next_sample = runs[sample_keys[current_index + 1]]

    return project, sample_out, prev_sample, next_sample


def get_one_sample(project_name, sample_name):
    """Return ``(project, sample_rows, prev_sample, next_sample)`` for one sample.

    The project document is returned WITHOUT ``runs``.  Loading ``runs`` — every
    sample and every feature row in the project — to render a single sample page
    pulled megabytes from the database per request and was the read
    amplification behind the repeated production outages of 2026-07/08.
    No caller of this function uses ``project['runs']``.

    ``prev_sample``/``next_sample`` are name-only stubs: the sole consumer
    (``views.sample_page``) reads ``[0]['Sample_name']`` from them for the
    prev/next navigation links.
    """
    try:
        with pymongo.timeout(page_query_timeout()):
            # get_one_project_sans_runs() carries the full lookup semantics —
            # ObjectId, alias, project name, older versions (current=False) and
            # deleted-version redirect tombstones — while excluding runs.
            project = get_one_project_sans_runs(project_name)

            if project is None:
                # Preserve the previous not-found behaviour (log, return None).
                return validate_project(None, project_name), None, None, None

            # Match on the RESOLVED id: for a tombstone redirect this is the
            # surviving project, which is not the id in the URL.
            rows, prev_name, next_name = _fetch_sample_slice(
                {'_id': project['_id']}, sample_name)
    except PyMongoError as exc:
        # ExecutionTimeout subclasses OperationFailure, so the error type alone
        # cannot tell a deadline from an unsupported aggregation operator.
        # Let deadlines propagate — falling back to the slower whole-document
        # scan is the last thing a database under stress needs.
        if getattr(exc, 'timeout', False):
            logging.warning(
                "get_one_sample: timed out after %ss loading %s/%s",
                page_query_timeout(), project_name, sample_name)
            raise
        logging.warning(
            "get_one_sample: server-side sample lookup failed for %s (%s); "
            "falling back to a full project scan", project_name, exc)
        return _get_one_sample_full_scan(project_name, sample_name)

    # validate_project() used to normalise space-containing keys for every
    # caller.  It needed the whole runs dict to do that, so apply the same
    # normalisation to just this sample's rows instead.
    if rows:
        rows = replace_space_to_underscore(rows)

    prev_sample = [{'Sample_name': prev_name}] if prev_name is not None else None
    next_sample = [{'Sample_name': next_name}] if next_name is not None else None

    return project, rows, prev_sample, next_sample


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
    query = combine(NOT_DELETED_QUERY, _id=project['_id'])
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
        _invalid_classes = {None, 'NA', 'None', 'Not Provided', ''}
        all_classifications = flatten(subset['Classification'].values.tolist())
        classifications = [c for c in all_classifications if c not in _invalid_classes]
        sample_dict['Classifications'] = list(set(classifications))
        class_counts = Counter(classifications)
        sample_dict['Classifications_counted'] = [
            f"{c} ({count})" if count > 1 else c
            for c, count in sorted(class_counts.items())
        ]
        sample_dict['Features'] = len(classifications) if classifications else 0
        
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
    

def resolve_redirect_tombstone(project, projection=None):
    redirect_to = project.get('redirect_to_project') if project else None
    if not redirect_to:
        return None
    try:
        redirected = collection_handle.find_one(
            combine(NOT_DELETED_QUERY, _id=ObjectId(str(redirect_to))), projection)
        if redirected is not None:
            prepare_project_linkid(redirected)
            return redirected
        redirected = collection_handle.find_one(
            status_query(LIVE, **{'previous_versions.linkid': str(redirect_to)}),
            projection)
        if redirected is not None:
            prepare_project_linkid(redirected)
            return redirected
    except Exception:
        logging.error(f"Could not resolve redirect tombstone {project.get('_id')} to {redirect_to}")
    return None


def get_one_project(project_name_or_uuid):
    """
    Gets one project from name or UUID. 
    
    if name, then checks the DB for an "alias" field, then gets that project if it has one 
    
    """
    
    try:
        project = collection_handle.find(
            combine(NOT_DELETED_QUERY, _id=ObjectId(project_name_or_uuid)))[0]
        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        ## first try finding the alias name
        try:
            project = collection_handle.find(
                combine(NOT_DELETED_QUERY, alias_name=project_name_or_uuid))[0]
            prepare_project_linkid(project)
            return project
        except:
            project = None

        ## then find project via project name
        try:
            project = collection_handle.find_one(
                combine(NOT_DELETED_QUERY, project_name=project_name_or_uuid))
            if project is not None:
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
                prepare_project_linkid(project)
                return project
        except:
            project = None


    ## Maybe we are looking for a superseded version: PRIOR_VERSION_QUERY is the
    ## fallback every cleanup tool has missed.  It matches SUPERSEDED documents
    ## and the TOMBSTONE documents that redirect -- deliberately both, since
    ## excluding tombstones here is what breaks deleted-version redirects.
    if project is None:
        try:
            project = collection_handle.find_one(
                combine(PRIOR_VERSION_QUERY, _id=ObjectId(project_name_or_uuid)))
            if project is not None:
                redirected = resolve_redirect_tombstone(project)
                if redirected is not None:
                    return redirected
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None

    if project is None:
        try:
            project = collection_handle.find_one(
                combine(PRIOR_VERSION_QUERY, project_name=project_name_or_uuid))
            if project is not None:
                redirected = resolve_redirect_tombstone(project)
                if redirected is not None:
                    return redirected
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
        project = collection_handle.find(
            combine(DELETE_FLAG_QUERY, _id=ObjectId(project_name_or_uuid)))[0]

        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        project = collection_handle.find_one(
            combine(NOT_DELETED_QUERY, project_name=project_name_or_uuid))
        logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
        # Only when the backstop found something: the line below used to run
        # unconditionally, so a lookup that found nothing raised TypeError out
        # of this function instead of returning None.  Callers check for None
        # -- the lines just below do -- and never saw one.
        if project is not None:
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


# Minimum AmpliconClassifier version considered current. Projects containing
# results from any AC release older than this are flagged so users know the
# classifications may be out of date.
MIN_CURRENT_AC_VERSION = (2, 0)

# Return values of classify_ac_version().
AC_VERSION_CURRENT = 'current'            # all identifiable AC versions are >= v2
AC_VERSION_OUTDATED = 'outdated'          # at least one identifiable AC version < v2
AC_VERSION_UNIDENTIFIED = 'unidentified'  # no AC version could be identified


def classify_ac_version(ac_version_str):
    """Classify a project's AmpliconClassifier version string.

    AC_version is a comma-separated string of the AC versions detected across a
    project's samples. It may also be a placeholder ('NA', 'None', ...), an empty
    string, or None when no version could be determined. When multiple versions
    are present, each is checked and the most concerning state wins.

    Returns one of:
      AC_VERSION_OUTDATED     - at least one identifiable AC version predates v2.0
      AC_VERSION_UNIDENTIFIED - no AC version could be identified for the project
      AC_VERSION_CURRENT      - all identifiable AC versions are >= v2.0
    """
    identified = False
    for token in str(ac_version_str or '').split(','):
        match = re.search(r'\d+(?:\.\d+)*', token)
        if not match:
            continue
        identified = True
        parts = tuple(int(p) for p in match.group(0).split('.'))
        # Pad/truncate to the threshold length so "2" compares equal to "2.0".
        n = len(MIN_CURRENT_AC_VERSION)
        normalized = (parts + (0,) * n)[:n]
        if normalized < MIN_CURRENT_AC_VERSION:
            return AC_VERSION_OUTDATED
    return AC_VERSION_CURRENT if identified else AC_VERSION_UNIDENTIFIED


VERSION_HISTORY_FIELDS = [
    'ASP_version', 'AA_version', 'AC_version', 'aggregator_version',
    'Reconstruction_tools', 'CoRAL_version',
]
DELETED_VERSION_HISTORY_FIELDS = [
    'version_deleted_from_history',
    'payload_purged',
    'redirect_to_project',
    'delete_date',
]


def _previous_version_entries(project):
    """The history entries stored on *project*, in one readable shape.

    Was a per-entry coercion here that turned anything non-dict into
    ``{'linkid': str(entry)}``.  For the five documents written before the
    April 2024 serialisation change that produced a linkid holding the entry's
    entire JSON text, which the template rendered as a link to
    ``/project/[{"date": ...}]`` and which matched no query.  Decoding lives in
    project_status.iter_previous_versions() so the site and the validator read
    the field the same way.
    """
    return [entry for entry, _encoding in iter_previous_versions(project)]


def _current_version_history_entry(project):
    entry = {
        'date': project.get('date', '1999-01-01T00:00:00.000000'),
        'linkid': str(project.get('linkid', project['_id'])),
    }
    for field in VERSION_HISTORY_FIELDS:
        entry[field] = project.get(field, 'NA')
    return entry


def _deleted_version_history_entry(tombstone):
    entry = {
        'date': tombstone.get('date', '1999-01-01T00:00:00.000000'),
        'linkid': str(tombstone['_id']),
    }
    for field in VERSION_HISTORY_FIELDS:
        entry[field] = tombstone.get(field, 'NA')
    for field in DELETED_VERSION_HISTORY_FIELDS:
        if field in tombstone:
            entry[field] = tombstone[field]
    entry.setdefault('version_deleted_from_history', True)
    entry.setdefault('payload_purged', True)
    return entry


def _version_history_linkids(project):
    linkids = []
    project_id = project.get('_id', project.get('linkid'))
    if project_id:
        linkids.append(str(project_id))
    # Third copy of "read previous_versions[]" that this module used to carry.
    # They disagreed about the pre-April 2024 encoding, which is how five
    # documents ended up with a history table nothing could follow.
    for linkid, _encoding in iter_lineage_references(project):
        linkids.append(linkid)
    return list(dict.fromkeys(linkids))


def _deleted_version_entries_for_project(project):
    project_ids = _version_history_linkids(project)
    if not project_ids:
        return []
    projection_fields = ['date'] + VERSION_HISTORY_FIELDS + DELETED_VERSION_HISTORY_FIELDS
    try:
        cursor = collection_handle.find(
            # Was a hand-written copy of STATUS_QUERIES[TOMBSTONE].  The grep
            # guard only looks for 'delete' and 'current' literals, so it walked
            # past this one; validate_project_lineage.py's I18 found it.
            combine(STATUS_QUERIES[TOMBSTONE],
                    redirect_to_project={'$in': project_ids}),
            {field: 1 for field in projection_fields},
        )
        entries = [_deleted_version_history_entry(doc) for doc in cursor]
        try:
            cursor.close()
        except Exception:
            pass
        return entries
    except Exception as e:
        logging.error(f"Error loading deleted version tombstones for project {project_ids[0]}: {e}")
        return []


def _merge_deleted_version_entries(entries, deleted_entries):
    merged = []
    by_linkid = {}
    for entry in entries:
        linkid = entry.get('linkid')
        if linkid:
            by_linkid[str(linkid)] = entry
        merged.append(entry)
    for deleted_entry in deleted_entries:
        linkid = str(deleted_entry['linkid'])
        if linkid in by_linkid:
            by_linkid[linkid].update(deleted_entry)
        else:
            merged.append(deleted_entry)
    return merged


def _sort_history_entries_newest_first(entries):
    return sorted(entries, key=lambda entry: entry.get('date') or '', reverse=True)


def _backfill_version_info_from_db(entries):
    """
    For previous_versions entries that are missing version fields (or have 'NA'),
    look up the actual old project documents by linkid and populate the real values.
    Uses a single batch MongoDB query for efficiency.
    """
    # Identify entries that need a DB lookup
    entries_needing_lookup = [
        entry for entry in entries
        if isinstance(entry, dict)
        and any(not entry.get(f) or entry.get(f) == 'NA' for f in VERSION_HISTORY_FIELDS)
    ]

    if entries_needing_lookup:
        # Build a map from linkid string -> entry for fast update
        linkid_to_entry = {}
        for entry in entries_needing_lookup:
            linkid = entry.get('linkid')
            if linkid:
                linkid_to_entry[str(linkid)] = entry

        # One unreadable linkid used to take the whole batch down with it: the
        # comprehension below raised on the first bad id, the except swallowed
        # it, and every *other* entry in the same history table silently went
        # unbackfilled and rendered as NA.  Skip the ones that cannot be looked
        # up rather than the ones that can.
        object_ids = []
        for lid in list(linkid_to_entry):
            try:
                object_ids.append(ObjectId(lid))
            except Exception:
                logging.warning(
                    "previous_versions entry has an unusable linkid %r; "
                    "leaving its version columns as NA", lid[:80])
                linkid_to_entry.pop(lid)

        if object_ids:
            try:
                proj_docs = collection_handle.find(
                    {'_id': {'$in': object_ids}},
                    {field: 1 for field in VERSION_HISTORY_FIELDS}
                )
                for doc in proj_docs:
                    doc_id = str(doc['_id'])
                    if doc_id in linkid_to_entry:
                        entry = linkid_to_entry[doc_id]
                        for field in VERSION_HISTORY_FIELDS:
                            if not entry.get(field) or entry.get(field) == 'NA':
                                entry[field] = doc.get(field, 'NA')
            except Exception as e:
                logging.error(f"Error backfilling version info from DB: {e}")

    # Ensure every entry has all four version fields (default 'NA' for anything still absent)
    for entry in entries:
        for field in VERSION_HISTORY_FIELDS:
            entry.setdefault(field, 'NA')


def previous_versions(project):
    """
    Gets a list of previous versions via UUID.
    Version fields (ASP_version, AA_version, AC_version, aggregator_version) are
    populated from the actual old project documents when not already present in the
    stored previous_versions entries.
    """
    res = []
    msg = None
    logging.info(f"Getting previous versions for project {project['_id']}")

    fields = [
        'date', 'previous_versions', 'AC_version', 'AA_version', 'ASP_version',
        'aggregator_version', 'Reconstruction_tools', 'CoRAL_version',
    ]
    cursor = collection_handle.find(
        combine(HEAD_VERSION_QUERY,
                **{'previous_versions.linkid': str(project['_id'])}),
        {field: 1 for field in fields}
    ).sort('date', -1)
    data = list(cursor)
    try:
        cursor.close()
    except Exception:
        pass

    if len(data) == 1:
        # Viewing an older version — data[0] is the current/latest project document
        res = _previous_version_entries(data[0])
        # Populate version fields from the actual old project documents
        _backfill_version_info_from_db(res)
        res = _merge_deleted_version_entries(res, _deleted_version_entries_for_project(data[0]))
        res.append(_current_version_history_entry(data[0]))
        res = _sort_history_entries_newest_first(res)
        msg = (f"Viewing an older version of the project. "
               f"View latest version <a href='/project/{str(data[0]['_id'])}'>here</a>")

    else:
        # Viewing the current version — build history from this project's previous_versions list
        if "previous_versions" in project:
            res = _previous_version_entries(project)
            # Populate version fields from the actual old project documents
            _backfill_version_info_from_db(res)
        res = _merge_deleted_version_entries(res, _deleted_version_entries_for_project(project))
        # Append the current version itself
        res.append(_current_version_history_entry(project))
        res = _sort_history_entries_newest_first(res)

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
        combine(HEAD_VERSION_QUERY,
                **{'previous_versions.linkid': str(project['_id'])}),
    )

    if doc is None:
        return project
    else:
        prepare_project_linkid(doc)
        return doc

def get_one_project_sans_runs(project_name_or_uuid, projection=None):
    """
    Gets one project from name or UUID, excluding the 'runs' field to reduce memory usage.

    if name, then checks the DB for an "alias" field, then gets that project if it has one

    This is useful when you only need project metadata without the full sample/feature data.

    Callers that need even less than "everything but runs" may pass their own
    ``projection``.  Doing that here, rather than in a separate loader, keeps the
    lookup chain below (ObjectId, alias, project name, non-current versions and
    deleted-version redirect tombstones) in one place — it is easy to get wrong.
    Any custom projection must still be an exclusion of unwanted fields, or an
    inclusion that keeps everything the caller and ``prepare_project_linkid()``
    touch (``_id``, ``linkid``, ``delete``, ``current``, ``redirect_to_project``).
    """

    # Projection to exclude the runs field
    if projection is None:
        projection = {'runs': 0}

    try:
        project = collection_handle.find(
            combine(NOT_DELETED_QUERY, _id=ObjectId(project_name_or_uuid)), projection)[0]
        prepare_project_linkid(project)
        return project

    except:
        project = None

    # backstop using the name the old way
    if project is None:
        ## first try finding the alias name
        try:
            project = collection_handle.find(
                combine(NOT_DELETED_QUERY, alias_name=project_name_or_uuid), projection)[0]
            prepare_project_linkid(project)
            return project
        except:
            project = None

        ## then find project via project name
        try:
            project = collection_handle.find_one(
                combine(NOT_DELETED_QUERY, project_name=project_name_or_uuid), projection)
            if project is not None:
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use project name!")
                prepare_project_linkid(project)
                return project
        except:
            project = None


    ## Maybe we are looking for a superseded version -- see get_one_project().
    if project is None:
        try:
            project = collection_handle.find_one(
                combine(PRIOR_VERSION_QUERY, _id=ObjectId(project_name_or_uuid)), projection)
            if project is not None:
                redirected = resolve_redirect_tombstone(project, projection)
                if redirected is not None:
                    return redirected
                prepare_project_linkid(project)
                logging.warning(f"Could not lookup project {project_name_or_uuid}, had to use previous project ids!")

                return project
        except:
            project = None

    if project is None:
        try:
            project = collection_handle.find_one(
                combine(PRIOR_VERSION_QUERY, project_name=project_name_or_uuid), projection)
            if project is not None:
                redirected = resolve_redirect_tombstone(project, projection)
                if redirected is not None:
                    return redirected
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
        query = combine(NOT_DELETED_QUERY, _id=project['_id'])
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


def normalize_visibility_field(private_value):
    """
    Normalize legacy boolean private field to new string visibility format.
    
    For backward compatibility with API calls that use boolean values:
    - True -> 'private'
    - False -> 'public'
    - String values are returned as-is
    
    Args:
        private_value: Boolean (True/False) or string ('private', 'public', 'hidden_public')
    
    Returns:
        String visibility value ('private', 'public', or 'hidden_public')
    """
    if isinstance(private_value, bool):
        return 'private' if private_value else 'public'
    elif isinstance(private_value, str):
        if private_value in ('private', 'public', 'hidden_public'):
            return private_value
        # Handle string representations of booleans (from URL params, etc.)
        if private_value.lower() in ('true', '1', 'yes'):
            return 'private'
        elif private_value.lower() in ('false', '0', 'no'):
            return 'public'
    return 'private'  # Default to private for safety


def is_project_private(visibility):
    """
    Check if a project should be treated as private.
    
    Returns True for 'private' and 'hidden_public' (hidden_public is private
    in terms of statistics and access control, just visible to anyone with the link).
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is private
    """
    return visibility in ('private', 'hidden_public')


def is_project_public(visibility):
    """
    Check if a project is fully public.
    
    Returns True only for 'public'.
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is public
    """
    return visibility == 'public'


def is_project_hidden_public(visibility):
    """
    Check if a project is hidden_public.
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is hidden_public
    """
    return visibility == 'hidden_public'


def format_visibility_for_display(private_value):
    """
    Format visibility value for display to users.
    
    Converts both legacy boolean values and new string values to
    user-friendly display strings.
    
    Args:
        private_value: Boolean (True/False) or string ('private', 'public', 'hidden_public')
    
    Returns:
        Display string: 'Private', 'Public', or 'Hidden Public'
    """
    # First normalize the value
    normalized = normalize_visibility_field(private_value)
    
    # Convert to display format
    if normalized == 'private':
        return 'Private'
    elif normalized == 'public':
        return 'Public'
    elif normalized == 'hidden_public':
        return 'Hidden Public'
    else:
        return 'Private'  # Default fallback
