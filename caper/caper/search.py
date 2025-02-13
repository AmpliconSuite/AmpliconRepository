from pymongo import MongoClient
from .utils import *
def perform_search(gene_search=None, project_name=None, classifications=None, sample_name=None, metadata=None, user=None):
    query = {"delete": False}  # Exclude deleted projects

    if project_name:
        query["project_name"] = {"$regex": project_name, "$options": "i"}
    if gene_search:
        query["sample_data.Oncogenes"] = {"$regex": gene_search, "$options": "i"}
    if classifications:
        query["sample_data.Classifications"] = {"$regex": classifications, "$options": "i"}
    if sample_name:
        query["sample_data.Sample_name"] = {"$regex": sample_name, "$options": "i"}
    if metadata:
        query["sample_data.extra_metadata_from_csv"] = {"$regex": metadata, "$options": "i"}

    print('query:', query)
    print('gene_search:', gene_search)
    # **Retrieve Public Projects**
    public_projects = list(collection_handle.find(
        {**query, "private": False},
        {"_id": 1, "project_name": 1, "description": 1, "date": 1, "project_members": 1, "sample_data": 1}
    ))

    # **Retrieve Private Projects (only for authenticated users)**
    private_projects = []
    if user and user.is_authenticated:
        private_query = {
            **query,
            "private": True,
            "$or": [{"project_members": user.username}, {"project_members": user.email}]
        }
        private_projects = list(collection_handle.find(
            private_query,
            {"_id": 1, "project_name": 1, "description": 1, "date": 1, "project_members": 1, "sample_data": 1}
        ))

    # **Ensure `linkid` Exists**
    def add_linkid(projects):
        for project in projects:
            project["linkid"] = str(project["_id"])  # Use MongoDB `_id` as `linkid`
            if "project_name" not in project or not project["project_name"]:
                project["project_name"] = "Unknown Project"  # Prevent empty project names
        return projects

    public_projects = add_linkid(public_projects)
    private_projects = add_linkid(private_projects)

    # **Extract Sample Data for Display**
    def collect_sample_data(projects):
        sample_data = []
        for project in projects:
            for sample in project.get("sample_data", []):
                sample_data.append({
                    "project_name": project["project_name"],
                    "project_linkid": project["linkid"],  # Now guaranteed to exist
                    "Sample_name": sample["Sample_name"],
                    "Features": sample.get("Features", 0),
                    "Oncogenes": sample["Oncogenes"],
                    "Classifications": sample["Classifications"]
                })
        return sample_data

    public_sample_data = collect_sample_data(public_projects)
    private_sample_data = collect_sample_data(private_projects)

    return {
        "public_projects": public_projects,
        "private_projects": private_projects,
        "public_sample_data": public_sample_data,
        "private_sample_data": private_sample_data
    }