from pymongo import MongoClient
from .utils import *

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

    # Gene Search
    if user.is_authenticated:
        username = user.username
        useremail = user.email
        query_obj = {'private': True, "$or": [{"project_members": username}, {"project_members": useremail}], 'delete': False}

        if project_name:
            query_obj['project_name'] = {'$regex': project_name, '$options': 'i'}

        private_projects = list(collection_handle.find(query_obj))
    else:
        private_projects = []

    public_query = {'private': False, 'delete': False}
    
    if project_name:
        public_query['project_name'] = {'$regex': project_name, '$options': 'i'}

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

        if genequery:
            df = df[df['All_genes'].apply(lambda x: genequery in [gene.replace("'", "") for gene in x])]

        if classquery:
            df = df[df['Classification'].str.contains(classquery, case=False, na=False)]

        if metadata_sample_name:
            df = df[df['Sample_name'].str.contains(metadata_sample_name, case=False, na=False)]

        if metadata_sample_type:
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
        if metadata_tissue_origin:
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
            sample_dict['All_genes'] = [i.replace("'", "").strip() for i in sample_dict['All_genes']]
            sample_data.append(sample_dict)

    return sample_data

