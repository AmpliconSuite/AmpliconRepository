from django.http import HttpResponse, StreamingHttpResponse, HttpResponseRedirect,JsonResponse,Http404
from .utils import *
import pandas as pd
import csv
from django.shortcuts import render, redirect



def process_metadata(request, project_id):
    if request.method == 'POST':
        print(f"Project ID from URL: {project_id}")
        uploaded_file = request.FILES.get('metadataFile')
        if not uploaded_file:
            return "No file uploaded"

        try:
            # Determine the file type and read the file into a Pandas DataFrame
            file_name = uploaded_file.name
            if file_name.endswith('.xlsx'):
                df = pd.read_excel(uploaded_file.open())  # Read Excel file
            elif file_name.endswith('.csv') or file_name.endswith('.tsv'):
                delimiter = '\t' if file_name.endswith('.tsv') else ','
                df = pd.read_csv(uploaded_file, delimiter=delimiter)  # Read CSV/TSV file
            else:
                return "Unsupported file type. Please upload a .csv, .tsv, or .xlsx file."

            # Convert DataFrame to a list of dictionaries
            records = df.to_dict(orient='records')

            # Retrieve the project using project_id
            project = collection_handle.find_one({'_id': ObjectId(project_id)})
            if not project:
                return "Project not found"

            # Access the 'runs' field of the project
            runs = project.get('runs', {})

            # Iterate through the records and update metadata
            for row in records:
                sample_name = row.get('sample_name')
                if not sample_name:
                    continue  # Skip rows without a sample_name

                # Find the corresponding sample in the runs
                sample_found = False
                for sample_key, sample_list in runs.items():
                    for sample in sample_list:
                        if sample.get('Sample_name') == sample_name:
                            sample_found = True
                            # Add metadata fields into `extra_metadata_from_csv`
                            if "extra_metadata_from_csv" not in sample:
                                sample["extra_metadata_from_csv"] = {}

                            for key, value in row.items():
                                if key != 'sample_name':  # Skip sample_name column
                                    sample["extra_metadata_from_csv"][key] = value
                            break
                    if sample_found:
                        break

                if not sample_found:
                    print(f"Sample {sample_name} not found in project {project_id}")

            # Update the project document in the database
            collection_handle.update_one(
                {'_id': ObjectId(project_id)},
                {'$set': {'runs': runs}}
            )

            return "complete"

        except Exception as e:
            logging.exception("Error processing metadata")
            return f"Error processing file: {str(e)}"

    else:
        return "Invalid request method"

def process_metadata_no_request(project_runs, metadata_file):
    """
    Updates the 'runs' field of a project dictionary with metadata from an uploaded file.

    Args:
        project_runs (dict): The 'runs' field of the project as a dictionary.
        metadata_file (UploadedFile): The metadata file uploaded by the user.

    Returns:
        dict: The updated 'runs' dictionary with appended metadata.
    """
    try:
        # Determine the file type and read the file into a Pandas DataFrame
        file_name = metadata_file.name
        if file_name.endswith('.xlsx'):
            df = pd.read_excel(metadata_file.open())  # Read Excel file
        elif file_name.endswith('.csv') or file_name.endswith('.tsv'):
            delimiter = '\t' if file_name.endswith('.tsv') else ','
            df = pd.read_csv(metadata_file, delimiter=delimiter)  # Read CSV/TSV file
        else:
            raise ValueError("Unsupported file type. Please upload a .csv, .tsv, or .xlsx file.")

        # Convert DataFrame to a list of dictionaries
        records = df.to_dict(orient='records')

        # Iterate through the records and update metadata
        for row in records:
            sample_name = row.get('sample_name')
            if not sample_name:
                continue  # Skip rows without a sample_name

            # Find the corresponding sample in the runs
            sample_found = False
            for sample_key, sample_list in project_runs.items():
                for sample in sample_list:
                    if sample.get('Sample_name') == sample_name:
                        sample_found = True
                        # Add metadata fields into `extra_metadata_from_csv`
                        if "extra_metadata_from_csv" not in sample:
                            sample["extra_metadata_from_csv"] = {}

                        for key, value in row.items():
                            if key != 'sample_name':  # Skip sample_name column
                                sample["extra_metadata_from_csv"][key] = value
                        break
                if sample_found:
                    break

            if not sample_found:
                print(f"Sample {sample_name} not found in the provided runs data.")

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
            print(f'Failed to get the metadata file from the form')
            print(e)
            return None

    