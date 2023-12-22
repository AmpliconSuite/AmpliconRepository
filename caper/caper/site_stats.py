import datetime

from .utils import get_collection_handle, collection_handle, db_handle


site_statistics_handle = get_collection_handle(db_handle,'site_statistics')

def get_latest_site_statistics():
    return site_statistics_handle.find().sort('_id', -1).limit(1).next()

def regenerate_site_statistics():
    print(db_handle.list_collection_names())
    # just get stats for all private
    all_private_proj_count = 0
    all_private_sample_count = 0
    all_private_projects = list(collection_handle.find({'private': True, 'delete': False}))
    for proj in all_private_projects:
        all_private_proj_count = all_private_proj_count + 1
        all_private_sample_count = all_private_sample_count + len(proj['runs'])
    # end private stats

    public_proj_count = 0
    public_sample_count = 0
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    # make it an array of name/values so we can add to it later
    repo_stats = {}
    repo_stats["public_proj_count"] = public_proj_count
    repo_stats["public_sample_count"] = public_sample_count
    repo_stats["all_private_proj_count"] = all_private_proj_count
    repo_stats["all_private_sample_count"] = all_private_sample_count
    repo_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(repo_stats)
    print(site_statistics_handle.count_documents({}))
    return repo_stats

