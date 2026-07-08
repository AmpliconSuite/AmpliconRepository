from bson import ObjectId

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'purge-local-db.py'
SPEC = importlib.util.spec_from_file_location('purge_local_db', MODULE_PATH)
purge_local_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(purge_local_db)


class FakeCursorCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        for doc in self.docs:
            if any(doc.get(key) != value for key, value in query.items()):
                continue
            if not projection:
                yield doc
                continue
            yield {
                key: doc[key]
                for key, included in projection.items()
                if included and key in doc
            }


class FakeDb:
    def __init__(self, projects, fs_files):
        self.collections = {
            'projects': FakeCursorCollection(projects),
            'fs.files': FakeCursorCollection(fs_files),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_collect_object_id_strings_recurses_through_project_documents():
    direct = ObjectId()
    string_id = ObjectId()
    nested = ObjectId()

    assert purge_local_db.collect_object_id_strings({
        '_id': direct,
        'tarfile': str(string_id),
        'runs': {
            'run1': [
                {'AA graph file': nested},
                {'notes': 'not an object id'},
            ],
        },
    }) == {str(direct), str(string_id), str(nested)}


def test_find_unreferenced_gridfs_files_ignores_project_referenced_files():
    referenced = ObjectId()
    unreferenced = ObjectId()
    db = FakeDb(
        projects=[],
        fs_files=[
            {'_id': referenced, 'length': 100, 'filename': 'kept'},
            {'_id': unreferenced, 'length': 200, 'filename': 'purged'},
        ],
    )

    assert purge_local_db.find_unreferenced_gridfs_files(db, {str(referenced)}) == [
        {'_id': unreferenced, 'length': 200, 'filename': 'purged'},
    ]


def test_reachable_scope_does_not_protect_deleted_non_current_projects():
    active_file = ObjectId()
    deleted_file = ObjectId()
    active_project = {
        '_id': ObjectId(),
        'current': True,
        'delete': False,
        'tarfile': active_file,
        'previous_versions': [],
    }
    deleted_project = {
        '_id': ObjectId(),
        'current': False,
        'delete': True,
        'tarfile': deleted_file,
        'previous_versions': [],
    }
    collection = FakeCursorCollection([active_project, deleted_project])

    assert purge_local_db.collect_project_referenced_ids(
        collection,
        scope='reachable',
        strategy='app-fields',
    ) == {str(active_file)}


def test_app_fields_strategy_does_not_protect_unrecognized_object_id_fields():
    protected_file = ObjectId()
    protected_png = ObjectId()
    stale_id = ObjectId()
    project = {
        '_id': ObjectId(),
        'current': True,
        'delete': False,
        'tarfile': protected_file,
        'runs': {'run1': [{'AA_PNG_file': protected_png}]},
        'legacy_unused_file': stale_id,
        'previous_versions': [],
    }
    collection = FakeCursorCollection([project])

    recursive = purge_local_db.collect_project_referenced_ids(
        collection,
        scope='reachable',
        strategy='recursive',
    )
    app_fields = purge_local_db.collect_project_referenced_ids(
        collection,
        scope='reachable',
        strategy='app-fields',
    )

    assert str(stale_id) in recursive
    assert app_fields == {str(protected_file), str(protected_png)}


def test_collect_gridfs_references_by_path_records_reference_paths():
    tar_id = ObjectId()
    png_id = ObjectId()
    refs = purge_local_db.collect_gridfs_references_by_path({
        'tarfile': tar_id,
        'runs': {'run1': [{'AA_PNG_file': png_id}]},
    })

    assert (str(tar_id), 'tarfile') in refs
    assert (str(png_id), 'runs.run1[].AA_PNG_file') in refs
    assert purge_local_db.reference_bucket('tarfile') == 'project tarfiles'
    assert purge_local_db.reference_bucket('runs.run1[].AA_PNG_file') == 'feature files: AA_PNG_file'


def test_report_tarfile_references_handles_existing_missing_and_absent_tarfiles(capsys):
    existing_tar = ObjectId()
    missing_tar = ObjectId()
    active_project_id = ObjectId()
    db = FakeDb(
        projects=[
            {
                '_id': active_project_id,
                'current': True,
                'delete': False,
                'project_name': 'active',
                'tarfile': existing_tar,
                'previous_versions': [],
            },
            {
                '_id': ObjectId(),
                'current': True,
                'delete': False,
                'project_name': 'missing',
                'tarfile': missing_tar,
                'previous_versions': [],
            },
            {
                '_id': ObjectId(),
                'current': True,
                'delete': False,
                'project_name': 'empty',
                'previous_versions': [],
            },
        ],
        fs_files=[
            {'_id': existing_tar, 'length': 1024, 'filename': 'active.tar.gz'},
        ],
    )

    purge_local_db.report_tarfile_references(db)
    out = capsys.readouterr().out

    assert 'reachable: 1 tarfiles' in out
    assert 'Missing tarfile references: 1' in out
    assert 'Projects without tarfile field/value: 1' in out
    assert str(missing_tar) in out
