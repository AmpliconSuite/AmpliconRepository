#!/usr/bin/env python
"""
Script to check current and delete flags for all projects in the database.
Shows project name, ID, and flag values (including when flags are missing).

Usage (inside Django app directory):
    cd /path/to/caper/caper
    python check_project_flags.py
    
Or use the Django shell version (recommended for Docker):
    python manage.py shell < check_project_flags_django.py
"""

import os
import sys

# Set up Django environment - this is required for Docker containers
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')

try:
    import django
    django.setup()
    from caper.utils import collection_handle
    print("✓ Using Django database connection")
    using_django = True
except ImportError as e:
    print(f"❌ ERROR: Could not import Django: {e}")
    print("\nThis script must be run from within the Django app directory.")
    print("Try running from: /path/to/caper/caper/")
    print("\nOr use the Django shell version instead:")
    print("  python manage.py shell < check_project_flags_django.py")
    sys.exit(1)
except Exception as e:
    print(f"❌ ERROR: Could not set up Django environment: {e}")
    print("\nMake sure you're in the correct directory and Django is properly configured.")
    print("\nAlternatively, use the Django shell version:")
    print("  python manage.py shell < check_project_flags_django.py")
    sys.exit(1)

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
    
    # Check for orphaned old versions (delete=False, current=False, but no other versions)
    print("\n" + "="*100)
    print("ORPHANED OLD VERSIONS CHECK")
    print("="*100 + "\n")
    print("Looking for projects with delete=False and current=False that have no other versions...")
    print()
    
    # Find all projects with delete=False and current=False
    old_versions = []
    for project in all_projects:
        delete_val = project.get('delete', None)
        current_val = project.get('current', None)
        
        # Must have delete=False and current=False
        if delete_val == False and current_val == False:
            old_versions.append(project)
    
    if not old_versions:
        print("✓ No projects found with delete=False and current=False")
    else:
        print(f"Found {len(old_versions)} project(s) with delete=False and current=False")
        print("Checking if they have other versions using previous_versions field...\n")
        
        orphaned_count = 0
        orphaned_projects = []
        
        for project in old_versions:
            project_name = project.get('project_name', 'UNNAMED')
            project_id = str(project.get('_id', 'NO_ID'))
            
            # Check if this project is truly orphaned:
            # 1. It should not have any entries in its own previous_versions array
            has_previous = len(project.get('previous_versions', [])) > 0
            
            # 2. It should not be referenced in any other project's previous_versions
            is_referenced = collection_handle.count_documents({
                'previous_versions.linkid': project_id
            }) > 0
            
            if not has_previous and not is_referenced:
                # No other versions found - this is orphaned!
                orphaned_count += 1
                orphaned_projects.append({
                    'project': project,
                    'name': project_name,
                    'id': project_id
                })
        
        if orphaned_count == 0:
            print("✓ All old versions (current=False) have corresponding current versions")
        else:
            print(f"⚠️  FOUND {orphaned_count} ORPHANED OLD VERSION(S) ⚠️")
            print(f"   These have delete=False and current=False but NO other versions exist")
            print()
            print("-" * 100)
            print(f"{'Project Name':<50} {'ID':<26} {'Private':<10}")
            print("-" * 100)
            
            for item in orphaned_projects:
                project = item['project']
                project_name = item['name'][:48]
                project_id = item['id']
                private = project.get('private', 'NOT SET')
                
                print(f"{project_name:<50} {project_id:<26} {str(private):<10}")
            
            print()
            print("⚠️  RECOMMENDATION ⚠️")
            print("These projects appear to be orphaned old versions with no current version.")
            print("They should probably either:")
            print("  1. Have 'current' set to True (if they should be the active version), OR")
            print("  2. Have 'delete' set to True (if they should be hidden)")
            print()
            print("To fix by setting current=True:")
            print("  from caper.utils import collection_handle")
            print("  from bson.objectid import ObjectId")
            print("  # For each orphaned project ID:")
            print("  collection_handle.update_one(")
            print("      {'_id': ObjectId('PROJECT_ID_HERE')},")
            print("      {'$set': {'current': True}}")
            print("  )")
    
    print("\n" + "="*100)
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

