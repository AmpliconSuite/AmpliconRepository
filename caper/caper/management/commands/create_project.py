import os
import sys
from django.core.management.base import BaseCommand
from django.http import HttpRequest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile

from caper.views import create_project


class Command(BaseCommand):
    help = 'Create a project from command line by calling the create_project view'

    def add_arguments(self, parser):
        parser.add_argument('project_name', type=str, help='Name of the project')
        parser.add_argument('username', type=str, help='Username of the project owner')
        parser.add_argument('file_path', type=str, help='Path to the tar.gz file to upload')
        parser.add_argument('--description', type=str, default='Created via command line', 
                          help='Project description')
        parser.add_argument('--private', action='store_true', default=True, 
                          help='Whether the project is private (default: True)')
        parser.add_argument('--members', type=str, default='', 
                          help='Comma-separated list of additional project members')
        parser.add_argument('--alias', type=str, default='', 
                          help='Project alias')
        parser.add_argument('--publication', type=str, default='', 
                          help='Publication link')
    
    def handle(self, *args, **options):
        project_name = options['project_name']
        username = options['username']
        file_path = options['file_path']
        description = options['description']
        private = options['private']
        members = options['members']
        alias = options['alias']
        publication = options['publication']

        # Validate that file exists
        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f'File not found: {file_path}'))
            return

        # Validate that user exists
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'User not found: {username}'))
            return

        # Create mock request object
        request = HttpRequest()
        request.method = 'POST'
        request.user = user
        
        # Add POST data
        request.POST = {
            'project_name': project_name,
            'description': description,
            'private': private,
            'project_members': username + (f', {members}' if members else ''),
            'alias': alias,
            'publication_link': publication,
            'accept_license': True
        }
        
        # Add file data
        with open(file_path, 'rb') as f:
            file_content = f.read()
            
        file_name = os.path.basename(file_path)
        uploaded_file = SimpleUploadedFile(
            name=file_name,
            content=file_content,
            content_type='application/gzip'
        )
        
        # Add FILES data
        request.FILES = {'document': uploaded_file}
        
        # Call the view function
        self.stdout.write(self.style.SUCCESS(f'Creating project "{project_name}" for user {username}...'))
        
        try:
            response = create_project(request)
            if response:
                status_code = response.status_code
                redirect_url = response.url if hasattr(response, 'url') else None
                self.stdout.write(self.style.SUCCESS(f'Project created successfully! Status code: {status_code}'))
                if redirect_url:
                    self.stdout.write(self.style.SUCCESS(f'Redirected to: {redirect_url}'))
            else:
                self.stdout.write(self.style.WARNING('No response received from create_project function'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error creating project: {str(e)}'))
            import traceback
            traceback.print_exc()
