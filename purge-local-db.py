from pymongo import MongoClient

def get_db_handle(db_name, host):
    client = MongoClient(host
                        )
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]

# Set up database connection
db_handle, mongo_client = get_db_handle('caper', 'mongodb://localhost:27017')
collection_handle = get_collection_handle(db_handle,'projects')
fs_files = get_collection_handle(db_handle,'fs.files')
fs_chunks = get_collection_handle(db_handle,'fs.chunks')

# Delete all projects
collection_handle.drop()

# Delete all files
fs_files.drop()
fs_chunks.drop()

