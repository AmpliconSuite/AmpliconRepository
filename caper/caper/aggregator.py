import gp
import os
from django.core.files.storage import FileSystemStorage
import datetime
GP_SERVER = gp.GPServer('http://beta.genepattern.org/gp','edwin5588', 'edwin123')
def upload_files(file_list, gpserver = GP_SERVER):
    
    """
    From a list of files, upload each file to the gp server.
    
    return a list of GP file URLs
    """
    urls = []
    for fp in file_list:
        filename = os.path.basename(fp)
        uploaded_file = gpserver.upload_file(filename, fp)
        urls.append(uploaded_file.get_url())
    return urls

def run_aggregator(results, proj_name, email, gpserver = GP_SERVER):
    """
    Runs aggregator on GP, and GP will post it back to AmpRepo
    
    
    results --> list of filepaths to project files
    Try with list of URLs, if that doesnt work make a .txt file with URLs as each line 
    proj_name --> project name for the new project to create
    email --> the email of the user
    """
    file_urls = upload_files(results)
    
    ## run aggregator
    module = gp.GPTask(gpserver, 'AmpliconSuiteAggregator')
    module.param_load()
    job_spec = module.make_job_spec()
    job_spec.set_parameter('Amplicon.Architect.Results', file_urls)
    job_spec.set_parameter('project_name', (proj_name + f"_via_GP"))
    job_spec.set_parameter('Amplicon.Repository.Email', email)
    job_spec.set_parameter('accept.license', 'Yes')
    
    job = gpserver.run_job(job_spec)
    return job

def run_amplicon_suite_aggregator(files, proj_id, form, user):
    '''
    runs amp suite aggregator via GP rest API
    '''
    project_data_path =  f"tmp/{proj_id}"
    file_fps = []
    proj_name = form['project_name']
    print(user)
    try: 
        email = user.email
    except:
        print('need user email')
    for file in files:
        
        fs = FileSystemStorage(location = project_data_path)
        saved = fs.save(file.name, file)
        print('file saved')
        fp = os.path.join(project_data_path, file.name)
        file_fps.append(fp)
        
    job = run_aggregator(file_fps, proj_name, email)
    return job