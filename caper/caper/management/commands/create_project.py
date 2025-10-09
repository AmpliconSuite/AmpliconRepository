import os
import sys
import tempfile
import requests
import boto3
from urllib.parse import urlparse
from botocore.exceptions import ClientError
from django.core.management.base import BaseCommand
from django.http import HttpRequest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.datastructures import MultiValueDict
from django.http import QueryDict

from caper.views import create_project


class Command(BaseCommand):
    help = 'Create a project from command line by calling the create_project view'

    def add_arguments(self, parser):
        parser.add_argument('project_name', type=str, help='Name of the project')
        parser.add_argument('username', type=str, help='Username of the project owner')
        parser.add_argument('file_path', type=str, 
                          help='Path to the tar.gz file to upload (local path, HTTP URL, or S3 URI)')
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
        parser.add_argument('--chunk-size', type=int, default=8192, 
                          help='Chunk size for downloading files (in bytes)')
    
    def handle(self, *args, **options):
        project_name = options['project_name']
        username = options['username']
        file_path = options['file_path']
        description = options['description']
        private = options['private']
        members = options['members']
        alias = options['alias']
        publication = options['publication']
        chunk_size = options['chunk_size']

        # Validate that user exists
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'User not found: {username}'))
            return
        
        # Handle different file sources (local, HTTP, S3)
        temp_file = None
        original_filename = os.path.basename(urlparse(file_path).path) or 'downloaded_file.tar.gz'
        
        # Determine file source and handle accordingly
        if file_path.startswith('http://') or file_path.startswith('https://'):
            # Handle HTTP/HTTPS URL
            self.stdout.write(f"Downloading file from URL: {file_path}")
            temp_file = self._download_from_url(file_path, original_filename, chunk_size)
            if not temp_file:
                return
            file_path = temp_file.name
            
        elif file_path.startswith('s3://'):
            # Handle S3 URI
            self.stdout.write(f"Downloading file from S3: {file_path}")
            temp_file = self._download_from_s3(file_path, original_filename, chunk_size)
            if not temp_file:
                return
            file_path = temp_file.name
            
        else:
            # Handle local file
            if not os.path.exists(file_path):
                self.stdout.write(self.style.ERROR(f'File not found: {file_path}'))
                return
            self.stdout.write(f"Using local file: {file_path}")

        try:
            # Create mock request object
            request = HttpRequest()
            request.method = 'POST'
            request.user = user
            
            # Add POST data - using QueryDict for POST data
            post_data = QueryDict('', mutable=True)
            post_data.update({
                'project_name': project_name,
                'description': description,
                'private': private,
                'project_members': username + (f', {members}' if members else ''),
                'alias': alias,
                'publication_link': publication,
                'accept_license': True
            })
            request.POST = post_data
            
            # Add file data
            with open(file_path, 'rb') as f:
                file_content = f.read()
                
            file_name = original_filename
            uploaded_file = SimpleUploadedFile(
                name=file_name,
                content=file_content,
                content_type='application/gzip'
            )
            
            # Add FILES data using MultiValueDict
            files = MultiValueDict()
            files.appendlist('document', uploaded_file)
            request.FILES = files
            
            # Call the view function
            self.stdout.write(self.style.SUCCESS(f'Creating project "{project_name}" for user {username}...'))
            
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
        finally:
            # Clean up any temporary files
            if temp_file and os.path.exists(temp_file.name):
                try:
                    temp_file.close()
                    os.unlink(temp_file.name)
                    self.stdout.write("Temporary file cleaned up.")
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Warning: Could not remove temp file: {str(e)}"))

    def _download_from_url(self, url, filename, chunk_size):
        """Download a file from an HTTP URL to a temporary file."""
        try:
            # Create a temporary file that will be automatically deleted
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.tar.gz')
            
            # Stream the download in chunks to handle large files
            with requests.get(url, stream=True) as response:
                response.raise_for_status()
                file_size = int(response.headers.get('content-length', 0))
                
                if file_size > 0:
                    self.stdout.write(f"File size: {self._format_size(file_size)}")
                
                downloaded = 0
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        temp_file.write(chunk)
                        downloaded += len(chunk)
                        if file_size > 0:
                            percent = int((downloaded / file_size) * 100)
                            self.stdout.write(f"\rDownloading: {percent}% ({self._format_size(downloaded)}/{self._format_size(file_size)})", ending='')
                            sys.stdout.flush()
                
                if file_size > 0:
                    self.stdout.write("")  # New line after progress
                
                self.stdout.write(self.style.SUCCESS(f"Download complete: {self._format_size(downloaded)}"))
                
            temp_file.close()  # Close the file so it can be opened by other processes
            return temp_file
            
        except requests.RequestException as e:
            self.stdout.write(self.style.ERROR(f"Error downloading from URL: {str(e)}"))
            if 'temp_file' in locals() and temp_file and os.path.exists(temp_file.name):
                temp_file.close()
                os.unlink(temp_file.name)
            return None

    def _download_from_s3(self, s3_uri, filename, chunk_size):
        """Download a file from an S3 URI to a temporary file."""
        try:
            # Parse the S3 URI to get bucket and key
            parsed_url = urlparse(s3_uri)
            bucket_name = parsed_url.netloc
            key = parsed_url.path.lstrip('/')
            
            # Create a temporary file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.tar.gz')
            
            # Initialize S3 client
            s3_client = boto3.client('s3')
            
            # Get object details
            response = s3_client.head_object(Bucket=bucket_name, Key=key)
            file_size = response.get('ContentLength', 0)
            
            if file_size > 0:
                self.stdout.write(f"File size: {self._format_size(file_size)}")
            
            # Download the file with progress tracking
            self.stdout.write(f"Downloading from S3: s3://{bucket_name}/{key}")
            
            downloaded = 0
            
            # Define a callback function to track download progress
            def progress_callback(bytes_transferred):
                nonlocal downloaded
                downloaded += bytes_transferred
                if file_size > 0:
                    percent = int((downloaded / file_size) * 100)
                    self.stdout.write(f"\rDownloading: {percent}% ({self._format_size(downloaded)}/{self._format_size(file_size)})", ending='')
                    sys.stdout.flush()
            
            # Download the file from S3 with progress tracking
            s3_client.download_file(
                Bucket=bucket_name,
                Key=key,
                Filename=temp_file.name,
                Callback=progress_callback
            )
            
            if file_size > 0:
                self.stdout.write("")  # New line after progress
                
            self.stdout.write(self.style.SUCCESS(f"Download complete: {self._format_size(downloaded)}"))
            
            temp_file.close()  # Close the file so it can be opened by other processes
            return temp_file
            
        except ClientError as e:
            self.stdout.write(self.style.ERROR(f"Error downloading from S3: {str(e)}"))
            if 'temp_file' in locals() and temp_file and os.path.exists(temp_file.name):
                temp_file.close()
                os.unlink(temp_file.name)
            return None
    
    def _format_size(self, size_bytes):
        """Format bytes to a human-readable size."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024 or unit == 'TB':
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
