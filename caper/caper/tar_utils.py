"""
Utility functions for working with GridFS-stored tar files.

These functions allow efficient extraction of specific files or directories
from tar files stored in MongoDB GridFS without requiring the entire tar
to be written to disk first.
"""

import os
import tarfile
import tempfile
import logging
from bson import ObjectId
from .utils import fs_handle, collection_handle

logger = logging.getLogger(__name__)


def extract_from_project_tarfile(project_id, tar_path_filter, output_dir=None, use_temp=False):
    """
    Efficiently extract specific files/directories from a project's GridFS-stored tarfile.
    
    This function can extract subdirectories like 'other_files/' or 'extracted_from_zips/'
    without downloading the entire tar file first.
    
    Args:
        project_id (str or ObjectId): The MongoDB ObjectID of the project
        tar_path_filter (str): Path or prefix to extract from the tar 
                               Examples: 'results/other_files/', 
                                        'results/AA_outputs/extracted_from_zips/'
        output_dir (str, optional): Directory to extract to. Defaults to tmp/{project_id}/
        use_temp (bool): If True, writes tar to temp file first (more reliable but uses disk)
                        If False, streams directly from GridFS (more efficient)
    
    Returns:
        tuple: (output_dir, extracted_count) - Path where files were extracted and count
    
    Raises:
        ValueError: If project not found or has no tarfile
        RuntimeError: If extraction fails
    
    Example:
        >>> # Extract other_files directory
        >>> output_dir, count = extract_from_project_tarfile(
        ...     '507f1f77bcf86cd799439011',
        ...     'results/other_files/',
        ...     './extracted/'
        ... )
        >>> print(f"Extracted {count} files to {output_dir}")
    """
    
    # Get project document
    try:
        if isinstance(project_id, str):
            project_id = ObjectId(project_id)
        project = collection_handle.find_one({'_id': project_id})
        if not project:
            raise ValueError(f"Project {project_id} not found")
    except Exception as e:
        raise ValueError(f"Invalid project_id: {e}")
    
    # Get tarfile ID
    if 'tarfile' not in project:
        raise ValueError(f"Project {project_id} has no tarfile stored")
    
    tar_id = project['tarfile']
    logger.info(f"Found tarfile ID: {tar_id} for project {project_id}")
    
    # Determine output directory
    if output_dir is None:
        output_dir = f"tmp/{project_id}"
    
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Will extract to: {output_dir}")
    
    # Get tarfile from GridFS
    try:
        tar_gridfs_file = fs_handle.get(ObjectId(tar_id))
        logger.info(f"Retrieved tarfile from GridFS (size: {tar_gridfs_file.length:,} bytes)")
    except Exception as e:
        raise RuntimeError(f"Failed to retrieve tarfile from GridFS: {e}")
    
    if use_temp:
        # Method 1: Write to temp file first (safer but uses more disk)
        extracted_count = _extract_via_tempfile(tar_gridfs_file, tar_path_filter, output_dir)
    else:
        # Method 2: Stream directly from GridFS (more memory efficient)
        try:
            tar_gridfs_file.seek(0)
            extracted_count = _extract_via_stream(tar_gridfs_file, tar_path_filter, output_dir)
        except Exception as e:
            logger.error(f"Direct streaming failed: {e}")
            logger.info("Falling back to temp file method...")
            tar_gridfs_file.seek(0)
            extracted_count = _extract_via_tempfile(tar_gridfs_file, tar_path_filter, output_dir)
    
    logger.info(f"Successfully extracted {extracted_count} files to {output_dir}")
    return output_dir, extracted_count


def _extract_via_tempfile(gridfs_file, path_filter, output_dir):
    """Extract via temporary file on disk."""
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as temp_tar:
        temp_tar_path = temp_tar.name
        logger.info(f"Writing tar to temporary file: {temp_tar_path}")
        
        # Write in chunks to avoid memory issues
        chunk_size = 8192
        bytes_written = 0
        while True:
            chunk = gridfs_file.read(chunk_size)
            if not chunk:
                break
            temp_tar.write(chunk)
            bytes_written += len(chunk)
            if bytes_written % (10 * 1024 * 1024) == 0:  # Log every 10MB
                logger.info(f"Written {bytes_written:,} bytes...")
        
        logger.info(f"Finished writing {bytes_written:,} bytes")
    
    try:
        # Open and extract from the temp file
        with tarfile.open(temp_tar_path, 'r:gz') as tar:
            extracted_count = _extract_matching_members(tar, path_filter, output_dir)
        return extracted_count
    finally:
        # Clean up temp file
        os.unlink(temp_tar_path)
        logger.info(f"Cleaned up temporary file: {temp_tar_path}")


def _extract_via_stream(gridfs_file, path_filter, output_dir):
    """Extract by streaming directly from GridFS."""
    logger.info("Streaming tar directly from GridFS...")
    with tarfile.open(fileobj=gridfs_file, mode='r:gz') as tar:
        return _extract_matching_members(tar, path_filter, output_dir)


def _extract_matching_members(tar, path_filter, output_dir):
    """
    Extract only members that match the path filter.
    
    Args:
        tar: tarfile.TarFile object
        path_filter: Path prefix to filter by
        output_dir: Directory to extract to
    
    Returns:
        int: Number of files extracted
    """
    # Normalize path filter
    path_filter = path_filter.rstrip('/')
    
    # Get all matching members
    all_members = tar.getmembers()
    matching_members = [m for m in all_members if m.name.startswith(path_filter)]
    
    logger.info(f"Found {len(matching_members)} members matching '{path_filter}' out of {len(all_members)} total")
    
    if not matching_members:
        logger.warning(f"No members found matching '{path_filter}'")
        logger.info(f"First 20 members in tar: {[m.name for m in all_members[:20]]}")
        return 0
    
    # Show what will be extracted
    logger.info(f"First 10 matching members: {[m.name for m in matching_members[:10]]}")
    
    # Extract matching members
    extracted_count = 0
    for member in matching_members:
        try:
            tar.extract(member, path=output_dir)
            extracted_count += 1
            
            if extracted_count % 100 == 0:
                logger.info(f"Extracted {extracted_count}/{len(matching_members)} files...")
        except Exception as e:
            logger.warning(f"Failed to extract {member.name}: {e}")
    
    return extracted_count


def list_project_tar_contents(project_id, path_filter=None):
    """
    List contents of a project's GridFS tarfile without extracting.
    
    Args:
        project_id (str or ObjectId): The MongoDB ObjectID of the project
        path_filter (str, optional): Only show paths matching this prefix
    
    Returns:
        list: List of file paths in the tar
    
    Raises:
        ValueError: If project not found or has no tarfile
    
    Example:
        >>> # List all files in other_files
        >>> files = list_project_tar_contents(
        ...     '507f1f77bcf86cd799439011',
        ...     'results/other_files/'
        ... )
        >>> print(f"Found {len(files)} files in other_files/")
    """
    # Get project document
    try:
        if isinstance(project_id, str):
            project_id = ObjectId(project_id)
        project = collection_handle.find_one({'_id': project_id})
        if not project:
            raise ValueError(f"Project {project_id} not found")
    except Exception as e:
        raise ValueError(f"Invalid project_id: {e}")
    
    # Get tarfile ID
    if 'tarfile' not in project:
        raise ValueError(f"Project {project_id} has no tarfile stored")
    
    tar_id = project['tarfile']
    
    # Get tarfile from GridFS
    tar_gridfs_file = fs_handle.get(ObjectId(tar_id))
    
    # List contents
    with tarfile.open(fileobj=tar_gridfs_file, mode='r:gz') as tar:
        all_names = tar.getnames()
        
        if path_filter:
            path_filter = path_filter.rstrip('/')
            filtered_names = [n for n in all_names if n.startswith(path_filter)]
            logger.info(f"Found {len(filtered_names)} files matching '{path_filter}' out of {len(all_names)} total")
            return filtered_names
        else:
            logger.info(f"Tar contains {len(all_names)} files total")
            return all_names


def check_extracted_files_exist(project_id, check_paths=None):
    """
    Check if extracted files exist on disk for a project.
    
    Args:
        project_id (str or ObjectId): The MongoDB ObjectID of the project
        check_paths (list, optional): List of relative paths to check within the project dir.
                                     Defaults to checking common directories.
    
    Returns:
        dict: Dictionary with paths as keys and boolean existence as values
    
    Example:
        >>> status = check_extracted_files_exist(
        ...     '507f1f77bcf86cd799439011',
        ...     ['results/other_files/', 'results/AA_outputs/extracted_from_zips/']
        ... )
        >>> if not status['results/other_files/']:
        ...     print("Need to extract other_files/")
    """
    if isinstance(project_id, str):
        project_id = ObjectId(project_id)
    
    project_dir = f"tmp/{project_id}"
    
    if check_paths is None:
        # Default paths to check
        check_paths = [
            'results/other_files/',
            'results/AA_outputs/',
            'results/AA_outputs/extracted_from_zips/',
            'results/run.json',
            'results/finished_project_creation.txt'
        ]
    
    status = {}
    for path in check_paths:
        full_path = os.path.join(project_dir, path)
        status[path] = os.path.exists(full_path)
        
        if os.path.isdir(full_path):
            # For directories, also check if they're not empty
            try:
                contents = os.listdir(full_path)
                status[f"{path}_file_count"] = len(contents)
            except:
                status[f"{path}_file_count"] = 0
    
    return status


def ensure_files_on_disk(project_id, paths_needed, force_reextract=False):
    """
    Ensure specific paths from the project tar are available on disk.
    
    This is a convenience function that checks if files exist and extracts
    them from GridFS if they don't.
    
    Args:
        project_id (str or ObjectId): The MongoDB ObjectID of the project
        paths_needed (list): List of paths that must be present (e.g., ['results/other_files/'])
        force_reextract (bool): If True, extract even if files already exist
    
    Returns:
        dict: Status dictionary with 'extracted' (bool) and 'output_dir' (str) keys
    
    Example:
        >>> # Ensure other_files is available
        >>> result = ensure_files_on_disk(
        ...     '507f1f77bcf86cd799439011',
        ...     ['results/other_files/']
        ... )
        >>> if result['extracted']:
        ...     print(f"Files extracted to {result['output_dir']}")
        >>> else:
        ...     print("Files already present on disk")
    """
    if isinstance(project_id, str):
        project_id = ObjectId(project_id)
    
    project_dir = f"tmp/{project_id}"
    
    if not force_reextract:
        # Check if any requested paths are missing
        missing_paths = []
        for path in paths_needed:
            full_path = os.path.join(project_dir, path)
            if not os.path.exists(full_path):
                missing_paths.append(path)
        
        if not missing_paths:
            logger.info(f"All requested paths already exist on disk for project {project_id}")
            return {
                'extracted': False,
                'output_dir': project_dir,
                'message': 'Files already present'
            }
    
    # Need to extract - extract all requested paths
    total_extracted = 0
    for path in paths_needed:
        logger.info(f"Extracting {path} for project {project_id}")
        output_dir, count = extract_from_project_tarfile(project_id, path, project_dir)
        total_extracted += count
    
    return {
        'extracted': True,
        'output_dir': project_dir,
        'file_count': total_extracted,
        'message': f'Extracted {total_extracted} files'
    }

