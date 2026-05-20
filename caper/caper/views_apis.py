"""
API views for the Caper application.
This module contains all API endpoints for file uploads and project management.
"""

import logging
import os
import uuid
import tarfile

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.db.models import Q

from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework import status

from threading import Thread

from .serializers import FileSerializer
from .forms import RunForm
from .utils import (
    collection_handle, get_one_project, form_to_dict,
    get_latest_project_version, normalize_visibility_field, is_project_private,
    fs_handle,
)
from .extra_metadata import *
from .background_tasks import get_background_task_status

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
            current_user = request.POST['project_members'][0]
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
        from .views import (
            create_project_helper, extract_project_files,
            upload_file_to_s3
        )
        
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
        samples_to_remove = []
        extract_thread = Thread(target=extract_project_files, args=(tarfile, file_location, project_data_path, new_id.inserted_id, None, None, samples_to_remove))
        extract_thread.start()

        if settings.USE_S3_DOWNLOADS:
            # load the zip asynch to S3 for later use
            file_location = f'{project_data_path}/{request_file.name}'

            s3_thread = Thread(target=upload_file_to_s3, args=(f'{request_file.name}', f'{new_id.inserted_id}/{new_id.inserted_id}.tar.gz'))
            s3_thread.start()


@method_decorator(csrf_exempt, name='dispatch')
class ProjectFileAddView(APIView):
    parser_class = (MultiPartParser,)
    permission_classes = []

    def post(self, request, format=None):
        project_uuid = request.data.get('project_uuid')
        project_key = request.data.get('project_key')
        username = request.data.get('username')

        # Validate project exists
        project = get_one_project(project_uuid)
        if not project:
            return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        # If project is not current, get the latest version.  check that the user is still a member of the latest version
        # but compare the project key to the original project version submitted.  This allows a user to run several
        # additions of samples without going back and forth to get the updated uuid and key after each one
        if not project.get('current', True) or project.get('delete', False):
            # get the latest
            latest_proj = get_latest_project_version(project)
            original_project = project
            project = latest_proj
        else:
            original_project = project

        # Validate user is a project member
        # Get the identifier (could be username or email)
        user_identifier = request.data.get('username')

        # Get all usernames and emails from project members
        project_members = project.get('project_members', [])
        user_objs = User.objects.filter(username__in=project_members) | User.objects.filter(email__in=project_members)
        member_identifiers = set([u.username for u in user_objs] + [u.email for u in user_objs])

        if user_identifier not in member_identifiers:
            return Response({'error': 'User not authorized'}, status=status.HTTP_403_FORBIDDEN)

        # Validate project key
        if original_project.get('privateKey') != project_key:
            return Response({'error': 'Invalid project key'}, status=status.HTTP_403_FORBIDDEN)

        #  file handling , save the new file to a temp dir
        api_id = uuid.uuid4().hex
        tmp_project_data_path = f"tmp/{api_id}"

        uploaded_file = request.FILES['file']
        os.makedirs(tmp_project_data_path, exist_ok=True)
        file_path = os.path.join(tmp_project_data_path, uploaded_file.name)

        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        # Start file processing in a background thread
        file_process_thread = Thread(
            target=self.process_file_in_background,
            args=(request, project, username, uploaded_file, api_id)
        )
        file_process_thread.start()


        return Response({'message': 'Files uploaded successfully and submitted for processing.'}, status=status.HTTP_200_OK)


    def process_file_in_background(self, request, project, username, uploaded_file, api_id):
        from django.core.files.uploadedfile import TemporaryUploadedFile
        # sys.path is already primed for AGGREGATOR_DEV_PATH in settings.py
        from AmpliconSuiteAggregator import Aggregator
        from .views import (
            project_update, project_delete, download_file, 
            _create_project
        )
        
        project_uuid = project['linkid']
        tmp_project_data_path = f"tmp/{api_id}"
        user_identifier = request.data.get('username')
        user = User.objects.get(Q(username=user_identifier) | Q(email=user_identifier))



        alert_message = None
        project_data_path = tmp_project_data_path
        uploaded_file_path = os.path.join(tmp_project_data_path, uploaded_file.name)
        file_fps = [uploaded_file_path]
        url = f'http://127.0.0.1:8000/project/{project["linkid"]}/download'
        download_path = os.path.join(project_data_path, 'download.tar.gz')
        try:
            ## try to download old project file
            print(f"PREVIOUS FILE FPS LIST: {file_fps}")

            download = download_file(url, download_path)
            file_fps.append(download_path)

        except:
            logging.error("Could not download existing project data for aggregation")

        logging.error(f"file_fps are {file_fps}")
        try:
            temp_directory = os.path.join('./tmp/', str(api_id))
            agg = Aggregator(
                input_paths=file_fps,
                project_name=str(api_id),
                work_dir=temp_directory,
            )
            if not agg.completed:
                ## redirect to edit page if aggregator fails
                alert_message = "Edit project failed. Please ensure all uploaded samples have the same reference genome and are valid AmpliconSuite results."
            else:

                with open(agg.aggregated_filename, 'rb') as f:
                    temp_file = TemporaryUploadedFile(
                        name=os.path.basename(agg.aggregated_filename),
                        content_type='application/gzip',
                        size=os.path.getsize(agg.aggregated_filename),
                        charset=None
                    )
                    # Copy file in chunks
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        temp_file.write(chunk)
                    temp_file.seek(0)
                    # Need to flush to ensure all data is written to the temp file
                    temp_file.file.flush()
                    request.FILES['document'] = temp_file

                # Explicitly delete aggregator object after use to free memory
                del agg
                agg = None

                self.request.user = user
                update_project = project_update(self.request, project_uuid)
                delete_project = project_delete(self.request, project_uuid)

                new_prev_versions = []
                if 'previous_versions' in project:
                    new_prev_versions = project['previous_versions']

                new_prev_versions.append({
                    'date': str(project['date']),
                    'linkid': str(project['linkid']),
                    'ASP_version': project.get('ASP_version', 'NA'),
                    'AA_version': project.get('AA_version', 'NA'),
                    'AC_version': project.get('AC_version', 'NA'),
                    'aggregator_version': project.get('aggregator_version', 'NA'),
                })
                # transfer the view and downloads counts to the new project version
                views = project['views']
                downloads = project['downloads']
                old_subscribers = project.get('subscribers', [])
                form_data = {
                    'project_name': project.get('project_name', ''),
                    'publication_link': project.get('publication_link', ''),
                    'description': project.get('description', ''),
                    'private': normalize_visibility_field(project.get('private', 'private')),
                    'project_members': ','.join(project.get('project_members', [])),
                    'alias': project.get('alias_name', ''),
                    'accept_license': True
                }
                form = RunForm(form_data)
                if not form.is_valid():
                    logging.error(f"Form validation failed in process_file_in_background: {form.errors}")
                    alert_message = "Edit project failed. Form validation error - please check project information."
                    # project_delete already removed old project stats; restore them so the
                    # site statistics are not left in a permanently decremented state.
                    from .site_stats import add_project_to_site_statistics
                    try:
                        vis = normalize_visibility_field(project.get('private', 'private'))
                        add_project_to_site_statistics(project, is_project_private(vis))
                        logging.error("Restored old project stats after form validation failure")
                    except Exception as stats_err:
                        logging.error(f"Failed to restore stats after form validation failure: {stats_err}")
                    raise ValueError(alert_message)

                extra_metadata_file_fp = None
                ## get extra metadata from csv first (if exists in old project), add it to the new proj
                old_extra_metadata = get_extra_metadata_from_project(project)
                new_id = _create_project(form, request, extra_metadata_file_fp, previous_versions=new_prev_versions,
                                         previous_views=[views, downloads], old_extra_metadata = old_extra_metadata, old_subscribers = old_subscribers)
                if new_id is None:
                    # _create_project failed after project_delete already removed the old project's
                    # stats — restore them so the site statistics are not permanently wrong.
                    from .site_stats import add_project_to_site_statistics
                    try:
                        vis = normalize_visibility_field(project.get('private', 'private'))
                        add_project_to_site_statistics(project, is_project_private(vis))
                        logging.error("Restored old project stats after _create_project failure")
                    except Exception as stats_err:
                        logging.error(f"Failed to restore stats after _create_project failure: {stats_err}")
                    alert_message = "Edit project failed. Could not create new project version."
                elif new_id is not None:
                    new_project_uuid = str(new_id.inserted_id)
                    alert_message = f"Aggregation successful. New samples added to project version: {new_project_uuid}"

                    # Notify subscribers about the project update
                    try:
                        from .user_preferences import notify_subscribers_of_project_update
                        new_project = get_one_project(new_project_uuid)
                        new_sample_count = new_project.get('sample_count', len(new_project.get('runs', {})))
                        notify_subscribers_of_project_update(project, new_id.inserted_id, new_sample_count)
                    except Exception as notify_error:
                        logging.error(f"Failed to notify subscribers of project update: {str(notify_error)}")
        except Exception as e:
            logging.error(e)
            alert_message = "Edit project failed. Error performing aggregation. Please ensure all uploaded samples are valid AmpliconSuite results."

        logging.error("Preparing to send  email with status: "+ alert_message)
        form_dict = {}
        # add details for the template
        form_dict['SITE_TITLE'] = settings.SITE_TITLE
        form_dict['SITE_URL'] = settings.SITE_URL
        form_dict['sharing_user_email'] = user.email
        form_dict['project_name'] = project.get('project_name', project_uuid)
        form_dict['project_id'] = project_uuid
        form_dict['alert_message'] = alert_message

        html_message = render_to_string('contacts/project_api_file_added.html', form_dict)
        plain_message = strip_tags(html_message)

        # send_mail(subject = subject, message = body, from_email = settings.EMAIL_HOST_USER_SECRET, recipient_list = [settings.RECIPIENT_ADDRESS])
        email = EmailMessage(
            f"Project update on {form_dict['project_name']}",
            html_message,
            settings.EMAIL_HOST_USER,
            [user.email],
            reply_to=[settings.EMAIL_HOST_USER]
        )
        email.content_subtype = "html"
        email.send(fail_silently=False)
        logging.error("Finished email sent")


# ===========================================================================
# REST API v1 — programmatic / curl access
# ===========================================================================

from bson.objectid import ObjectId
from django.http import StreamingHttpResponse, HttpResponseRedirect
from rest_framework.authentication import TokenAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed


# ── Auth + access-control helpers ───────────────────────────────────────────

def _authenticate_api_request(request):
    """
    Inspect the Authorization header for a DRF Token.

    Returns:
      (user, None)       — valid token
      (None, None)       — no token present (anonymous caller)
      (None, Response)   — token present but invalid; caller must return this
    """
    auth = TokenAuthentication()
    try:
        result = auth.authenticate(request)
        return (result[0], None) if result else (None, None)
    except AuthenticationFailed as exc:
        return (None, Response({'error': str(exc)}, status=status.HTTP_401_UNAUTHORIZED))


def _user_can_access_project(project, user):
    """Return True if user (Django User or None for anonymous) may read project."""
    vis = normalize_visibility_field(project.get('private', 'private'))
    if not is_project_private(vis):
        return True
    if user is None:
        return False
    members = project.get('project_members', [])
    return user.username in members or (user.email and user.email in members)


_SAFE_PREV_VERSION_KEYS = frozenset({
    'date', 'linkid', 'AA_version', 'AC_version', 'ASP_version', 'aggregator_version'
})
_SAMPLE_SKIP_FIELDS = frozenset({'Features', 'Sample_metadata_JSON', 'Sample_files_JSON'})


def _project_to_dict(project):
    """Serialize a MongoDB project document to a JSON-safe dict, omitting internal fields."""
    linkid = str(project.get('linkid') or project.get('_id', ''))
    return {
        'id':                 linkid,
        'project_name':       project.get('project_name', ''),
        'description':        project.get('description', ''),
        'sample_count':       project.get('sample_count', 0),
        'visibility':         normalize_visibility_field(project.get('private', 'private')),
        'date':               str(project.get('date', '')),
        'publication_link':   project.get('publication_link', ''),
        'creator':            project.get('creator', ''),
        'reference_genome':   project.get('Genome_build', project.get('reference_genome', '')),
        'AA_version':         project.get('AA_version', ''),
        'AC_version':         project.get('AC_version', ''),
        'ASP_version':        project.get('ASP_version', ''),
        'aggregator_version': project.get('aggregator_version', ''),
        'oncogenes':          project.get('Oncogenes', []),
        'classifications':    project.get('Classifications', []),
        'previous_versions': [
            {k: v for k, v in pv.items() if k in _SAFE_PREV_VERSION_KEYS}
            for pv in project.get('previous_versions', [])
        ],
    }


def _sample_to_dict(sample, run_name):
    """Serialize a sample dict, omitting GridFS-ID fields that are useless externally."""
    d = {'run': run_name}
    for k, v in sample.items():
        if k not in _SAMPLE_SKIP_FIELDS:
            d[k] = v
    return d


# ── GET /api/v1/projects/ ────────────────────────────────────────────────────

class ProjectListView(APIView):
    """
    List all projects visible to the caller.

    Unauthenticated callers see public projects only.
    Authenticated callers (API token) also see private/hidden projects where
    they are listed in project_members.

    Optional query parameter:
        ?name=<substring>   case-insensitive filter on project_name

    curl example:
        curl https://ampliconrepository.org/api/v1/projects/ \\
             -H "Authorization: Token <your-token>"
    """
    permission_classes = []

    def get(self, request):
        user, err = _authenticate_api_request(request)
        if err:
            return err

        name_filter = request.query_params.get('name', '').strip()
        name_q = {'$regex': name_filter, '$options': 'i'} if name_filter else None

        public_q = {'private': {'$in': [False, 'public']}, 'delete': False, 'current': True}
        if name_q:
            public_q['project_name'] = name_q
        projects = list(collection_handle.find(public_q))

        if user is not None:
            private_q = {
                'private': {'$in': [True, 'private', 'hidden_public']},
                '$or': [{'project_members': user.username}, {'project_members': user.email}],
                'delete': False,
                'current': True,
            }
            if name_q:
                private_q['project_name'] = name_q
            seen = {str(p['_id']) for p in projects}
            for p in collection_handle.find(private_q):
                if str(p['_id']) not in seen:
                    projects.append(p)

        for p in projects:
            if 'linkid' not in p:
                p['linkid'] = str(p['_id'])

        return Response([_project_to_dict(p) for p in projects])


# ── GET /api/v1/projects/<project_id>/ ──────────────────────────────────────

class ProjectDetailView(APIView):
    """
    Return metadata for a single project.

    curl example:
        curl https://ampliconrepository.org/api/v1/projects/<id>/ \\
             -H "Authorization: Token <your-token>"
    """
    permission_classes = []

    def get(self, request, project_id):
        user, err = _authenticate_api_request(request)
        if err:
            return err

        project = get_one_project(project_id)
        if not project:
            return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)
        if not _user_can_access_project(project, user):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        if 'linkid' not in project:
            project['linkid'] = str(project['_id'])
        return Response(_project_to_dict(project))


# ── GET /api/v1/projects/<project_id>/samples/ ───────────────────────────────

class ProjectSamplesView(APIView):
    """
    Return the list of samples (with metadata) for a project.

    curl example:
        curl https://ampliconrepository.org/api/v1/projects/<id>/samples/ \\
             -H "Authorization: Token <your-token>"
    """
    permission_classes = []

    def get(self, request, project_id):
        user, err = _authenticate_api_request(request)
        if err:
            return err

        project = get_one_project(project_id)
        if not project:
            return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)
        if not _user_can_access_project(project, user):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        samples = []
        for run_name, run_samples in project.get('runs', {}).items():
            for sample in run_samples:
                samples.append(_sample_to_dict(sample, run_name))

        return Response(samples)


# ── GET /api/v1/projects/<project_id>/download/ ──────────────────────────────

class ProjectDownloadView(APIView):
    """
    Download the project tar.gz archive.

    S3-backed deployments: returns HTTP 302 to a time-limited presigned URL.
    Local-storage deployments: streams the file from GridFS.

    curl example (follow redirect with -L):
        curl -L -O https://ampliconrepository.org/api/v1/projects/<id>/download/ \\
             -H "Authorization: Token <your-token>"
    """
    permission_classes = []

    def get(self, request, project_id):
        from django.conf import settings as django_settings

        user, err = _authenticate_api_request(request)
        if err:
            return err

        project = get_one_project(project_id)
        if not project:
            return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)
        if not _user_can_access_project(project, user):
            return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)

        tar_id = project.get('tarfile')
        if not tar_id:
            return Response(
                {'error': 'Project has no downloadable archive yet'},
                status=status.HTTP_404_NOT_FOUND,
            )

        linkid = str(project.get('linkid') or project.get('_id', ''))
        filename = f"{project.get('project_name', linkid)}.tar.gz"

        # S3 path — redirect to a presigned URL
        if getattr(django_settings, 'USE_S3_DOWNLOADS', False):
            try:
                import boto3
                bucket_path = getattr(django_settings, 'S3_DOWNLOADS_BUCKET_PATH', '')
                s3_key = f'{bucket_path}{linkid}/{linkid}.tar.gz'
                session = boto3.Session(
                    profile_name=getattr(django_settings, 'AWS_PROFILE_NAME', None) or 'default')
                s3client = session.client('s3')
                presigned_url = s3client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': django_settings.S3_DOWNLOADS_BUCKET, 'Key': s3_key},
                    ExpiresIn=600,
                )
                return HttpResponseRedirect(presigned_url)
            except Exception as exc:
                logging.error(f"[API] S3 presigned URL failed for {linkid}: {exc}")
                return Response({'error': 'Download temporarily unavailable'},
                                status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Non-S3 path — stream from GridFS
        try:
            gridfs_file = fs_handle.get(ObjectId(str(tar_id)))

            def _stream():
                while True:
                    chunk = gridfs_file.read(32768)
                    if not chunk:
                        break
                    yield chunk

            response = StreamingHttpResponse(_stream(), content_type='application/gzip')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        except Exception as exc:
            logging.error(f"[API] GridFS stream failed for {linkid}: {exc}")
            return Response({'error': 'Download temporarily unavailable'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)


# ── POST /api/v1/projects/download/ (batch) ──────────────────────────────────

class ProjectBatchDownloadView(APIView):
    """
    Resolve a list of project IDs to individual download URLs in one call.

    Request body: {"ids": ["<id1>", "<id2>", ...]}

    Response:
      {
        "downloads": [
          {"id": "...", "project_name": "...", "download_url": "https://..."},
          ...
        ],
        "skipped": ["<id_not_found_or_inaccessible>", ...]
      }

    The caller then fetches each download_url individually (curl -L -O ...).
    IDs that do not exist, have no archive, or are inaccessible to the caller
    are silently placed in "skipped".

    curl example:
        curl -X POST https://ampliconrepository.org/api/v1/projects/download/ \\
             -H "Authorization: Token <your-token>" \\
             -H "Content-Type: application/json" \\
             -d '{"ids": ["abc123", "def456"]}'
    """
    permission_classes = []

    def post(self, request):
        user, err = _authenticate_api_request(request)
        if err:
            return err

        ids = request.data.get('ids', [])
        if not isinstance(ids, list):
            return Response({'error': "'ids' must be a JSON array"},
                            status=status.HTTP_400_BAD_REQUEST)

        # build_absolute_uri raises DisallowedHost in test environments;
        # construct the base URL directly from META to avoid that.
        scheme = request.META.get('wsgi.url_scheme',
                 'https' if request.META.get('HTTPS') == 'on' else 'http')
        host = request.META.get('HTTP_HOST', '')
        base = f'{scheme}://{host}' if host else ''
        downloads, skipped = [], []

        for pid in ids:
            project = get_one_project(str(pid))
            if not project or not _user_can_access_project(project, user) \
                    or not project.get('tarfile'):
                skipped.append(pid)
                continue
            linkid = str(project.get('linkid') or project.get('_id', ''))
            downloads.append({
                'id':           linkid,
                'project_name': project.get('project_name', ''),
                'download_url': f'{base}/api/v1/projects/{linkid}/download/',
            })

        return Response({'downloads': downloads, 'skipped': skipped})


# ── /api/v1/token/ — token management (browser session) ─────────────────────

class ApiTokenView(APIView):
    """
    Manage the caller's personal API token.

    Must be called from a logged-in browser session (Django session cookie +
    CSRF token).  Not reachable via the API token itself.

    GET    — {"has_token": true,  "token_suffix": "...last8chars"}
             {"has_token": false, "token_suffix": null}
    POST   — generate / regenerate token → {"token": "<full-token>"}
             The full token is returned only on creation; store it securely.
    DELETE — revoke token → {"detail": "API token revoked"}
    """
    # Use session auth but without DRF's per-request CSRF check.  CSRF for this
    # endpoint is the caller's responsibility (the profile page always sends a
    # CSRF cookie; tests set req.user directly and don't have a real session).
    # The endpoint is harmless to CSRF-attack: a cross-origin POST can generate
    # a token but cannot read the response body, so the attacker gains nothing.
    class _NoCsrfSessionAuth(SessionAuthentication):
        def enforce_csrf(self, request):
            pass  # intentionally omitted — see class docstring

    authentication_classes = [_NoCsrfSessionAuth]
    permission_classes = []

    def _session_user(self, request):
        user = request.user
        return user if (user and user.is_authenticated) else None

    def get(self, request):
        user = self._session_user(request)
        if not user:
            return Response({'error': 'Login required'}, status=status.HTTP_401_UNAUTHORIZED)
        from rest_framework.authtoken.models import Token
        try:
            token = Token.objects.get(user=user)
            return Response({'has_token': True, 'token_suffix': token.key[-8:]})
        except Token.DoesNotExist:
            return Response({'has_token': False, 'token_suffix': None})

    def post(self, request):
        user = self._session_user(request)
        if not user:
            return Response({'error': 'Login required'}, status=status.HTTP_401_UNAUTHORIZED)
        from rest_framework.authtoken.models import Token
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)
        return Response({'token': token.key}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        user = self._session_user(request)
        if not user:
            return Response({'error': 'Login required'}, status=status.HTTP_401_UNAUTHORIZED)
        from rest_framework.authtoken.models import Token
        deleted, _ = Token.objects.filter(user=user).delete()
        if deleted:
            return Response({'detail': 'API token revoked'})
        return Response({'detail': 'No active token to revoke'}, status=status.HTTP_404_NOT_FOUND)


class BackgroundTaskStatusView(APIView):
    """
    Public read-only API endpoint that reports whether any project
    create/edit background tasks are currently running in the thread pool.

    GET /api/background-task-status/

    Returns JSON:
    {
        "is_busy": true,
        "active_count": 2,
        "max_workers": 4,
        "tasks": [
            {"id": "...", "label": "Project Edit: <id>", "state": "running", "started_at": "..."},
            ...
        ]
    }

    Example curl usage:
        curl https://example.com/api/background-task-status/
    """

    permission_classes = []  # publicly accessible – read-only and contains no sensitive data

    def get(self, request):
        task_status = get_background_task_status()
        return Response(task_status, status=status.HTTP_200_OK)


