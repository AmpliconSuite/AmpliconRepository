"""A project that would not fit in one database document is refused before anything is stored."""

import io
import json
import tarfile
from unittest.mock import patch

import bson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from caper.project_record_size import (
    LIMIT_BYTES,
    ProjectRecordTooLarge,
    refuse_new_version_over_limit,
    size_after_metadata_edit,
    size_of_new_version,
    too_large_message,
)


def _rows(name, count=2):
    return [{'Sample_name': name, 'Feature_ID': f'{name}_{i}', 'Classification': 'ecDNA',
             'All_genes': ['EGFR'], 'Oncogenes': ['EGFR']} for i in range(count)]


def _project(samples=3):
    return {'project_name': 'P', 'runs': {f'sample_{i}': _rows(f'S{i}') for i in range(samples)}}


def _sheet(cell_bytes, samples=3, name='meta.tsv'):
    body = 'sample_name\tnote\n' + ''.join(f'S{i}\t{"x" * cell_bytes}\n' for i in range(samples))
    return SimpleUploadedFile(name, body.encode())


def _archive(tmp_path, runs):
    path = tmp_path / 'agg.tar.gz'
    payload = json.dumps({'runs': runs}).encode()
    with tarfile.open(path, 'w:gz') as tar:
        info = tarfile.TarInfo('results/run.json')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return str(path)


def test_a_sheet_counts_towards_the_record():
    small = size_after_metadata_edit(_project(), _sheet(10))
    large = size_after_metadata_edit(_project(), _sheet(10_000))
    assert large - small >= 3 * (10_000 - 10)


def test_sizing_a_sheet_leaves_the_project_and_the_file_as_they_were():
    project = _project()
    sheet = _sheet(10)
    size_after_metadata_edit(project, sheet)
    assert 'extra_metadata_from_csv' not in project['runs']['sample_0'][0]
    assert sheet.read().startswith(b'sample_name')


def test_sample_data_still_to_be_written_is_counted():
    project = _project()
    without = size_after_metadata_edit(project, _sheet(10))
    stored = dict(project, sample_data=[{'Sample_name': 'S0'}])
    assert without > size_after_metadata_edit(stored, _sheet(10))


def test_an_unreadable_sheet_raises_and_still_rewinds():
    sheet = SimpleUploadedFile('meta.pdf', b'not a sheet')
    with pytest.raises(ValueError):
        size_after_metadata_edit(_project(), sheet)
    assert sheet.read() == b'not a sheet'


def test_the_message_says_how_big_and_what_to_do():
    text = too_large_message(28_300_000, True)
    assert '28.3 MB' in text and 'Remove metadata columns' in text and 'Nothing was changed' in text
    assert 'Remove metadata' not in too_large_message(28_300_000, False)


def test_new_version_size_swaps_file_paths_for_ids(tmp_path):
    runs = {'s1': [dict(_rows('S1', 1)[0], **{'CNV BED file': 'samples/' + 'p' * 5000 + '/a.bed'})]}
    assert size_of_new_version(_archive(tmp_path, runs)) < 5000


def test_stored_rows_hold_ids_and_the_older_names():
    from caper.project_record_size import _row_as_stored
    stored = _row_as_stored({'Graph_PNG_file': 'samples/a/a.png', 'CNV_BED_file': 'Not Provided',
                             'Cancer_type': 'Bone/Soft tissue'})
    assert isinstance(stored['Graph_PNG_file'], bson.ObjectId)
    assert stored['AA_PNG_file'] == stored['Graph_PNG_file']
    assert stored['CNV_BED_file'] == 'Not Provided'
    assert stored['Cancer_type'] == 'Bone/Soft tissue'


def test_new_version_size_counts_a_new_sheet(tmp_path):
    archive = _archive(tmp_path, {'s1': _rows('S1')})
    sheet = tmp_path / 'meta.tsv'
    sheet.write_text('sample_name\tnote\nS1\t' + 'x' * 20_000 + '\n')
    assert size_of_new_version(archive, str(sheet)) >= size_of_new_version(archive) + 20_000


def test_new_version_size_counts_retained_metadata(tmp_path):
    archive = _archive(tmp_path, {'s1': _rows('S1')})
    retained = {'S1': {'note': 'x' * 20_000}}
    assert (size_of_new_version(archive, old_extra_metadata=retained)
            >= size_of_new_version(archive) + 20_000)


def test_an_over_limit_version_is_refused(tmp_path):
    archive = _archive(tmp_path, {'s1': _rows('S1')})
    sheet = tmp_path / 'meta.tsv'
    sheet.write_text('sample_name\tnote\nS1\t' + 'x' * (LIMIT_BYTES + 1) + '\n')
    with pytest.raises(ProjectRecordTooLarge, match='Remove metadata columns'):
        refuse_new_version_over_limit(archive, str(sheet))


def test_a_version_that_fits_is_not_refused(tmp_path):
    refuse_new_version_over_limit(_archive(tmp_path, {'s1': _rows('S1')}))


def test_not_being_able_to_size_it_does_not_stop_the_upload(tmp_path):
    missing = str(tmp_path / 'no-such.tar.gz')
    refuse_new_version_over_limit(missing)


def test_an_archive_without_run_json_is_left_to_ingestion(tmp_path):
    path = tmp_path / 'agg.tar.gz'
    with tarfile.open(path, 'w:gz'):
        pass
    assert size_of_new_version(str(path)) is None


def test_edit_page_refuses_an_oversized_sheet_without_writing(request_factory, test_user, mongo_collection):
    from bson import ObjectId
    from caper.views import edit_project_page

    doc = {'project_name': 'RecordLimitTest', 'creator': test_user.username, 'private': 'private',
           'delete': False, 'current': True, 'FINISHED?': True, 'previous_versions': [],
           'project_members': [test_user.username, test_user.email], 'alias_name': 'record-limit-alias',
           'description': 'd', 'publication_link': '', 'runs': {'s1': _rows('S1')}, 'date': '2026-11-05'}
    oid = mongo_collection.insert_one(doc).inserted_id
    mongo_collection.update_one({'_id': oid}, {'$set': {'linkid': str(oid)}})
    sheet = SimpleUploadedFile(
        'meta.tsv', ('sample_name\tnote\nS1\t' + 'x' * (LIMIT_BYTES + 1) + '\n').encode())
    try:
        request = request_factory.post(f'/project/{oid}/edit', data={
            'project_name': 'RecordLimitTest', 'description': 'd', 'private': 'private',
            'publication_link': '', 'project_members': '', 'alias': '', 'accept_license': 'on',
            'metadataFile': sheet})
        request.user = test_user
        with patch('caper.views.get_one_project', return_value=mongo_collection.find_one({'_id': oid})):
            response = edit_project_page(request, project_name=str(oid))
        assert response.status_code == 400
        assert b'Remove metadata columns' in response.content
        after = mongo_collection.find_one({'_id': oid})
        assert after['alias_name'] == 'record-limit-alias'
        assert 'extra_metadata_from_csv' not in after['runs']['s1'][0]
    finally:
        mongo_collection.delete_one({'_id': ObjectId(oid)})
