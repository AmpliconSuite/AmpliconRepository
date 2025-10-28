#!/usr/bin/env python
"""
Migration script to add onProjectUpdate=True to all existing user preferences.
This ensures existing users have the new notification preference enabled by default.

Usage: python migrate_user_prefs_onProjectUpdate.py
"""

import os
import sys
import django

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
django.setup()

from caper.utils import get_collection_handle, db_handle

def migrate_user_preferences():
    """Add onProjectUpdate=True to all existing user preferences that don't have it."""
    
    user_preferences_handle = get_collection_handle(db_handle, 'user_preferences')
    
    # Find all user preferences that don't have onProjectUpdate field
    users_without_field = user_preferences_handle.find({
        'onProjectUpdate': {'$exists': False}
    })
    
    count = 0
    for user_pref in users_without_field:
        count += 1
        email = user_pref.get('email', 'unknown')
        print(f"Updating preferences for user: {email}")
    
    # Update all user preferences to add onProjectUpdate=True if it doesn't exist
    result = user_preferences_handle.update_many(
        {'onProjectUpdate': {'$exists': False}},
        {'$set': {'onProjectUpdate': True}}
    )
    
    print(f"\nMigration complete!")
    print(f"Total user preferences checked: {count}")
    print(f"Total user preferences updated: {result.modified_count}")
    print(f"Users with onProjectUpdate now set to True: {result.modified_count}")
    
    # Verify the update
    total_users = user_preferences_handle.count_documents({})
    users_with_field = user_preferences_handle.count_documents({'onProjectUpdate': {'$exists': True}})
    
    print(f"\nVerification:")
    print(f"Total user preferences in database: {total_users}")
    print(f"User preferences with onProjectUpdate field: {users_with_field}")

if __name__ == '__main__':
    print("Starting migration to add onProjectUpdate=True to existing user preferences...")
    print("-" * 70)
    migrate_user_preferences()
    print("-" * 70)
    print("Migration script completed successfully!")

