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
    get_latest_project_version
)
from .extra_metadata import *

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
        file_fps = [uploaded_file.name]
        url = f'http://127.0.0.1:8000/project/{project["linkid"]}/download'
        download_path = project_data_path + '/download.tar.gz'
        try:
            ## try to download old project file
            print(f"PREVIOUS FILE FPS LIST: {file_fps}")

            download = download_file(url, download_path)
            file_fps.append(os.path.join('download.tar.gz'))

        except:
            logging.error("Could not download existing project data for aggregation")

        logging.error(f"file_fps are {file_fps}")
        try:
            temp_directory = os.path.join('./tmp/', str(api_id))
            agg = Aggregator(file_fps, temp_directory, tmp_project_data_path, 'No', "", 'python3', uuid=str(api_id))
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
                    'linkid': str(project['linkid'])
                })
                # transfer the view and downloads counts to the new project version
                views = project['views']
                downloads = project['downloads']
                old_subscribers = project.get('subscribers', [])
                form_data = {
                    'project_name': project.get('project_name', ''),
                    'publication_link': project.get('publication_link', ''),
                    'description': project.get('description', ''),
                    'private': project.get('private', True),
                    'project_members': ','.join(project.get('project_members', [])),
                    'alias': project.get('alias_name', ''),
                    'accept_license': True
                }
                form = RunForm(form_data)
                if not form.is_valid():
                    logging.error(form.errors)
                extra_metadata_file_fp = None
                ## get extra metadata from csv first (if exists in old project), add it to the new proj
                old_extra_metadata = get_extra_metadata_from_project(project)
                new_id = _create_project(form, request, extra_metadata_file_fp, previous_versions=new_prev_versions,
                                         previous_views=[views, downloads], old_extra_metadata = old_extra_metadata, old_subscribers = old_subscribers)
                if new_id is not None:
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

