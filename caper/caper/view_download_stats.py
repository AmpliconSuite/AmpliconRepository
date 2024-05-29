from .utils import *

def session_visit(request, project):
    """
    If the user session hasn't viewed that project page yet, then record it.
    If it has visited, don't increment. 
    
    """
    ## if the user session hasn't visited the project page yet, increment. 
    proj_id = project['_id']
    if (request.session.get(f'visited_{proj_id}') is None) or (request.session.get(f'visited_{proj_id}') == False):
        ## increment:
        
        request.session[f'visited_{proj_id}'] = True
        
        return get_increment_view_and_download_statistics(project)
    else:
        ## only get current stats
        query = {'_id':project['_id']}
        res = collection_handle.find_one(query, {'views':1, 'downloads':1})
        return [res.get('views'), res.get('downloads')]


def get_increment_view_and_download_statistics(project):
    
    """
    Gets the view and download statistics for a project, adds 1, and returns it. 
    
    """
    query = {'_id':project['_id']}
    if ('views' not in project.keys()) or ('downloads' not in project.keys()) :
        
        collection_handle.update(query, {
            '$set':{
                'views':1,
                'downloads':0
            }
        })
        
        return [1, 0]
    
    else:
        collection_handle.update(query, {
            '$inc':{
                'views':1,
            }
        })
        res = collection_handle.find_one(query, {'views':1, 'downloads':1})
        return [res.get('views'), res.get('downloads')]

def increment_download(project):
    """
    Increments download count
    """
    query = {'_id':project['_id']}
    collection_handle.update(query, {
        '$inc':{
            'downloads':1
        }
    })
    