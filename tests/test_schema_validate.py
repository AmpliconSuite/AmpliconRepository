import copy
import json
from pathlib import Path

import pytest
from bson import ObjectId
from jsonschema import Draft7Validator

from caper import schema_validate


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / 'caper' / 'schema' / 'schema.json'


class FakeCollection:
    def __init__(self, documents):
        self.documents = {
            document['_id']: copy.deepcopy(document)
            for document in documents
        }
        self.update_calls = []

    def find(self):
        # PyMongo returns independent dictionaries; previewing repairs must not
        # mutate the stored fake documents through shared references.
        return [copy.deepcopy(document) for document in self.documents.values()]

    def update_one(self, query, update):
        self.update_calls.append((copy.deepcopy(query), copy.deepcopy(update)))
        document = self.documents[query['_id']]
        for path, value in update.get('$set', {}).items():
            _set_mongo_path(document, path, copy.deepcopy(value))


class FakeDatabase:
    name = 'schema-test'

    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, collection_name):
        assert collection_name == 'projects'
        return self.collection


class FakeMongoClient:
    def __init__(self, database):
        self.database = database

    def __getitem__(self, database_name):
        assert database_name == 'schema-test'
        return self.database


def _set_mongo_path(document, path, value):
    parts = path.split('.')
    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})
    if isinstance(current, list):
        current[int(parts[-1])] = value
    else:
        current[parts[-1]] = value


def _write_visibility_schema(tmp_path):
    schema_path = tmp_path / 'schema.json'
    schema_path.write_text(json.dumps({
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'type': 'object',
        'properties': {
            'private': {
                'type': 'string',
                'enum': ['private', 'public', 'hidden_public'],
                'default': 'private',
            },
        },
        'required': ['private'],
    }))
    return schema_path


def _run_repair(monkeypatch, tmp_path, documents, *, apply_changes):
    collection = FakeCollection(documents)
    client = FakeMongoClient(FakeDatabase(collection))
    monkeypatch.setattr(schema_validate, 'mongo_client', client)
    monkeypatch.setattr(schema_validate, 'mongo_client_primary', client)
    report = schema_validate.run_fix_schema(
        db_name='schema-test',
        collection_name='projects',
        schema_path=str(_write_visibility_schema(tmp_path)),
        apply_changes=apply_changes,
    )
    return collection, report


def test_project_schema_requires_canonical_visibility_strings():
    schema = json.loads(SCHEMA_PATH.read_text())
    visibility_schema = schema['properties']['private']
    validator = Draft7Validator(visibility_schema)

    for visibility in ('private', 'public', 'hidden_public'):
        assert list(validator.iter_errors(visibility)) == []

    for legacy_or_invalid in (True, False, 'unexpected'):
        assert list(validator.iter_errors(legacy_or_invalid))

    assert visibility_schema['default'] == 'private'


def test_project_schema_accepts_no_result_numeric_sentinels_and_gridfs_ids():
    schema = json.loads(SCHEMA_PATH.read_text())
    feature_properties = (
        schema['properties']['runs']['patternProperties']['^(sample_[0-9]+)$']
        ['items']['properties']
    )

    for field_name in (
        'AA_amplicon_number',
        'Captured_interval_length',
        'Complexity_score',
        'Feature_maximum_copy_number',
        'Feature_median_copy_number',
    ):
        validator = Draft7Validator(feature_properties[field_name])
        assert list(validator.iter_errors('NA')) == []
        assert list(validator.iter_errors('unexpected')) != []

    assert list(Draft7Validator(
        feature_properties['Run_metadata_JSON']
    ).iter_errors(ObjectId())) == []


def test_schema_defaults_are_used_before_type_fallbacks():
    assert schema_validate.get_default_value({
        'type': 'string',
        'default': 'private',
    }) == 'private'


def test_schema_repair_preview_reports_changes_without_writing(monkeypatch, tmp_path):
    private_id = ObjectId()
    public_id = ObjectId()
    missing_id = ObjectId()
    collection, report = _run_repair(
        monkeypatch,
        tmp_path,
        [
            {'_id': private_id, 'private': True, 'delete': False},
            {'_id': public_id, 'private': False, 'delete': False},
            {'_id': missing_id, 'delete': False},
        ],
        apply_changes=False,
    )

    assert collection.update_calls == []
    assert collection.documents[private_id]['private'] is True
    assert collection.documents[public_id]['private'] is False
    assert 'private' not in collection.documents[missing_id]
    assert 'DRY RUN' in report
    assert 'normalized_legacy_visibility' in report
    assert 'added_missing_required_key' in report
    assert 'Documents with repairable changes: 3' in report
    assert 'Updated documents: 0' in report


def test_schema_repair_applies_targeted_visibility_updates(monkeypatch, tmp_path):
    private_id = ObjectId()
    public_id = ObjectId()
    missing_id = ObjectId()
    hidden_id = ObjectId()
    collection, report = _run_repair(
        monkeypatch,
        tmp_path,
        [
            {'_id': private_id, 'private': True, 'delete': False},
            {'_id': public_id, 'private': False, 'delete': False},
            {'_id': missing_id, 'delete': False},
            {'_id': hidden_id, 'private': 'hidden_public', 'delete': False},
        ],
        apply_changes=True,
    )

    assert collection.documents[private_id]['private'] == 'private'
    assert collection.documents[public_id]['private'] == 'public'
    assert collection.documents[missing_id]['private'] == 'private'
    assert collection.documents[hidden_id]['private'] == 'hidden_public'
    assert len(collection.update_calls) == 3
    assert all(set(update) == {'$set'} for _, update in collection.update_calls)
    assert all(set(update['$set']) == {'private'} for _, update in collection.update_calls)
    assert 'APPLY' in report
    assert 'Updated documents: 3' in report


def test_schema_repair_preview_lists_unrepairable_validation_errors(monkeypatch, tmp_path):
    schema_path = tmp_path / 'schema.json'
    schema_path.write_text(json.dumps({
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'type': 'object',
        'properties': {'value': {'type': 'integer'}},
    }))
    document_id = ObjectId()
    collection = FakeCollection([{
        '_id': document_id,
        'project_name': 'Legacy aggregate project',
        'creator': 'legacy-owner',
        'date_created': '2023-08-09',
        'value': 'not an integer',
        'delete': False,
    }])
    client = FakeMongoClient(FakeDatabase(collection))
    monkeypatch.setattr(schema_validate, 'mongo_client_primary', client)

    report = schema_validate.run_fix_schema(
        db_name='schema-test',
        collection_name='projects',
        schema_path=str(schema_path),
        apply_changes=False,
    )

    assert f"Project: 'Legacy aggregate project'" in report
    assert f'Document ID: {document_id}' in report
    assert 'Creator: legacy-owner' in report
    assert 'Created: 2023-08-09' in report
    assert 'Remaining validation errors after proposed repairs:' in report
    assert "Path: /value" in report
    assert 'Documents requiring further review: 1' in report


@pytest.mark.parametrize(('legacy', 'canonical', 'bucket'), [
    (True, 'private', 'all_private'),
    (False, 'public', 'public'),
])
def test_visibility_normalization_preserves_site_stats_bucket(legacy, canonical, bucket):
    from caper.site_stats import (
        BUCKET_PREFIXES,
        BUCKET_QUERY_VALUES,
    )
    from caper.utils import normalize_visibility_field

    assert normalize_visibility_field(legacy) == canonical
    assert BUCKET_PREFIXES[normalize_visibility_field(legacy)] == bucket
    assert legacy in BUCKET_QUERY_VALUES[canonical]
    assert canonical in BUCKET_QUERY_VALUES[canonical]


def test_validation_reports_every_error_in_a_document(capsys):
    collection = FakeCollection([{
        '_id': ObjectId(),
        'first': 'not an integer',
        'second': 'also not an integer',
    }])
    database = FakeDatabase(collection)
    schema = {
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'type': 'object',
        'properties': {
            'first': {'type': 'integer'},
            'second': {'type': 'integer'},
        },
    }

    assert schema_validate.validate_collection(
        schema,
        db_handle=database,
        collection_name='projects',
    ) == (1, 1, 0)

    output = capsys.readouterr().out
    assert 'Path: /first' in output
    assert 'Path: /second' in output


def test_validation_summary_distinguishes_deleted_documents(capsys):
    collection = FakeCollection([
        {'_id': ObjectId(), 'value': 1, 'delete': False},
        {'_id': ObjectId(), 'value': 'ignored', 'delete': True},
    ])
    database = FakeDatabase(collection)
    schema = {
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'type': 'object',
        'properties': {'value': {'type': 'integer'}},
    }

    assert schema_validate.validate_collection(
        schema,
        db_handle=database,
        collection_name='projects',
    ) == (2, 0, 0)

    output = capsys.readouterr().out
    assert 'Documents validated: 1' in output
    assert 'Deleted documents skipped: 1' in output
    assert 'Valid documents: 1' in output


def test_admin_schema_repair_template_requires_preview_before_apply():
    template = (
        REPO_ROOT / 'caper' / 'templates' / 'pages' /
        'admin_fix_schema_report.html'
    ).read_text()

    assert 'name="action" value="apply"' in template
    assert '{% if can_apply %}' in template
