from bson import ObjectId

from cleanup_orphaned_projects import collect_protected_ids, delete_gridfs_files_for_project


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                yield {
                    key: doc[key]
                    for key in projection
                    if key in doc
                }


class FakeGridFS:
    def __init__(self):
        self.deleted = []

    def delete(self, object_id):
        self.deleted.append(str(object_id))


def test_collect_protected_ids_accepts_legacy_string_previous_versions():
    current_id = ObjectId()
    dict_prev_id = ObjectId()
    string_prev_id = ObjectId()
    soft_deleted_id = ObjectId()

    collection = FakeCollection([
        {
            '_id': current_id,
            'delete': False,
            'previous_versions': [
                {'linkid': dict_prev_id},
                str(string_prev_id),
            ],
        },
        {
            '_id': soft_deleted_id,
            'delete': True,
            'current': True,
            'previous_versions': [],
        },
    ])

    assert collect_protected_ids(collection) == {
        str(current_id),
        str(dict_prev_id),
        str(string_prev_id),
        str(soft_deleted_id),
    }


def test_delete_gridfs_files_for_project_handles_current_underscore_keys():
    tar_id = ObjectId()
    png_id = ObjectId()
    graph_id = ObjectId()
    fs = FakeGridFS()
    project = {
        'tarfile': tar_id,
        'runs': {
            'sample1': [{
                'AA_PNG_file': png_id,
                'AA_graph_file': graph_id,
            }],
        },
    }

    assert delete_gridfs_files_for_project(fs, project) == 3
    assert fs.deleted == [str(tar_id), str(png_id), str(graph_id)]
