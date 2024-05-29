from .utils import *



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
    