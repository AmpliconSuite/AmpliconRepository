"""
GridFS Caching Utilities

This module provides caching for GridFS file reads to improve performance
when the same files are accessed multiple times within a short time window.
"""

import logging
from bson import ObjectId
from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_gridfs_file_cached(fs_handle, file_id, cache_timeout=600):
    """
    Read a file from GridFS with caching.
    Falls back to direct GridFS read if cache is unavailable.
    
    Args:
        fs_handle: GridFS handle
        file_id: ObjectId or string ID of the file
        cache_timeout: Cache timeout in seconds (default: 10 minutes)
    
    Returns:
        bytes: File contents
    """
    # Convert to string for cache key
    file_id_str = str(file_id)
    cache_key = f"gridfs_file_{file_id_str}"
    
    # Try to get from cache
    try:
        cached_file = cache.get(cache_key)
        if cached_file is not None:
            logger.debug(f"GridFS cache HIT for {file_id_str}")
            return cached_file
    except Exception as e:
        logger.debug(f"Cache unavailable, falling back to direct read: {e}")
    
    # Cache miss or cache unavailable - read from GridFS
    logger.debug(f"GridFS cache MISS for {file_id_str}")
    try:
        file_contents = fs_handle.get(ObjectId(file_id)).read()
        
        # Try to cache the file contents
        try:
            cache.set(cache_key, file_contents, cache_timeout)
            logger.debug(f"Cached GridFS file {file_id_str} ({len(file_contents)} bytes)")
        except Exception as e:
            logger.debug(f"Could not cache file {file_id_str}: {e}")
        
        return file_contents
        
    except Exception as e:
        logger.error(f"Error reading GridFS file {file_id_str}: {e}")
        raise


def invalidate_gridfs_cache(file_id):
    """
    Invalidate cached GridFS file.
    
    Args:
        file_id: ObjectId or string ID of the file to invalidate
    """
    file_id_str = str(file_id)
    cache_key = f"gridfs_file_{file_id_str}"
    cache.delete(cache_key)
    logger.debug(f"Invalidated GridFS cache for {file_id_str}")


def get_multiple_gridfs_files_cached(fs_handle, file_ids, cache_timeout=600):
    """
    Read multiple files from GridFS with caching (batch operation).
    
    Args:
        fs_handle: GridFS handle
        file_ids: List of ObjectId or string IDs
        cache_timeout: Cache timeout in seconds (default: 10 minutes)
    
    Returns:
        dict: Mapping of file_id -> file_contents
    """
    results = {}
    
    for file_id in file_ids:
        results[str(file_id)] = get_gridfs_file_cached(
            fs_handle, 
            file_id, 
            cache_timeout
        )
    
    return results

