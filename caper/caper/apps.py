import os
import subprocess
import threading
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CaperConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'caper'

    def ready(self):
        """
        This method is called when Django starts up.
        It syncs static files to S3 in a background thread and ensures database indexes exist.
        """
        # Only run this once (Django loads apps multiple times in certain scenarios)
        if os.environ.get('RUN_MAIN') != 'true':
            return
        
        # Ensure MongoDB indexes exist for optimal query performance
        try:
            self.ensure_indexes()
            logger.info("Successfully ensured MongoDB indexes exist")
        except Exception as e:
            logger.warning(f"Failed to create MongoDB indexes: {str(e)}")
        
        # Start the S3 sync in a background thread
        sync_thread = threading.Thread(target=self.sync_static_to_s3, daemon=True)
        sync_thread.start()
        logger.info("Started background thread for syncing static files to S3")

    def ensure_indexes(self):
        """
        Ensure MongoDB indexes exist for optimal query performance.
        Creates indexes if they don't exist, or does nothing if they already exist.
        This is safe to call on every startup.
        
        Note: These indexes are compatible with both MongoDB and Amazon DocumentDB.
        DocumentDB supports compound indexes with the same syntax as MongoDB.
        """
        try:
            from .utils import collection_handle
            
            # Index 1: For public projects query on index page
            # Query pattern: {'delete': False, 'current': True, 'private': False}
            # This speeds up the main index page query for public projects
            try:
                collection_handle.create_index(
                    [
                        ('delete', 1),
                        ('current', 1),
                        ('private', 1),
                        ('featured', 1)
                    ],
                    name='idx_index_public_projects',
                    background=True  # Don't block other operations
                )
                logger.info("✓ Index 'idx_index_public_projects' ensured")
            except Exception as e:
                logger.warning(f"Could not create index 'idx_index_public_projects': {str(e)}")
            
            # Index 2: For private projects query on index page
            # Query pattern: {'delete': False, 'current': True, 'private': True, 'project_members': <user>}
            # This speeds up queries for authenticated users' private projects
            try:
                collection_handle.create_index(
                    [
                        ('delete', 1),
                        ('current', 1),
                        ('private', 1),
                        ('project_members', 1)
                    ],
                    name='idx_index_private_projects',
                    background=True
                )
                logger.info("✓ Index 'idx_index_private_projects' ensured")
            except Exception as e:
                logger.warning(f"Could not create index 'idx_index_private_projects': {str(e)}")
            
            # Index 3: For project lookups by _id and delete status
            # This is used throughout the application
            try:
                collection_handle.create_index(
                    [
                        ('_id', 1),
                        ('delete', 1)
                    ],
                    name='idx_project_id_delete',
                    background=True
                )
                logger.info("✓ Index 'idx_project_id_delete' ensured")
            except Exception as e:
                logger.warning(f"Could not create index 'idx_project_id_delete': {str(e)}")
                
        except Exception as e:
            # Log but don't crash the application if index creation fails
            logger.error(f"Error in ensure_indexes: {str(e)}", exc_info=True)
            raise


    def sync_static_to_s3(self):
        """
        Sync static files to S3 bucket using AWS CLI.
        This runs in a background thread and won't block server startup.
        """
        try:
            # Get environment variables
            amplicon_env = os.environ.get('AMPLICON_ENV', 'dev')
            aws_profile = os.environ.get('AWS_PROFILE_NAME', 'amprepo')
            
            # Determine the local static directory
            # Get the project root (parent of caper app directory)
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local_static_dir = os.path.join(current_dir, 'static/')
            
            # Define S3 bucket path
            s3_bucket = f"s3://amprepobucket/{amplicon_env}/static/"
            
            logger.info(f"Starting S3 sync from {local_static_dir} to {s3_bucket}")
            
            # Build the AWS CLI command
            aws_cmd = [
                'aws', 's3', 'sync',
                local_static_dir,
                s3_bucket,
                '--profile', aws_profile
            ]
            
            # Execute the sync command
            result = subprocess.run(
                aws_cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully synced static files to S3: {s3_bucket}")
                if result.stdout:
                    logger.debug(f"AWS S3 sync output: {result.stdout}")
            else:
                logger.error(f"Failed to sync static files to S3. Return code: {result.returncode}")
                if result.stderr:
                    logger.error(f"Error output: {result.stderr}")
                    
        except subprocess.TimeoutExpired:
            logger.error("S3 sync timed out after 5 minutes")
        except FileNotFoundError:
            logger.error("AWS CLI not found. Please ensure AWS CLI is installed and in PATH")
        except Exception as e:
            logger.error(f"Unexpected error during S3 sync: {str(e)}", exc_info=True)

