import datetime

from .project_status import LIVE, project_runs, status_query
from .publications import publication_url, count_unique_publications
from .utils import get_collection_handle, collection_handle, db_handle_primary, replace_space_to_underscore, preprocess_sample_data, get_one_sample, sample_data_from_feature_list, is_project_private, is_project_public, normalize_visibility_field, VISIBILITY_QUERY_VALUES

site_statistics_handle = get_collection_handle(db_handle_primary, 'site_statistics')

# Upload placeholders (runs={}, sample_count=0) and projects whose aggregation failed stay
# current/undeleted so their owner can still reach them, but the incremental add/delete
# functions never counted them. Regeneration has to skip them too, or it reports projects the
# running totals never included. Deliberately not filtering on 'FINISHED?': that is False on
# real projects while their files extract, and those are counted incrementally.
COMPLETED_PROJECT_FILTER = status_query(
    LIVE,
    aggregation_in_progress={'$ne': True},
    aggregation_failed={'$ne': True},
)

# Each visibility owns a set of site_statistics keys, prefixed as below. The three buckets are
# mutually exclusive and sum to the site total: unlisted ('hidden_public') projects are counted
# on their own rather than folded in with private ones, even though access control still treats
# them as private (see is_project_private).
BUCKET_PREFIXES = {
    'public': 'public',
    'private': 'all_private',
    'hidden_public': 'hidden_public',
}

# Values the 'private' field can hold for each bucket, including the legacy booleans that
# predate the string visibilities. Defined in utils so the query sites and the statistics
# buckets cannot end up matching different sets of values.
BUCKET_QUERY_VALUES = VISIBILITY_QUERY_VALUES

BUCKET_STAT_DEFAULTS = {
    'proj_count': 0,
    'sample_count': 0,
    'coral_project_count': 0,
    'coral_sample_count': 0,
    # Samples carrying at least one ecDNA amplicon. Not derivable from
    # amplicon_classifications_count, which counts amplicons: one sample can
    # carry several, so the two numbers answer different questions.
    'ecdna_positive_sample_count': 0,
    'amplicon_classifications_count': dict,
    'tissue_of_origin_count': dict,
    'cancer_type_count': dict,
    # One resolved publication URL per project that has one -- a list, not a
    # counter dict keyed by URL, because publication URLs contain dots and dots
    # are not legal in MongoDB field names. Keeping every project's entry
    # (rather than a set) is what lets a project be removed without dropping a
    # paper that another project also cites.
    'publication_links': list,
}

# Derived on read from publication_links rather than stored, so the two can
# never disagree.
UNIQUE_PUBLICATION_SUFFIX = 'unique_publication_count'
PUBLICATION_PROJECT_SUFFIX = 'projects_with_publication'


def count_ecdna_positive_samples(project):
    """Number of samples in a project carrying at least one ecDNA amplicon.

    Counted per project and stored, rather than worked out on read: answering
    it from the projects themselves means loading every project's runs, which
    is exactly what the statistics document exists to avoid.
    """
    positive = 0
    for features in project_runs(project).values():
        if any(feature.get('Classification') == 'ecDNA' for feature in features):
            positive += 1
    return positive


def is_coral_project(project):
    """Return whether a project contains results reconstructed with CoRAL."""
    tools = project.get('Reconstruction_tools', '')
    if isinstance(tools, (list, tuple, set)):
        return 'CoRAL' in tools
    return 'CoRAL' in str(tools)


def get_date():
    today = datetime.datetime.now()
    date = today.strftime('%Y-%m-%dT%H:%M:%S.%f')
    return date


def _bucket_stat_keys():
    """Yield (key, default value) for every statistics key each bucket owns."""
    for prefix in BUCKET_PREFIXES.values():
        for suffix, default in BUCKET_STAT_DEFAULTS.items():
            yield f'{prefix}_{suffix}', default() if callable(default) else default


def get_latest_site_statistics():
    # check to auto create the stats if needed
    if site_statistics_handle.count_documents({})==0:
        regenerate_site_statistics()

    latest = site_statistics_handle.find().sort('_id', -1).limit(1).next()
    # Documents written before a bucket existed lack its keys entirely, and a missing key reaches
    # templates as '' rather than as a number or dict. Fill the defaults in so every reader sees
    # the full set until the next regeneration writes them for real.
    for key, default in _bucket_stat_keys():
        latest.setdefault(key, default)

    # Derived, not stored: a handful of set operations over one string per
    # project, which costs nothing next to the queries this document exists to
    # avoid. Both numbers are wanted because they differ -- two projects can
    # cite the same paper -- and the difference is the interesting part.
    for prefix in BUCKET_PREFIXES.values():
        links = latest[f'{prefix}_publication_links']
        latest[f'{prefix}_{UNIQUE_PUBLICATION_SUFFIX}'] = count_unique_publications(links)
        latest[f'{prefix}_{PUBLICATION_PROJECT_SUFFIX}'] = len(links)

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
        if val == 0:
            sum_holder.pop(class_name, None)
        else:
            sum_holder[class_name] = val
    return sum_holder


def _sum_projects_into_bucket(projects, prefix):
    """Total up every project in one visibility bucket into that bucket's statistics keys."""
    amplicon_counts = dict()
    tissue_counts = dict()
    cancer_type_counts = dict()
    publication_links = []
    proj_count = 0
    sample_count = 0
    coral_project_count = 0
    coral_sample_count = 0
    ecdna_positive_sample_count = 0

    for proj in projects:
        class_keys, proj_amplicon_counts = get_project_amplicon_counts(proj)
        sum_amplicon_counts_by_classification(class_keys, proj_amplicon_counts, amplicon_counts)
        # sum_/subtract_tissue_of_origin_counts are plain dict arithmetic; the cancer type
        # counter reuses them so both label counters clamp and warn identically.
        sum_tissue_of_origin_counts(get_project_tissue_of_origin_counts(proj), tissue_counts)
        sum_tissue_of_origin_counts(get_project_cancer_type_counts(proj), cancer_type_counts)
        proj_count += 1
        sample_count += len(project_runs(proj))
        if is_coral_project(proj):
            coral_project_count += 1
            coral_sample_count += len(project_runs(proj))
        ecdna_positive_sample_count += count_ecdna_positive_samples(proj)
        link = publication_url(proj.get('publication_link', ''))
        if link:
            publication_links.append(link)

    return {
        f'{prefix}_proj_count': proj_count,
        f'{prefix}_sample_count': sample_count,
        f'{prefix}_coral_project_count': coral_project_count,
        f'{prefix}_coral_sample_count': coral_sample_count,
        f'{prefix}_ecdna_positive_sample_count': ecdna_positive_sample_count,
        f'{prefix}_amplicon_classifications_count': amplicon_counts,
        f'{prefix}_tissue_of_origin_count': tissue_counts,
        f'{prefix}_cancer_type_count': cancer_type_counts,
        f'{prefix}_publication_links': publication_links,
    }


def regenerate_site_statistics():
    repo_stats = {}
    for visibility, prefix in BUCKET_PREFIXES.items():
        projects = list(collection_handle.find({
            'private': {'$in': BUCKET_QUERY_VALUES[visibility]},
            **COMPLETED_PROJECT_FILTER
        }))
        repo_stats.update(_sum_projects_into_bucket(projects, prefix))

    repo_stats["date"] = get_date()
    site_statistics_handle.insert_one(repo_stats)
    print(f"SITE STATS REGENERATED FROM SCRATCH    {repo_stats['public_amplicon_classifications_count']} ")

    return repo_stats


def missing_statistics_keys():
    """Keys the code now reads that the newest stored snapshot does not carry.

    Read against the stored document rather than against
    get_latest_site_statistics, which fills the defaults in on the way out and
    so can never report anything missing.
    """
    if site_statistics_handle.count_documents({}) == 0:
        return [key for key, _ in _bucket_stat_keys()]

    stored = site_statistics_handle.find().sort('_id', -1).limit(1).next()
    return [key for key, _ in _bucket_stat_keys() if key not in stored]


def regenerate_site_statistics_if_stale():
    """Rebuild the statistics when, and only when, the stored shape is behind.

    Statistics are stored as snapshots, so a release that adds a counter leaves
    the newest snapshot without it: get_latest_site_statistics fills a zero in,
    the home page reports zero, and it stays zero until someone regenerates by
    hand. That has to be caught at startup, because nothing at runtime will.

    What it must not do is regenerate on every start. Regeneration reads every
    project document in full -- ``runs`` included, which is nearly all of the
    bytes -- and that is tens to hundreds of megabytes off a remote database,
    paid in the master process before gunicorn forks a worker that can serve
    anything. So the check is one query, and the scan happens only when the
    shape actually changed.

    This is deliberately not a fix for counters drifting from the truth: drift
    accrues while the site is running, not while it is booting, so restarting is
    the wrong moment to look for it. Regenerate from the admin page for that.

    Returns the keys that were missing, or [] if the snapshot was current.
    """
    missing = missing_statistics_keys()
    if missing:
        regenerate_site_statistics()
    return missing


def _carry_forward_buckets(current_stats):
    """Copy every bucket's keys onto a new statistics document.

    Statistics are stored as full snapshots rather than deltas, so the buckets that aren't
    changing still have to be written out or their totals would be lost.
    """
    return {key: current_stats.get(key, default) for key, default in _bucket_stat_keys()}


def _apply_project_to_site_statistics(project, visibility, sign):
    """Add (sign=1) or remove (sign=-1) one project's contribution to its visibility bucket."""
    prefix = BUCKET_PREFIXES[normalize_visibility_field(visibility)]
    updated_stats = _carry_forward_buckets(get_latest_site_statistics())
    sample_count = len(project_runs(project))

    updated_stats[f'{prefix}_proj_count'] += sign
    updated_stats[f'{prefix}_sample_count'] += sign * sample_count
    if is_coral_project(project):
        updated_stats[f'{prefix}_coral_project_count'] = max(
            0, updated_stats[f'{prefix}_coral_project_count'] + sign)
        updated_stats[f'{prefix}_coral_sample_count'] = max(
            0, updated_stats[f'{prefix}_coral_sample_count'] + sign * sample_count)
    updated_stats[f'{prefix}_ecdna_positive_sample_count'] = max(
        0, updated_stats[f'{prefix}_ecdna_positive_sample_count']
        + sign * count_ecdna_positive_samples(project))

    class_keys, amplicon_counts = get_project_amplicon_counts(project)
    tissue_counts = get_project_tissue_of_origin_counts(project)
    cancer_type_counts = get_project_cancer_type_counts(project)
    amplicon_holder = updated_stats[f'{prefix}_amplicon_classifications_count']
    tissue_holder = updated_stats[f'{prefix}_tissue_of_origin_count']
    cancer_type_holder = updated_stats[f'{prefix}_cancer_type_count']
    if sign > 0:
        sum_amplicon_counts_by_classification(class_keys, amplicon_counts, amplicon_holder)
        sum_tissue_of_origin_counts(tissue_counts, tissue_holder)
        sum_tissue_of_origin_counts(cancer_type_counts, cancer_type_holder)
    else:
        subtract_amplicon_counts_by_classification(class_keys, amplicon_counts, amplicon_holder)
        subtract_tissue_of_origin_counts(tissue_counts, tissue_holder)
        subtract_tissue_of_origin_counts(cancer_type_counts, cancer_type_holder)

    # A project's publication is one entry, so removal takes out one entry and
    # leaves any other project citing the same paper counted. If the stored
    # value changed since the project was added there is nothing to remove and
    # the list drifts -- the same exposure the label counters above already
    # carry, and the same fix: regenerate.
    links_holder = updated_stats[f'{prefix}_publication_links']
    link = publication_url(project.get('publication_link', ''))
    if link:
        if sign > 0:
            links_holder.append(link)
        elif link in links_holder:
            links_holder.remove(link)

    updated_stats["date"] = get_date()
    site_statistics_handle.insert_one(updated_stats)


def add_project_to_site_statistics(project, visibility='private'):
    """
    Adds a project's statistics to the site-wide statistics.

    Args:
        project (dict): Project dictionary
        visibility: 'public', 'private' or 'hidden_public'. Legacy booleans are accepted and
            normalized (True -> private, False -> public), so they can never select the
            hidden_public bucket.
    """
    _apply_project_to_site_statistics(project, visibility, sign=1)


def delete_project_from_site_statistics(project, visibility='private'):
    """
    Removes a project's statistics from the site-wide statistics.

    Args:
        project (dict): Project dictionary
        visibility: 'public', 'private' or 'hidden_public'. Legacy booleans are accepted and
            normalized (True -> private, False -> public), so they can never select the
            hidden_public bucket.
    """
    _apply_project_to_site_statistics(project, visibility, sign=-1)


def edit_proj_privacy(project, old_privacy, new_privacy):
    """
    Edits site stats based on old and new project privacy settings.

    Handles both legacy boolean values and new string visibility values. Each visibility has
    its own bucket, so a move between any two of them shifts the project's contribution.
    """
    old_visibility = normalize_visibility_field(old_privacy)
    new_visibility = normalize_visibility_field(new_privacy)

    if old_visibility == new_visibility:
        return

    delete_project_from_site_statistics(project, old_visibility)
    add_project_to_site_statistics(project, new_visibility)


def get_project_amplicon_counts(project):
    project_linkid = project['_id']
    amplicon_counts = dict()
    class_keys = set()
    runs = project_runs(project)
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
    runs = project_runs(project)
    
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


def get_project_cancer_type_counts(project):
    """
    Counts the number of samples per Cancer_type in a project.

    Mirrors get_project_tissue_of_origin_counts. Kept as its own counter rather than derived
    on read: the home page needs to report how many public samples carry a cancer type label,
    and working that out from the projects themselves would mean loading every project's runs
    on a page that otherwise touches no sample data at all.

    Args:
        project (dict): Project dictionary containing runs data

    Returns:
        dict: Dictionary with Cancer_type as keys and counts as values
    """
    cancer_type_counts = dict()
    runs = project_runs(project)

    for sample_num in runs.keys():
        sample_data = runs[sample_num]
        # Check if sample_data is a list (array of features) or dict
        if isinstance(sample_data, list) and len(sample_data) > 0:
            # Get the first feature to access sample-level metadata
            sample_info = sample_data[0]
        else:
            sample_info = sample_data

        cancer_type = sample_info.get('Cancer_type', None)

        # Only count if Cancer_type exists and is not None/empty
        if cancer_type and str(cancer_type).strip():
            cancer_type = str(cancer_type).strip()
            cancer_type_counts[cancer_type] = cancer_type_counts.get(cancer_type, 0) + 1

    return cancer_type_counts


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
        if val == 0:
            sum_holder.pop(tissue_name, None)
        else:
            sum_holder[tissue_name] = val
    
    return sum_holder
