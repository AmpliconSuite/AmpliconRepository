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
        It syncs static files to S3 in a background thread.
        """
        # Only run this once (Django loads apps multiple times in certain scenarios)
        if os.environ.get('RUN_MAIN') != 'true':
            return
        
        # Start the S3 sync in a background thread
        sync_thread = threading.Thread(target=self.sync_static_to_s3, daemon=True)
        sync_thread.start()
        logger.info("Started background thread for syncing static files to S3")

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

