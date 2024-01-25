import datetime

from .utils import get_collection_handle, collection_handle, db_handle, replace_space_to_underscore, preprocess_sample_data, get_one_sample, sample_data_from_feature_list

site_statistics_handle = get_collection_handle(db_handle,'site_statistics')


def get_latest_site_statistics():
    # check to auto create the stats if needed
    if site_statistics_handle.find().count()==0:
        regenerate_site_statistics()

    latest = site_statistics_handle.find().sort('_id', -1).limit(1).next()
    # for public display we want to collect these 3, this is a backstop for backwards compatibility
    if latest['public_amplicon_classifications_count'].get('otherfscna') == None:
       linear = latest['public_amplicon_classifications_count'].get('Linear_amplification',0)
       # unclassified = latest['public_amplicon_classifications_count'].get('Unclassified', 0)
       virus = latest['public_amplicon_classifications_count'].get('Virus', 0)
       cnc =latest['public_amplicon_classifications_count'].get('Complex_non_cyclic', 0)
       latest['public_amplicon_classifications_count']['otherfscna'] = linear + cnc + virus

    return latest


def sum_amplicon_counts_by_classification(class_keys, class_values, sum_holder, sum_sign=1):
    for class_name in class_keys:
        prev = 0
        if sum_holder.get(class_name) != None:
            prev = sum_holder.get(class_name)

        sum_holder[class_name] = prev + sum_sign * class_values.get(class_name)
    return sum_holder


def subtract_amplicon_counts_by_classification(class_keys, class_values, sum_holder):
    for class_name in class_keys:
        prev = 0
        if sum_holder.get(class_name) != None:
            prev = sum_holder.get(class_name)

        val = prev - class_values.get(class_name)
        if (val < 0):
            print(" NEGATIVE AMPOLICON COUNTS IN SITE STATS AFTER PROJECT DELETION -- SHOULD REGENRATE ALL SITE STATS")
            val = 0
        sum_holder[class_name] = val
    return sum_holder


def regenerate_site_statistics():
    pub_amplicon_counts = dict()
    priv_amplicon_counts = dict()

    # just get stats for all private
    all_private_proj_count = 0
    all_private_sample_count = 0
    all_private_projects = list(collection_handle.find({'private': True, 'delete': False}))
    for proj in all_private_projects:
        class_keys, amplicon_counts = get_project_amplicon_counts(proj)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts)
        all_private_proj_count = all_private_proj_count + 1
        all_private_sample_count = all_private_sample_count + len(proj['runs'])
    # end private stats

    public_proj_count = 0
    public_sample_count = 0
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        class_keys, amplicon_counts = get_project_amplicon_counts(proj)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts)
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    print(f"  pub amp counts is  == {pub_amplicon_counts} ")
    # make it an array of name/values so we can add to it later
    repo_stats = {}
    repo_stats["public_proj_count"] = public_proj_count
    repo_stats["public_sample_count"] = public_sample_count
    repo_stats["all_private_proj_count"] = all_private_proj_count
    repo_stats["all_private_sample_count"] = all_private_sample_count
    repo_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts
    repo_stats["public_amplicon_classifications_count"] = pub_amplicon_counts

    repo_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(repo_stats)
    #print(site_statistics_handle.count_documents({}))
    print(f"SITE STATS REGENERATED FROM SCRATCH    {pub_amplicon_counts} ")

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
        updated_stats["public_sample_count"] = current_stats["public_sample_count"]
        priv_amplicon_counts = current_stats["all_private_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts)
        updated_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts
        updated_stats["public_amplicon_classifications_count"]=current_stats["public_amplicon_classifications_count"]
    else:
        # change to public
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] + 1
        updated_stats["public_sample_count"] = current_stats["public_sample_count"] + len(project['runs'])
        pub_amplicon_counts = current_stats["public_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts)
        updated_stats["public_amplicon_classifications_count"] = pub_amplicon_counts
        updated_stats["all_private_amplicon_classifications_count"] = current_stats["all_private_amplicon_classifications_count"]

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
        updated_stats["public_sample_count"] = current_stats["public_sample_count"]
        priv_amplicon_counts = current_stats["all_private_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts, sum_sign=-1)
        updated_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts
        updated_stats["public_amplicon_classifications_count"]=current_stats["public_amplicon_classifications_count"]
    else:
        #  public stats updated
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] - 1
        updated_stats["public_sample_count"] = current_stats["public_sample_count"] - len(project['runs'])
        pub_amplicon_counts = current_stats["public_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts, sum_sign=-1)
        updated_stats["public_amplicon_classifications_count"] = pub_amplicon_counts
        updated_stats["all_private_amplicon_classifications_count"] = current_stats["all_private_amplicon_classifications_count"]

    print(f"DELETE                                 updated {updated_stats} ")
    updated_stats["date"] = datetime.datetime.today()
    new_id = site_statistics_handle.insert_one(updated_stats)


def get_project_amplicon_counts(project):
    project_linkid = project['_id']
    amplicon_counts = dict()
    class_keys = set()
    runs = project['runs']
    for sample_num in runs.keys():
        features = runs[sample_num]
        for feat in features:
            #print(f"FEATURE: {feat} ")
            classification = feat['Classification']
            if (classification == None):
                classification = "Unclassified"
            class_counts = 0
            if (amplicon_counts.get(classification) != None):
                class_counts = amplicon_counts.get(classification)

            amplicon_counts[classification] = class_counts +1
            class_keys.add(classification)

            # temporary (?) hack to not have spaces to make using this in the django templates easier
            if classification == 'Complex non-cyclic':
                amplicon_counts['Complex_non_cyclic'] = amplicon_counts['Complex non-cyclic']
                class_keys.add('Complex_non_cyclic')
            if classification == "Complex-non-cyclic":
                amplicon_counts['Complex_non_cyclic'] = amplicon_counts['Complex-non-cyclic']
                class_keys.add('Complex_non_cyclic')
            if classification == 'Linear amplification':
                amplicon_counts['Linear_amplification'] = amplicon_counts['Linear amplification']
                class_keys.add('Linear_amplification')
            if classification == 'Linear':
                amplicon_counts['Linear_amplification'] = amplicon_counts['Linear']
                class_keys.add('Linear_amplification')

    # on the index page we will want to dispplay the sum of these 3 counts as other fsCNA
    linear = amplicon_counts.get('Linear_amplification', 0)
    # unclassified = amplicon_counts.get('Unclassified', 0)
    cnc = amplicon_counts.get('Complex_non_cyclic', 0)
    virus = amplicon_counts.get('Virus', 0)
    amplicon_counts['otherfscna'] = linear + cnc + virus


    return class_keys, amplicon_counts