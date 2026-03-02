#!/usr/bin/env python3
"""
Data migration script to convert existing MongoDB projects from boolean 'private' field
to new string-based visibility field.

This script handles:
1. Boolean True -> 'private'
2. Boolean False -> 'public'
3. Already migrated projects remain unchanged

Run this script after deploying the visibility update changes.
Usage: 
    cd caper && python manage.py shell < ../migrate_project_visibility.py
    OR
    python ../migrate_project_visibility.py (if Django settings configured)
"""

import os
import sys
import logging

# Setup Django if running standalone
if __name__ == '__main__':
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'caper'))
    django.setup()

from caper.utils import collection_handle, normalize_visibility_field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_project_visibility():
    """
    Migrate all projects in MongoDB from boolean private field to string visibility field.
    """
    logger.info("Starting project visibility migration...")
    logger.info("=" * 80)
    
    try:
        # Count total projects
        total_projects = collection_handle.count_documents({'delete': False})
        logger.info(f"Total projects to check: {total_projects}")
        
        # Counters for tracking migration
        private_count = 0
        public_count = 0
        hidden_public_count = 0
        already_migrated_count = 0
        error_count = 0
        
        # Process all projects (not just non-deleted for comprehensive migration)
        all_projects = collection_handle.count_documents({})
        logger.info(f"Total projects (including deleted): {all_projects}")
        
        projects = collection_handle.find({})
        
        for idx, project in enumerate(projects, 1):
            try:
                project_id = project['_id']
                project_name = project.get('project_name', 'Unknown')
                current_private = project.get('private')
                
                # Check if already migrated (string value)
                if isinstance(current_private, str):
                    already_migrated_count += 1
                    if current_private == 'private':
                        private_count += 1
                    elif current_private == 'public':
                        public_count += 1
                    elif current_private == 'hidden_public':
                        hidden_public_count += 1
                    
                    if idx % 100 == 0:
                        logger.info(
                            f"Progress: {idx}/{all_projects} - "
                            f"Already migrated: {already_migrated_count}"
                        )
                    continue
                
                # Convert boolean to string
                new_visibility = normalize_visibility_field(current_private)
                
                # Update the project
                result = collection_handle.update_one(
                    {'_id': project_id},
                    {'$set': {'private': new_visibility}}
                )
                
                if result.modified_count > 0:
                    # Track statistics
                    if new_visibility == 'private':
                        private_count += 1
                    elif new_visibility == 'public':
                        public_count += 1
                    elif new_visibility == 'hidden_public':
                        hidden_public_count += 1
                    
                    if idx % 100 == 0:
                        logger.info(
                            f"Progress: {idx}/{all_projects} - "
                            f"Private: {private_count}, Public: {public_count}, "
                            f"Hidden Public: {hidden_public_count}"
                        )
                
            except Exception as e:
                error_count += 1
                logger.error(
                    f"Error migrating project {project.get('_id', 'unknown')} "
                    f"({project_name}): {str(e)}"
                )
        
        # Final report
        logger.info("")
        logger.info("=" * 80)
        logger.info("MIGRATION COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Total projects processed: {all_projects}")
        logger.info(f"Migrated to 'private': {private_count}")
        logger.info(f"Migrated to 'public': {public_count}")
        logger.info(f"Already had string values: {already_migrated_count}")
        logger.info(f"Errors encountered: {error_count}")
        logger.info("=" * 80)
        
        if error_count == 0:
            logger.info("✓ Migration completed successfully!")
        else:
            logger.warning(
                f"⚠ Migration completed with {error_count} errors. "
                f"Please review the logs above."
            )
        
        return True
        
    except Exception as e:
        logger.error(f"Fatal error during migration: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False


if __name__ == '__main__':
    success = migrate_project_visibility()
    exit(0 if success else 1)
else:
    # Running in Django shell
    print("Running MongoDB project visibility migration...")
    print("=" * 80)
    migrate_project_visibility()

