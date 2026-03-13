import csv
import time
import os

import pandas as pd

from .utils import *


def _build_metadata_lookup_from_dataframe(df):
    """
    Helper function to build a metadata lookup dictionary from a DataFrame.

    Args:
        df (pd.DataFrame): DataFrame containing metadata with 'sample_name' column

    Returns:
        dict: Dictionary mapping sample_name -> metadata row dict
    """
    records = df.to_dict(orient='records')
    metadata_lookup = {}
    for row in records:
        # look for sample name, case insensitive
        sample_name = {k.lower(): v for k, v in row.items()}.get('sample_name')
        if sample_name:
            metadata_lookup[sample_name] = row
        sample_name_alias = {k.lower(): v for k, v in row.items()}.get('sample_name_alias')
        if sample_name_alias:
            metadata_lookup[sample_name_alias] = row
    return metadata_lookup


def _read_metadata_file(metadata_file=None, file_path=None):
    """
    Helper function to read metadata from either an uploaded file or file path.

    Args:
        metadata_file (UploadedFile, optional): Uploaded file object
        file_path (str, optional): Path to metadata file on disk

    Returns:
        pd.DataFrame: DataFrame containing the metadata

    Raises:
        ValueError: If file type is unsupported or neither argument is provided
    """
    if metadata_file:
        file_name = metadata_file.name
        if file_name.endswith('.xlsx'):
            return pd.read_excel(metadata_file.open())
        elif file_name.endswith('.csv') or file_name.endswith('.tsv'):
            delimiter = '\t' if file_name.endswith('.tsv') else ','
            return pd.read_csv(metadata_file, delimiter=delimiter)
        else:
            raise ValueError("Unsupported file type. Please upload a .csv, .tsv, or .xlsx file.")
    elif file_path:
        if file_path.endswith('.xlsx'):
            return pd.read_excel(file_path)
        elif file_path.endswith('.csv') or file_path.endswith('.tsv'):
            delimiter = '\t' if file_path.endswith('.tsv') else ','
            return pd.read_csv(file_path, delimiter=delimiter)
        else:
            raise ValueError("Unsupported file type. Please provide a .csv, .tsv, or .xlsx file.")
    else:
        raise ValueError("Invalid file source. Provide either 'metadata_file' or 'file_path'.")


def _apply_metadata_to_runs(project_runs, metadata_lookup, old_extra_metadata=None):
    """
    Helper function to apply metadata to project runs.

    Args:
        project_runs (dict): The 'runs' field of the project
        metadata_lookup (dict): Dictionary mapping sample_name -> metadata row
        old_extra_metadata (dict, optional): Existing metadata to preserve

    Returns:
        int: Number of samples updated
    """
    samples_updated = 0

    for sample_key, sample_list in project_runs.items():
        for sample in sample_list:
            sample_name = sample.get('Sample_name')
            if not sample_name:
                continue

            # O(1) lookup instead of O(n) nested iteration
            row = metadata_lookup.get(sample_name)
            if not row:
                # If no metadata found for this sample, keep existing metadata if it exists in sample['Sample_metadata_JSON']
                
                continue

            samples_updated += 1

            if "extra_metadata_from_csv" not in sample:
                sample["extra_metadata_from_csv"] = {}

            # If there is old metadata for this sample, preserve it
            if old_extra_metadata and sample_name in old_extra_metadata:
                sample["extra_metadata_from_csv"].update(old_extra_metadata[sample_name])

            # Update with new metadata
            for key, value in row.items():
                if key != 'sample_name':
                    sample["extra_metadata_from_csv"][key] = value
                if key.lower() == 'sample_name':
                    sample["Sample_name"] = value
                # make sure to do alias after sample_name so the alias is used 
                if key.lower() == 'sample_name_alias':
                    sample["Sample_name"] = value
                if key.lower() == 'cancer_type':
                    sample["Cancer_type"] = value
                if key.lower() == 'sample_type':
                    sample["Sample_type"] = value
                if key.lower() == 'tissue_of_origin':
                    sample["Tissue_of_origin"] = value

    return samples_updated


def process_metadata(request, project_id):
    """
    Process metadata from a request and update the project in the database.

    Args:
        request: HTTP request containing the metadata file
        project_id: ID of the project to update

    Returns:
        str: Status message
    """
    if request.method != 'POST':
        return "Invalid request method"

    uploaded_file = request.FILES.get('metadataFile')
    if not uploaded_file:
        return "No file uploaded"

    try:
        # Read the metadata file
        df = _read_metadata_file(metadata_file=uploaded_file)

        # Build metadata lookup
        metadata_lookup = _build_metadata_lookup_from_dataframe(df)

        # Retrieve the project
        project = collection_handle.find_one({'_id': ObjectId(project_id)})
        if not project:
            return "Project not found"

        # Access the 'runs' field of the project
        runs = project.get('runs', {})

        # Apply metadata to runs
        samples_updated = _apply_metadata_to_runs(runs, metadata_lookup)

        logging.info(f"Updated {samples_updated} samples with metadata for project {project_id}")

        # Update the project document in the database
        collection_handle.update_one(
            {'_id': ObjectId(project_id)},
            {'$set': {'runs': runs}}
        )
        return "complete"

    except Exception as e:
        logging.exception("Error processing metadata")
        return f"Error processing file: {str(e)}"


def process_metadata_no_request(project_runs, metadata_file=None, old_extra_metadata=None, file_path=None):
    """
    Updates the 'runs' field of a project dictionary with metadata from an uploaded file or a file path.

    Args:
        project_runs (dict): The 'runs' field of the project as a dictionary.
        metadata_file (UploadedFile, optional): The metadata file uploaded by the user.
        old_extra_metadata (dict, optional): Existing extra metadata to be preserved.
        file_path (str, optional): The path to the metadata file on disk.

    Returns:
        dict: The updated 'runs' dictionary with appended metadata.

    Raises:
        ValueError: If neither `metadata_file` nor `file_path` is provided or there is an error in processing.
    """
    start_time = time.time()

    if not metadata_file and not file_path and not old_extra_metadata:
        logging.info(f"process_metadata_no_request: No metadata to process - took {time.time() - start_time:.4f}s")
        return project_runs

    if not metadata_file and not file_path and old_extra_metadata:
        # add the old extra metadata
        for sample_key, sample_list in project_runs.items():
            for sample in sample_list:
                sample_name = sample.get('Sample_name')
                if sample_name and sample_name in old_extra_metadata:
                    if "extra_metadata_from_csv" not in sample:
                        sample["extra_metadata_from_csv"] = {}
                    sample["extra_metadata_from_csv"].update(old_extra_metadata[sample_name])
        logging.info(f"process_metadata_no_request: Applied old metadata - took {time.time() - start_time:.4f}s")
        return project_runs

    try:
        # Read metadata file
        df = _read_metadata_file(metadata_file=metadata_file, file_path=file_path)

        file_read_time = time.time()
        logging.info(f"process_metadata_no_request: File read took {file_read_time - start_time:.4f}s")

        # Build metadata lookup dictionary
        metadata_lookup = _build_metadata_lookup_from_dataframe(df)

        dict_build_time = time.time()
        logging.info(
            f"process_metadata_no_request: Metadata dict built in {dict_build_time - file_read_time:.4f}s ({len(metadata_lookup)} samples)")

        # Apply metadata to runs
        samples_updated = _apply_metadata_to_runs(project_runs, metadata_lookup, old_extra_metadata)

        end_time = time.time()
        logging.info(
            f"process_metadata_no_request: Updated {samples_updated} samples in {end_time - dict_build_time:.4f}s")
        logging.info(
            f"process_metadata_no_request: Metadata processing complete - total time {end_time - start_time:.4f}s")

        return project_runs

    except Exception as e:
        logging.exception("Error processing metadata")
        raise ValueError(f"Error processing file: {str(e)}")


def get_metadata_file_from_request(request):
    """
    Gets the metadata file from request
    expecting the field in the form to be "metadataFile"
    """
    if request.method == "POST":
        try:
            metadata_file = request.FILES.get("metadataFile")
            return metadata_file
        except Exception as e:
            logging.error(f'Failed to get the metadata file from the form: {e}')
            return None
    return None


def save_metadata_file(request, project_data_path, old_project_extra_metadata=None):
    """
    Saves the 'metadataFile' from the request to the specified project data path.

    Args:
        request (HttpRequest): The incoming HTTP request containing the 'metadataFile'.
        project_data_path (str): The directory path where the file should be saved.

    Returns:
        str: The full file path to the saved metadata file.

    Raises:
        ValueError: If the file is not present or there are issues saving it.
    """
    # Get the 'metadataFile' from the request
    metadata_file = request.FILES.get("metadataFile")
    if not metadata_file:
        logging.info("No 'metadataFile' found in the request.")
        return None

    # Ensure the target directory exists
    os.makedirs(project_data_path, exist_ok=True)

    # Construct the full file path
    file_path = os.path.join(project_data_path, metadata_file.name)

    try:
        # Save the file
        with open(file_path, "wb+") as destination:
            for chunk in metadata_file.chunks():
                destination.write(chunk)

        return file_path

    except Exception as e:
        raise IOError(f"Failed to save metadata file: {str(e)}")


def get_extra_metadata_from_project(project):
    """
    Retrieves the extra metadata from the project's runs.

    Args:
        project (dict): The project dictionary.

    Returns:
        dict: A dictionary containing the extra metadata from the project's runs.
    """
    return {
        sample['Sample_name']: sample['extra_metadata_from_csv']
        for sample_list in project.get('runs', {}).values()
        for sample in sample_list
        if 'extra_metadata_from_csv' in sample
    }


def has_sample_metadata(project):
    """
    Checks if there is any sample metadata in the project's runs.

    Args:
        project_id (str): The ID of the project to check.

    Returns:
        bool: True if sample metadata exists, False otherwise.
    """
    if not project or 'runs' not in project:
        return False
    for sample_list in project['runs'].values():
        for sample in sample_list:
            if 'extra_metadata_from_csv' in sample:
                return True
    return False