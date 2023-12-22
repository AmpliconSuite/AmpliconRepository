import datetime

from .utils import get_collection_handle, collection_handle, db_handle


site_statistics_handle = get_collection_handle(db_handle,'site_statistics')

def get_latest_site_statistics():
    return site_statistics_handle.find().sort('_id', -1).limit(1).next()

def regenerate_site_statistics():

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
    #print(site_statistics_handle.count_documents({}))
    print("SITE STATS REGENERATED FROM SCRATCH")
    return repo_stats

#
# shortcut for updating stats when a new project is added so that we don't have to go voer the whole
# Db again to calculate.  We will have to do that when a project is updated with a new file though
# since we are keeping summary stats, not per project stats and can't easily remove the old details
# after the update.
#
def add_project_to_site_statistics(project):
    current_stats = get_latest_site_statistics()
    updated_stats = {}
    updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"]+1
    updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"] + len(project['runs'])

    print(f"ADD     PRIVATE IS {  project['private']  }")
    if project['private'] == True:
        # private proj, don't change public stats
        updated_stats["public_proj_count"] = current_stats["public_proj_count"]
        updated_stats["public_sample_count"] = current_stats["public_proj_count"]
    else:
        # change to public
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] + 1
        updated_stats["public_sample_count"] = current_stats["public_proj_count"] + len(project['runs'])

    updated_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(updated_stats)

def delete_project_from_site_statistics(project):
    current_stats = get_latest_site_statistics()
    updated_stats = {}
    updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"]-1
    updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"] - len(project['runs'])

    print(f"DELETE PRIVATE IS { project['private'] }     current {current_stats} ")
    if project['private'] == True:
        # public stats unchanged deleting private
        updated_stats["public_proj_count"] = current_stats["public_proj_count"]
        updated_stats["public_sample_count"] = current_stats["public_proj_count"]
    else:
        #  public stats updated
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] - 1
        updated_stats["public_sample_count"] = current_stats["public_proj_count"] - len(project['runs'])

    print(f"DELETE                                 updated {updated_stats} ")
    updated_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(updated_stats)
