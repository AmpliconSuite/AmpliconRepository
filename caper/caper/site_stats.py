import datetime

from .utils import get_collection_handle, collection_handle, db_handle, replace_space_to_underscore, preprocess_sample_data, get_one_sample, sample_data_from_feature_list

site_statistics_handle = get_collection_handle(db_handle,'site_statistics')


def get_date():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%dT%H:%M:%S.%f')
    return date


def get_latest_site_statistics():
    # check to auto create the stats if needed
    if site_statistics_handle.count_documents({})==0:
        regenerate_site_statistics()

    latest = site_statistics_handle.find().sort('_id', -1).limit(1).next()
    # for public display we want to collect these 3, this is a backstop for backwards compatibility
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
    pub_tissue_counts = dict()
    priv_tissue_counts = dict()

    # just get stats for all private
    all_private_proj_count = 0
    all_private_sample_count = 0
    all_private_projects = list(collection_handle.find({'private': True, 'delete': False}))
    for proj in all_private_projects:
        class_keys, amplicon_counts = get_project_amplicon_counts(proj)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts)
        tissue_counts = get_project_tissue_of_origin_counts(proj)
        sum_tissue_of_origin_counts(tissue_counts, priv_tissue_counts)
        all_private_proj_count = all_private_proj_count + 1
        all_private_sample_count = all_private_sample_count + len(proj['runs'])
    # end private stats

    public_proj_count = 0
    public_sample_count = 0
    public_projects = list(collection_handle.find({'private': False, 'delete': False}))
    for proj in public_projects:
        class_keys, amplicon_counts = get_project_amplicon_counts(proj)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts)
        tissue_counts = get_project_tissue_of_origin_counts(proj)
        sum_tissue_of_origin_counts(tissue_counts, pub_tissue_counts)
        public_proj_count = public_proj_count + 1
        public_sample_count = public_sample_count + len(proj['runs'])

    print(f"  pub amp counts is  == {pub_amplicon_counts} ")
    print(f"  pub tissue counts is  == {pub_tissue_counts} ")
    # make it an array of name/values so we can add to it later
    repo_stats = {}
    repo_stats["public_proj_count"] = public_proj_count
    repo_stats["public_sample_count"] = public_sample_count
    repo_stats["all_private_proj_count"] = all_private_proj_count
    repo_stats["all_private_sample_count"] = all_private_sample_count
    repo_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts
    repo_stats["public_amplicon_classifications_count"] = pub_amplicon_counts
    repo_stats["all_private_tissue_of_origin_count"] = priv_tissue_counts
    repo_stats["public_tissue_of_origin_count"] = pub_tissue_counts

    repo_stats["date"] = get_date()
    new_id = site_statistics_handle.insert_one(repo_stats)
    #print(site_statistics_handle.count_documents({}))
    print(f"SITE STATS REGENERATED FROM SCRATCH    {pub_amplicon_counts} ")

    return repo_stats


def add_project_to_site_statistics(project, is_private=False):
    """
    Adds a project's statistics to the site-wide statistics.

    Args:
        project (dict): Project dictionary
        is_private (bool): If True, add to private stats; if False, add to public stats
    """
    current_stats = get_latest_site_statistics()
    updated_stats = {}

    if is_private:
        # Adding to private stats
        updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"] + 1
        updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"] + len(project['runs'])

        # Get amplicon counts for private stats
        priv_amplicon_counts = current_stats["all_private_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts)
        updated_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts

        # Get tissue of origin counts for private stats
        priv_tissue_counts = current_stats.get("all_private_tissue_of_origin_count", {})
        tissue_counts = get_project_tissue_of_origin_counts(project)
        sum_tissue_of_origin_counts(tissue_counts, priv_tissue_counts)
        updated_stats["all_private_tissue_of_origin_count"] = priv_tissue_counts

        # Public stats remain unchanged
        updated_stats["public_proj_count"] = current_stats["public_proj_count"]
        updated_stats["public_sample_count"] = current_stats["public_sample_count"]
        updated_stats["public_amplicon_classifications_count"] = current_stats["public_amplicon_classifications_count"]
        updated_stats["public_tissue_of_origin_count"] = current_stats.get("public_tissue_of_origin_count", {})
    else:
        # Adding to public stats
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] + 1
        updated_stats["public_sample_count"] = current_stats["public_sample_count"] + len(project['runs'])

        # Get amplicon counts for public stats
        pub_amplicon_counts = current_stats["public_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts)
        updated_stats["public_amplicon_classifications_count"] = pub_amplicon_counts

        # Get tissue of origin counts for public stats
        pub_tissue_counts = current_stats.get("public_tissue_of_origin_count", {})
        tissue_counts = get_project_tissue_of_origin_counts(project)
        sum_tissue_of_origin_counts(tissue_counts, pub_tissue_counts)
        updated_stats["public_tissue_of_origin_count"] = pub_tissue_counts

        # Private stats remain unchanged
        updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"]
        updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"]
        updated_stats["all_private_amplicon_classifications_count"] = current_stats[
            "all_private_amplicon_classifications_count"]
        updated_stats["all_private_tissue_of_origin_count"] = current_stats.get("all_private_tissue_of_origin_count", {})

    updated_stats["date"] = get_date()
    new_id = site_statistics_handle.insert_one(updated_stats)


def delete_project_from_site_statistics(project, is_private):
    """
    Removes a project's statistics from the site-wide statistics.

    Args:
        project (dict): Project dictionary
        is_private (bool): If True, remove from private stats; if False, remove from public stats
    """
    current_stats = get_latest_site_statistics()
    updated_stats = {}

    if is_private:
        # Removing from private stats
        updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"] - 1
        updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"] - len(project['runs'])

        # Get amplicon counts to subtract from private stats
        priv_amplicon_counts = current_stats["all_private_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        subtract_amplicon_counts_by_classification(class_keys, amplicon_counts, priv_amplicon_counts)
        updated_stats["all_private_amplicon_classifications_count"] = priv_amplicon_counts

        # Get tissue of origin counts to subtract from private stats
        priv_tissue_counts = current_stats.get("all_private_tissue_of_origin_count", {})
        tissue_counts = get_project_tissue_of_origin_counts(project)
        subtract_tissue_of_origin_counts(tissue_counts, priv_tissue_counts)
        updated_stats["all_private_tissue_of_origin_count"] = priv_tissue_counts

        # Public stats remain unchanged
        updated_stats["public_proj_count"] = current_stats["public_proj_count"]
        updated_stats["public_sample_count"] = current_stats["public_sample_count"]
        updated_stats["public_amplicon_classifications_count"] = current_stats["public_amplicon_classifications_count"]
        updated_stats["public_tissue_of_origin_count"] = current_stats.get("public_tissue_of_origin_count", {})
    else:
        # Removing from public stats
        updated_stats["public_proj_count"] = current_stats["public_proj_count"] - 1
        updated_stats["public_sample_count"] = current_stats["public_sample_count"] - len(project['runs'])

        # Get amplicon counts to subtract from public stats
        pub_amplicon_counts = current_stats["public_amplicon_classifications_count"]
        class_keys, amplicon_counts = get_project_amplicon_counts(project)
        subtract_amplicon_counts_by_classification(class_keys, amplicon_counts, pub_amplicon_counts)
        updated_stats["public_amplicon_classifications_count"] = pub_amplicon_counts

        # Get tissue of origin counts to subtract from public stats
        pub_tissue_counts = current_stats.get("public_tissue_of_origin_count", {})
        tissue_counts = get_project_tissue_of_origin_counts(project)
        subtract_tissue_of_origin_counts(tissue_counts, pub_tissue_counts)
        updated_stats["public_tissue_of_origin_count"] = pub_tissue_counts

        # Private stats remain unchanged
        updated_stats["all_private_proj_count"] = current_stats["all_private_proj_count"]
        updated_stats["all_private_sample_count"] = current_stats["all_private_sample_count"]
        updated_stats["all_private_amplicon_classifications_count"] = current_stats[
            "all_private_amplicon_classifications_count"]
        updated_stats["all_private_tissue_of_origin_count"] = current_stats.get("all_private_tissue_of_origin_count", {})

    updated_stats["date"] = get_date()
    new_id = site_statistics_handle.insert_one(updated_stats)


def edit_proj_privacy(project, old_privacy, new_privacy):
    """
    Edits site stats based on old and new project privacy settings.
    """
    ## going from private to public:
    if (old_privacy == True) and (new_privacy == False):
        delete_project_from_site_statistics(project, is_private=True)  # Remove from private stats
        add_project_to_site_statistics(project, is_private=False)  # Add to public stats

    ## going from public to private:
    elif (old_privacy == False) and (new_privacy == True):
        delete_project_from_site_statistics(project, is_private=False)  # Remove from public stats
        add_project_to_site_statistics(project, is_private=True)  # Add to private stats


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


def get_project_tissue_of_origin_counts(project):
    """
    Counts the number of samples per tissue_of_origin in a project.
    
    Args:
        project (dict): Project dictionary containing runs data
        
    Returns:
        dict: Dictionary with tissue_of_origin as keys and counts as values
    """
    tissue_counts = dict()
    runs = project['runs']
    
    for sample_num in runs.keys():
        sample_data = runs[sample_num]
        # Check if sample_data is a list (array of features) or dict
        if isinstance(sample_data, list) and len(sample_data) > 0:
            # Get the first feature to access sample-level metadata
            sample_info = sample_data[0]
        else:
            sample_info = sample_data
            
        # Get tissue_of_origin from the sample
        tissue = sample_info.get('Tissue_of_origin', None)
        
        # Only count if tissue_of_origin exists and is not None/empty
        if tissue and str(tissue).strip():
            tissue = str(tissue).strip()
            tissue_counts[tissue] = tissue_counts.get(tissue, 0) + 1
    
    return tissue_counts


def sum_tissue_of_origin_counts(tissue_counts, sum_holder):
    """
    Adds tissue_of_origin counts to the running sum.
    
    Args:
        tissue_counts (dict): Dictionary of tissue_of_origin counts to add
        sum_holder (dict): Dictionary holding the cumulative counts
        
    Returns:
        dict: Updated sum_holder with added counts
    """
    for tissue_name, count in tissue_counts.items():
        prev = sum_holder.get(tissue_name, 0)
        sum_holder[tissue_name] = prev + count
    
    return sum_holder


def subtract_tissue_of_origin_counts(tissue_counts, sum_holder):
    """
    Subtracts tissue_of_origin counts from the running sum.
    
    Args:
        tissue_counts (dict): Dictionary of tissue_of_origin counts to subtract
        sum_holder (dict): Dictionary holding the cumulative counts
        
    Returns:
        dict: Updated sum_holder with subtracted counts
    """
    for tissue_name, count in tissue_counts.items():
        prev = sum_holder.get(tissue_name, 0)
        val = prev - count
        if val < 0:
            print(f" NEGATIVE TISSUE_OF_ORIGIN COUNTS IN SITE STATS AFTER PROJECT DELETION -- SHOULD REGENERATE ALL SITE STATS (tissue: {tissue_name})")
            val = 0
        sum_holder[tissue_name] = val
    
    return sum_holder