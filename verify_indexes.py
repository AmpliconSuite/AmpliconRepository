#!/usr/bin/env python3
"""
Verify that MongoDB indexes exist for the projects collection.

This script checks if the indexes created by the application startup
are properly in place and provides information about their configuration.

Usage:
    python verify_indexes.py
"""

import sys
import os

# Add the caper directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'caper'))

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
import django
django.setup()

from caper.utils import collection_handle

def verify_indexes():
    """Check which indexes exist on the projects collection"""
    
    print("=" * 70)
    print("MongoDB/DocumentDB Index Verification")
    print("=" * 70)
    print()
    
    # Expected indexes
    expected_indexes = {
        'idx_index_public_projects': ['delete', 'current', 'private', 'featured'],
        'idx_index_private_projects': ['delete', 'current', 'private', 'project_members'],
        'idx_project_id_delete': ['_id', 'delete']
    }
    
    # Get all indexes
    indexes = list(collection_handle.list_indexes())
    
    print(f"Total indexes found: {len(indexes)}")
    print()
    
    # Track which expected indexes we found
    found_indexes = set()
    
    # Display all indexes
    for idx, index_info in enumerate(indexes, 1):
        index_name = index_info.get('name', 'unknown')
        index_keys = index_info.get('key', {})
        
        print(f"{idx}. Index Name: {index_name}")
        print(f"   Fields:")
        for field, direction in index_keys.items():
            direction_str = "ascending (1)" if direction == 1 else "descending (-1)"
            print(f"     - {field}: {direction_str}")
        
        # Check if this is one of our expected indexes
        if index_name in expected_indexes:
            found_indexes.add(index_name)
            expected_fields = expected_indexes[index_name]
            actual_fields = list(index_keys.keys())
            
            if expected_fields == actual_fields:
                print(f"   Status: ✅ CORRECT - Matches expected configuration")
            else:
                print(f"   Status: ⚠️  WARNING - Fields don't match expected")
                print(f"   Expected: {expected_fields}")
                print(f"   Actual: {actual_fields}")
        elif index_name == '_id_':
            print(f"   Status: ℹ️  Default MongoDB index (always present)")
        else:
            print(f"   Status: ℹ️  Other index")
        
        # Check for background option
        background = index_info.get('background', False)
        if background:
            print(f"   Background: Yes (created without blocking)")
        
        print()
    
    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    
    missing_indexes = set(expected_indexes.keys()) - found_indexes
    
    if not missing_indexes:
        print("✅ All expected indexes are present!")
        print()
        print("Your database is properly optimized for index page queries.")
    else:
        print("⚠️  Some expected indexes are missing:")
        for idx_name in missing_indexes:
            print(f"   - {idx_name}")
        print()
        print("The indexes should be automatically created on application startup.")
        print("Check application logs for any errors during index creation.")
        print()
        print("You can also create them manually by running:")
        print("   python create_index_page_indexes.py")
    
    print()
    
    # Check database type (MongoDB vs DocumentDB)
    try:
        from caper.utils import mongo_client
        server_info = mongo_client.server_info()
        version = server_info.get('version', 'unknown')
        print(f"Database Version: {version}")
        
        # DocumentDB typically has version like "4.0.0" or similar
        # and may include "documentdb" in the build info
        build_info = mongo_client.admin.command('buildInfo')
        modules = build_info.get('modules', [])
        
        if 'enterprise' in modules:
            print("Database Type: MongoDB Enterprise")
        elif version.startswith('4.0') or version.startswith('5.0'):
            print("Database Type: MongoDB or DocumentDB (compatible)")
        else:
            print("Database Type: MongoDB")
            
    except Exception as e:
        print(f"Could not determine database type: {e}")
    
    print()
    print("=" * 70)
    
    return len(missing_indexes) == 0

if __name__ == '__main__':
    try:
        success = verify_indexes()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)

