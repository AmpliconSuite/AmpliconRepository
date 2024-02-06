

from bson.objectid import ObjectId
from .utils import get_collection_handle, db_handle

user_preferences_handle = get_collection_handle(db_handle,'user_preferences')

def get_user_preferences(user):
    latest = user_preferences_handle.find_one({'email': user.email})
    return latest

def update_user_preferences(user, prefs_dict):
    old_prefs = get_user_preferences(user)
    prefs_dict['email'] = user.email
    if (old_prefs == None):
        user_preferences_handle.insert_one(prefs_dict)
    else:
        query = {'_id': ObjectId(old_prefs['_id'])}
        new_val = {"$set":  prefs_dict}
        res = user_preferences_handle.update_one(query, new_val)
        print (f"Updated { res } --  {  prefs_dict }")
