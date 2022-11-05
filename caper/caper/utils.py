from pymongo import MongoClient

def get_db_handle(db_name, host):
    client = MongoClient(host
                        )
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]

def create_run_display(project):
    runs = project['runs']
    run_list = []
    for run in runs:
        for sample in runs[run]:
            for key in list(sample.keys()):
                newkey = key.replace(" ", "_")
                sample[newkey] = sample.pop(key)
            run_list.append(sample)
    return run_list