#!/usr/bin/env python
"""
Script to check current and delete flags for all projects in the database.
Shows project name, ID, and flag values (including when flags are missing).

Usage:
    python check_project_flags.py
    
Or if using Django environment:
    python manage.py shell < check_project_flags.py
"""

import os
import sys
from pymongo import MongoClient

# Try to use Django settings if available
try:
    import django
    django.setup()
    from caper.utils import collection_handle
    print("Using Django database connection")
    using_django = True
except:
    print("Django not available, using direct MongoDB connection")
    using_django = False
    # Fallback to direct connection - update these values for your server
    MONGO_HOST = os.environ.get('MONGO_HOST', 'localhost')
    MONGO_PORT = int(os.environ.get('MONGO_PORT', 27017))
    MONGO_DB = os.environ.get('MONGO_DB', 'caper')
    client = MongoClient(MONGO_HOST, MONGO_PORT)
    db = client[MONGO_DB]
    collection_handle = db['projects']

def check_flag_value(project, flag_name):
    """
    Check if a flag exists and return its value with status indicator.
    """
    if flag_name not in project:
        return 'NOT SET', 'missing'
    else:
        value = project[flag_name]
        if value is True:
            return 'True', 'set'
        elif value is False:
            return 'False', 'set'
        else:
            return str(value), 'set'

def main():
    print("\n" + "="*100)
    print("PROJECT FLAGS REPORT")
    print("="*100 + "\n")
    
    # Get all projects (no filtering)
    all_projects = list(collection_handle.find({}).sort('project_name', 1))
    
    if not all_projects:
        print("No projects found in database.")
        return
    
    print(f"Total projects in database: {len(all_projects)}\n")
    
    # Counters for summary
    missing_current = 0
    missing_delete = 0
    current_true = 0
    current_false = 0
    delete_true = 0
    delete_false = 0
    
    # Header
    print(f"{'Project Name':<50} {'ID':<26} {'current':<10} {'delete':<10}")
    print("-" * 100)
    
    for project in all_projects:
        project_name = project.get('project_name', 'UNNAMED')[:48]
        project_id = str(project.get('_id', 'NO_ID'))
        
        current_value, current_status = check_flag_value(project, 'current')
        delete_value, delete_status = check_flag_value(project, 'delete')
        
        # Update counters
        if current_status == 'missing':
            missing_current += 1
        elif current_value == 'True':
            current_true += 1
        elif current_value == 'False':
            current_false += 1
            
        if delete_status == 'missing':
            missing_delete += 1
        elif delete_value == 'True':
            delete_true += 1
        elif delete_value == 'False':
            delete_false += 1
        
        # Format output with color indicators (if terminal supports it)
        current_display = current_value
        delete_display = delete_value
        
        if current_status == 'missing':
            current_display = f"⚠️  {current_value}"
        if delete_status == 'missing':
            delete_display = f"⚠️  {delete_value}"
            
        print(f"{project_name:<50} {project_id:<26} {current_display:<10} {delete_display:<10}")
    
    # Summary statistics
    print("\n" + "="*100)
    print("SUMMARY STATISTICS")
    print("="*100)
    print(f"\nTotal Projects: {len(all_projects)}")
    print(f"\n'current' flag:")
    print(f"  - True:     {current_true:4d}")
    print(f"  - False:    {current_false:4d}")
    print(f"  - NOT SET:  {missing_current:4d} ⚠️")
    print(f"\n'delete' flag:")
    print(f"  - True:     {delete_true:4d}")
    print(f"  - False:    {delete_false:4d}")
    print(f"  - NOT SET:  {missing_delete:4d} ⚠️")
    
    # Warnings
    if missing_current > 0:
        print(f"\n⚠️  WARNING: {missing_current} project(s) missing 'current' flag!")
        print("   These projects may not appear in filtered queries.")
    
    if missing_delete > 0:
        print(f"\n⚠️  WARNING: {missing_delete} project(s) missing 'delete' flag!")
        print("   These projects may behave unexpectedly in delete operations.")
    
    # Show problematic projects in detail
    if missing_current > 0 or missing_delete > 0:
        print("\n" + "="*100)
        print("PROJECTS WITH MISSING FLAGS (Detailed)")
        print("="*100 + "\n")
        
        for project in all_projects:
            current_value, current_status = check_flag_value(project, 'current')
            delete_value, delete_status = check_flag_value(project, 'delete')
            
            if current_status == 'missing' or delete_status == 'missing':
                project_name = project.get('project_name', 'UNNAMED')
                project_id = str(project.get('_id', 'NO_ID'))
                private = project.get('private', 'NOT SET')
                
                print(f"Project: {project_name}")
                print(f"  ID:      {project_id}")
                print(f"  Private: {private}")
                print(f"  current: {current_value} {'⚠️  MISSING' if current_status == 'missing' else ''}")
                print(f"  delete:  {delete_value} {'⚠️  MISSING' if delete_status == 'missing' else ''}")
                print()
    
    print("="*100)
    print("Report complete.")
    print("="*100 + "\n")

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

