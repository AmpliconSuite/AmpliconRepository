import requests
import os
import uuid

def post_package(file_paths, data, server='local-debug'):
    """
    Posts multiple files to the /create_project/ endpoint.

    :param file_paths: List of file paths to upload.
    :param data: Dictionary containing project metadata:
        {'project_name': string,
         'description': string,
         'publication_link': string,
         'private': boolean,
         'project_members': list,
         'accept_license': boolean}
    :param server: Server type ('prod', 'dev', 'local-debug').
    """
    servers = {
        'prod': 'https://ampliconrepository.org',
        'dev': 'https://dev.ampliconrepository.org',
        'local-debug': 'http://127.0.0.1:8000'
    }

    if server not in servers:
        raise ValueError(f"Unrecognized server option: {server}")

    homepage = servers[server]
    upload_url = f"{homepage}/create-project/"

    session = requests.Session()
    response = session.get(homepage)
    csrf_token = response.cookies.get('csrftoken')

    if not csrf_token:
        raise Exception("Failed to retrieve CSRF token. Ensure CSRF protection is disabled for API access or pass a valid token.")

    # Prepare multiple files
    files = [('document', (os.path.basename(fp), open(fp, 'rb'), 'application/gzip')) for fp in file_paths]

    response = session.post(upload_url, data=data, files=files, headers={'X-CSRFToken': csrf_token})

    print(f"Upload response: {response.status_code} - {response.text}")

    for _, f, _ in files:
        f.close()

# Example Usage
if __name__ == '__main__':
    file_paths = [
        '/Users/edwinhuang/Downloads/650086713d950b28f5361766.tar.gz'
    ]

    project_data = {
        "project_name": "Test_Project",
        "description": "A test project for API upload",
        "publication_link": "https://example.com",
        "private": False,
        "project_members": ["test021225"],
        "accept_license": True
    }

    post_package(file_paths, project_data, server="local-debug")