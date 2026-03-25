import re
from pymongo import MongoClient
from .utils import *


def wildcard_to_regex(pattern):
    """
    Converts a glob-style wildcard pattern (using * as wildcard) to a regex pattern.
    If the pattern contains no *, returns None (caller should use substring matching).

    Examples:
        FLO*  ->  ^FLO.*$       (starts with FLO)
        *LO*  ->  ^.*LO.*$      (contains LO)
        F*H   ->  ^F.*H$        (starts with F, ends with H)
        EGFR  ->  None          (no wildcard, unchanged)
    """
    if '*' not in pattern:
        return None
    parts = pattern.split('*')
    escaped_parts = [re.escape(p) for p in parts]
    return '^' + '.*'.join(escaped_parts) + '$'


def _gene_matches(query_gene, gene_list):
    """
    Check whether query_gene (which may contain a * wildcard) matches
    any entry in gene_list (already normalised to upper-case strings).
    """
    regex_pat = wildcard_to_regex(query_gene.upper())
    if regex_pat:
        compiled = re.compile(regex_pat, re.IGNORECASE)
        return any(compiled.match(g) for g in gene_list)
    else:
        return query_gene.upper() in gene_list

def perform_search(genequery=None,
                   project_name=None, 
                   classquery=None, 
                   metadata_sample_name=None,
                   metadata_sample_type=None,
                   metadata_cancer_type=None,
                   metadata_tissue_origin=None, 
                   extra_metadata=None,
                   user=None):

    gen_query = {'$regex': genequery } if genequery else None

    # Build the project-name MongoDB filter, respecting * wildcards
    if project_name:
        wc_regex = wildcard_to_regex(project_name)
        if wc_regex:
            name_filter = {'$regex': wc_regex, '$options': 'i'}
        else:
            name_filter = {'$regex': project_name, '$options': 'i'}
    else:
        name_filter = None

    # Gene Search
    # Use $in to handle both legacy boolean values (True/False) and current string values
    # ('private'/'hidden_public' for restricted, 'public' for open access)
    if user.is_authenticated:
        username = user.username
        useremail = user.email
        query_obj = {
            'private': {'$in': [True, 'private', 'hidden_public']},
            "$or": [{"project_members": username}, {"project_members": useremail}],
            'delete': False,
            'current': True
        }

        if name_filter:
            query_obj['project_name'] = name_filter

        private_projects = list(collection_handle.find(query_obj))
    else:
        private_projects = []

    public_query = {'private': {'$in': [False, 'public']}, 'delete': False, 'current': True}

    if name_filter:
        public_query['project_name'] = name_filter

    public_projects = list(collection_handle.find(public_query))

    for proj in private_projects:
        prepare_project_linkid(proj)
    for proj in public_projects:
        prepare_project_linkid(proj)

    # Fetch sample data based on new metadata fields
    public_sample_data = get_samples_from_features(
        public_projects, genequery=genequery, classquery=classquery,
        metadata_sample_name=metadata_sample_name, metadata_sample_type=metadata_sample_type, metadata_cancer_type=metadata_cancer_type,
        metadata_tissue_origin=metadata_tissue_origin, extra_metadata = extra_metadata
    )

    private_sample_data = get_samples_from_features(
        private_projects, genequery=genequery, classquery=classquery,
        metadata_sample_name=metadata_sample_name, metadata_sample_type=metadata_sample_type, metadata_cancer_type=metadata_cancer_type,
        metadata_tissue_origin=metadata_tissue_origin, extra_metadata = extra_metadata
    )

    # Extract project names from sample data
    public_project_names = {sample["project_name"] for sample in public_sample_data}
    private_project_names = {sample["project_name"] for sample in private_sample_data}

    # Filter projects to only include those found in sample data
    public_projects = [proj for proj in public_projects if proj["project_name"] in public_project_names]
    private_projects = [proj for proj in private_projects if proj["project_name"] in private_project_names]

    return {
        "public_projects": public_projects,
        "private_projects": private_projects,
        "public_sample_data": public_sample_data,
        "private_sample_data": private_sample_data
    }
    
    
def collect_metadata_samples(sample_data, metadata_to_find):
    """
    collects the samples with matching metadata to find
    """
    samples_to_return = []
    fields_to_search_in = ['Sample_type', 'Cancer_type', 'Tissue_of_origin']
    for sample in sample_data:
        for field in fields_to_search_in:
            if metadata_to_find.lower() == sample[field].lower():
                samples_to_return.append(sample)
        if metadata_to_find.lower() in [val.lower() for val in sample.get('extra_metadata_from_csv', {}).values()]:
            samples_to_return.append(sample)
    return samples_to_return


def add_extra_metadata(df):
    '''
    adds extra metadata to df
    '''
    if 'extra_metadata_from_csv' in df.columns:
        ## metadata filtering:
        corresponding_sample = df[df.extra_metadata_from_csv.notnull()].iloc[0].Sample_name
        extra_metadata_from_csv = df[df.extra_metadata_from_csv.notnull()].iloc[0].extra_metadata_from_csv
        for k, v in extra_metadata_from_csv.items():
            if k == 'sample_name':
                df.loc[df.Sample_name == corresponding_sample,'Sample_name' ]= v
            elif k == 'sample_type':
                df.loc[df.Sample_name == corresponding_sample, 'Sample_type'] = v
            elif k == 'cancer_type':
                df.loc[df.Sample_name == corresponding_sample, 'Cancer_type'] = v
            else:
                df.loc[df.Sample_name == corresponding_sample, k] = v
                

        return df, extra_metadata_from_csv
    return df, None


def get_samples_from_features(projects, genequery, classquery, metadata_sample_name, metadata_sample_type,
                              metadata_cancer_type, metadata_tissue_origin, extra_metadata):
    """
    Takes in a features_list dict, and finds matches for samples for some: 

    genequery: str
    classquery: str
    metadata: str

    returns a list of samples and feature_ids
    """

    sample_data = []
    for project in projects:
        project_name = project['project_name']
        project_linkid = project['_id']
        features = project['runs']
        features_list = replace_space_to_underscore(features)

        df = pd.DataFrame(features_list)
        df, extra_metadata_from_csv = add_extra_metadata(df)

        if genequery and 'All_genes' in df.columns:
            # Parse gene query for multi-gene search with | (OR) and & (AND) operators
            if '&' in genequery:
                # AND logic: sample must have ALL genes (each may contain a wildcard)
                genes_to_find = [g.strip().upper() for g in genequery.split('&') if g.strip()]
                df = df[df['All_genes'].apply(lambda x: all(
                    _gene_matches(gene, [g.replace("'", "").strip().upper() for g in x])
                    for gene in genes_to_find
                ))]
            elif '|' in genequery:
                # OR logic: sample must have ANY of the genes (each may contain a wildcard)
                genes_to_find = [g.strip().upper() for g in genequery.split('|') if g.strip()]
                df = df[df['All_genes'].apply(lambda x: any(
                    _gene_matches(gene, [g.replace("'", "").strip().upper() for g in x])
                    for gene in genes_to_find
                ))]
            else:
                # Single gene — support wildcard or fall back to exact match
                gq_upper = genequery.upper()
                wc_regex = wildcard_to_regex(gq_upper)
                if wc_regex:
                    compiled_gene = re.compile(wc_regex, re.IGNORECASE)
                    df = df[df['All_genes'].apply(lambda x: any(
                        compiled_gene.match(gene.replace("'", "").strip().upper()) for gene in x
                    ))]
                else:
                    df = df[df['All_genes'].apply(lambda x: gq_upper in [gene.replace("'", "").strip().upper() for gene in x])]

        if classquery:
            # Split multiple classifications (joined by |) and build OR pattern
            class_queries = [cq.strip() for cq in classquery.split('|') if cq.strip()]
            regex_patterns = []
            
            for cq in class_queries:
                cq_upper = cq.upper()
                # Special case: if searching for "LINEAR AMPLIFICATION", also match just "Linear"
                if cq_upper == "LINEAR AMPLIFICATION":
                    regex_patterns.append('LINEAR AMPLIFICATION|LINEAR')
                # Special case: if searching for "COMPLEX NON-CYCLIC", match with any character (or none) between words
                elif cq_upper == "COMPLEX NON-CYCLIC":
                    regex_patterns.append(r'COMPLEX.?NON.?CYCLIC')
                else:
                    # Escape special regex characters for literal matching
                    regex_patterns.append(re.escape(cq))
            
            # Combine all patterns with OR logic
            if regex_patterns and 'Classification' in df.columns:
                combined_pattern = '|'.join(regex_patterns)
                df = df[df['Classification'].str.contains(combined_pattern, case=False, na=False, regex=True)]

        if metadata_sample_name and 'Sample_name' in df.columns:
            wc_regex = wildcard_to_regex(metadata_sample_name)
            if wc_regex:
                df = df[df['Sample_name'].str.contains(wc_regex, case=False, na=False, regex=True)]
            else:
                df = df[df['Sample_name'].str.contains(metadata_sample_name, case=False, na=False)]

        if metadata_sample_type and 'Sample_type' in df.columns:
            df = df[df['Sample_type'].str.contains(metadata_sample_type, case=False, na=False)]

        # Combined search for Cancer Type or Tissue
        if metadata_cancer_type:
            # Create a mask for Cancer_type matches (if column exists)
            cancer_mask = pd.Series(False, index=df.index)
            if 'Cancer_type' in df.columns:
                cancer_mask = df['Cancer_type'].str.contains(metadata_cancer_type, case=False, na=False)

            # Create a mask for Tissue_of_origin matches
            tissue_mask = pd.Series(False, index=df.index)
            if 'Tissue_of_origin' in df.columns:
                tissue_mask = df['Tissue_of_origin'].str.contains(metadata_cancer_type, case=False, na=False)

            # Combine both masks with OR logic
            combined_mask = cancer_mask | tissue_mask
            df = df[combined_mask]

        # The original tissue_origin filter is not needed since we combined it above
        # Only keep this if you need backward compatibility with existing code
        if metadata_tissue_origin and 'Tissue_of_origin' in df.columns:
            df = df[df['Tissue_of_origin'].str.contains(metadata_tissue_origin, case=False, na=False)]

        if extra_metadata and ('extra_metadata_from_csv' in df.columns):
            for key in extra_metadata_from_csv.keys():
                if key != 'sample_name' and key != 'sample_type' and key != 'tissue_of_origin' and key != "cancer_type":
                    query = df[df[key].str.contains(extra_metadata, case=False, na=False)]
                    if len(query) > 0:
                        df = query

        for _, row in df.iterrows():
            sample_dict = row.to_dict()
            sample_dict['project_name'] = project_name
            sample_dict['project_linkid'] = project_linkid
            # Only process All_genes if it exists in the row
            if 'All_genes' in sample_dict and sample_dict['All_genes'] is not None:
                sample_dict['All_genes'] = [i.replace("'", "").strip() for i in sample_dict['All_genes']]

            sample_data.append(sample_dict)

    return sample_data

