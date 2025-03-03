from pymongo import MongoClient
from .utils import *
def perform_search(genequery=None, project_name=None, classquery=None, metadata=None, user=None):

    gen_query = {'$regex': genequery }
    # Gene Search
    if user.is_authenticated:
        username = user.username
        useremail = user.email
        if project_name:
            query_obj = {'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}] , 'Oncogenes' : gen_query, 'delete': False, 'project_name' : {'$regex' : project_name, '$options' : 'i'}}
        else:
            query_obj = {'private' : True, "$or": [{"project_members": username}, {"project_members": useremail}] , 'Oncogenes' : gen_query, 'delete': False}

        private_projects = list(collection_handle.find(query_obj))
    else:
        private_projects = []

    if project_name:
        public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'delete': False, 'project_name' : {'$regex' : project_name, '$options' : 'i'}}))
    else:
        public_projects = list(collection_handle.find({'private' : False, 'Oncogenes' : gen_query, 'delete': False}))

    for proj in private_projects:
        prepare_project_linkid(proj)    
    for proj in public_projects:
        prepare_project_linkid(proj)

    # def collect_class_data(projects):
    #     """
    #     Collects data based on the user queries that were given. 
    #     """
    #     sample_data = []
    #     for project in projects:
    #         project_name = project['project_name']
    #         project_linkid = project['_id']
    #         features = project['runs']
    #         features_list = replace_space_to_underscore(features)
    #         data = sample_data_from_feature_list(features_list)
            
    #         for sample in data:
    #             match_found = True  # Assume match unless proven otherwise

    #             # Ensure genequery exists in Oncogenes
    #             if genequery and genequery not in sample.get('Oncogenes', []):
    #                 match_found = False

    #             # Ensure classquery exists in Classifications
    #             if classquery:
    #                 upperclass = list(map(str.upper, sample.get('Classifications', [])))
    #                 if classquery.upper() not in upperclass:
    #                     match_found = False

    #             # Ensure metadata exists in sample
    #             if metadata:
    #                 metadata_values = [val.lower() for val in sample.values() if isinstance(val, str)]
    #                 if metadata.lower() not in metadata_values:
    #                     match_found = False

    #             if match_found:
    #                 sample['project_name'] = project_name
    #                 sample['project_linkid'] = project_linkid
    #                 sample_data.append(sample)
            

    #     return sample_data


    # Collect sample data
    public_sample_data = get_samples_from_features(public_projects, genequery=genequery, classquery=classquery, metadata=metadata)
    private_sample_data = get_samples_from_features(private_projects, genequery=genequery, classquery=classquery, metadata=metadata)

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
    fields_to_search_in = ['Sample_type','Tissue_of_origin']
    for sample in sample_data:
        for field in fields_to_search_in:
            if metadata_to_find.lower() == sample[field].lower():
                samples_to_return.append(sample)
        if metadata_to_find.lower() in [val.lower() for val in sample['extra_metadata_from_csv'].values()]:
            samples_to_return.append(sample)
    return samples_to_return

def get_samples_from_features(projects, genequery, classquery, metadata):
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
        cols = ['Sample_name', 'Oncogenes', 'Classification', 'Feature_ID', 'Sample_type', 'Tissue_of_origin', 'extra_metadata_from_csv']
        df = df[[col for col in cols if col in df.columns]]

        if genequery:
            df = df[df['Oncogenes'].apply(lambda x: genequery in [oncogene.replace("'", "") for oncogene in x])]

        if classquery:
            df = df[df['Classification'].str.contains(classquery, case=False, na=False)]

        if metadata:
            df = df[
                df['Sample_name'].str.contains(metadata, case=False, na=False) |
                df['Sample_type'].str.contains(metadata, case=False, na=False) |
                df['Tissue_of_origin'].str.contains(metadata, case=False, na=False)
            ]
            
        for _, row in df.iterrows():
            sample_dict = row.to_dict()
            sample_dict['project_name'] = project_name
            sample_dict['project_linkid'] = project_linkid
            sample_dict['Oncogenes'] = [i.replace("'", "").strip() for i in sample_dict['Oncogenes']]
            sample_data.append(sample_dict)

    return sample_data