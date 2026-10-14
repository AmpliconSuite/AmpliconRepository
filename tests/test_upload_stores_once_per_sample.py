"""
An upload stores each file once per sample, not once per feature.

run.json has one row per feature and repeats the sample's columns on every row,
so the sample-level files (CNV BED, metadata JSONs, the reconstruction
directory) are named once per feature. Until this was fixed every row stored
its own copy: measured 2026-10-14, 137.4 of 185.5 GiB of prod's
document-named GridFS bytes were those repeat copies.

Every feature must still name an id for every key it names today, because the
readers take a sample's first row or the id in a URL.
"""

import json
import logging
import tarfile

import pytest
from bson.objectid import ObjectId

import caper.views as views

SAMPLE_KEYS = ('CNV BED file', 'Sample metadata JSON', 'Run metadata JSON')


def _feature(sample, n, amplicon):
    return {
        'Sample name': sample,
        'Feature ID': f'{sample}_amplicon{amplicon}_ecDNA_{n}',
        'Classification': 'ecDNA',
        'Oncogenes': [],
        'Feature BED file': f'{sample}/feature_{n}.bed',
        'AA PNG file': f'{sample}/amplicon{amplicon}.png',
        'CNV BED file': f'{sample}/cnv.bed',
        'Sample metadata JSON': f'{sample}/sample_meta.json',
        'Run metadata JSON': f'{sample}/run_meta.json',
        'Reconstruction directory': f'{sample}/reconstruction',
    }


def _build_tar(tmp_path):
    """Two samples of three features; features 1 and 2 share amplicon 1."""
    src = tmp_path / 'src' / 'results'
    runs = {}
    for sample in ('S1', 'S2'):
        runs[sample] = [_feature(sample, n, amplicon)
                        for n, amplicon in ((1, 1), (2, 1), (3, 2))]
        files = {path for feature in runs[sample] for key, path in feature.items()
                 if key.endswith('file') or key.endswith('JSON')}
        for path in files:
            (src / path).parent.mkdir(parents=True, exist_ok=True)
            (src / path).write_text(f'{path}\n')
        recon = src / sample / 'reconstruction'
        recon.mkdir()
        (recon / 'amplicon1_graph.txt').write_text('graph\n')
    (src / 'run.json').write_text(json.dumps({'runs': runs}))

    tar_path = tmp_path / 'upload.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(src, arcname='results')
    return str(tar_path)


@pytest.fixture
def upload(tmp_path, monkeypatch):
    """Run extract_project_files with GridFS and the database faked out.

    Returns (puts, saved, deleted, run): every put as (sample, key), the runs
    the document was saved with, every discarded id, and a callable that runs
    the upload.
    """
    puts, saved, deleted = [], {}, []
    fail_next = set()

    def fake_put(fs, data, filename=None, project_id=None, sample_name=None,
                 feature_key=None):
        if (sample_name, feature_key) in fail_next:
            fail_next.discard((sample_name, feature_key))
            raise IOError('transient GridFS error')
        data.read()
        puts.append((sample_name, feature_key))
        return ObjectId()

    class FakeCollection:
        fail_on_save = False

        def update_one(self, query, update):
            if self.fail_on_save:
                raise RuntimeError('document save failed')
            saved.update(update['$set'])

    collection = FakeCollection()
    monkeypatch.setattr(views, 'put_with_backlink', fake_put)
    monkeypatch.setattr(views, 'delete_gridfs_file', deleted.append)
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'get_one_project', lambda project_id: {})
    monkeypatch.setattr(views, 'get_tool_versions', lambda project, runs: None)
    monkeypatch.setattr(views, 'project_content_changed', lambda project_id: None)
    monkeypatch.setattr(views, 'process_metadata_no_request',
                        lambda runs, **kwargs: runs)

    def run(fail_first=(), fail_on_save=False):
        fail_next.update(fail_first)
        collection.fail_on_save = fail_on_save
        dest = tmp_path / 'dest'
        dest.mkdir(exist_ok=True)
        views.extract_project_files(tarfile, _build_tar(tmp_path), str(dest),
                                    str(ObjectId()), None, None, [])

    return puts, saved, deleted, run


def _ids(saved, sample, key):
    """The id each of a sample's features names under key, in either spelling."""
    underscored = key.replace(' ', '_')
    return [feature.get(key, feature.get(underscored)) for feature in saved['runs'][sample]]


def test_sample_level_files_are_stored_once_per_sample(upload):
    puts, saved, _deleted, run = upload
    run()

    for sample in ('S1', 'S2'):
        for key in SAMPLE_KEYS + ('Reconstruction directory',):
            assert puts.count((sample, key)) == 1, (sample, key)
            ids = _ids(saved, sample, key)
            assert len(ids) == 3 and len(set(ids)) == 1, (sample, key)
            assert isinstance(ids[0], ObjectId)
        # The legacy alias names the same stored directory.
        assert _ids(saved, sample, 'AA directory') == _ids(saved, sample, 'Reconstruction directory')


def test_amplicon_and_feature_files_are_stored_once_per_path(upload):
    puts, saved, _deleted, run = upload
    run()

    for sample in ('S1', 'S2'):
        # Features 1 and 2 belong to amplicon 1, so name the same PNG.
        assert puts.count((sample, 'AA PNG file')) == 2
        png = _ids(saved, sample, 'AA PNG file')
        assert png[0] == png[1] != png[2]
        # Every feature has its own BED.
        assert puts.count((sample, 'Feature BED file')) == 3
        assert len(set(_ids(saved, sample, 'Feature BED file'))) == 3


def test_samples_never_share_an_id(upload):
    _puts, saved, _deleted, run = upload
    run()

    for key in SAMPLE_KEYS:
        assert set(_ids(saved, 'S1', key)).isdisjoint(_ids(saved, 'S2', key))


def test_a_failed_put_is_retried_by_the_next_feature(upload):
    """Only successes are cached, so one transient error cannot blank a sample."""
    puts, saved, _deleted, run = upload
    run(fail_first={('S1', 'CNV BED file'), ('S1', 'Reconstruction directory')})

    for key in ('CNV BED file', 'Reconstruction directory'):
        ids = _ids(saved, 'S1', key)
        assert ids[0] == 'Not Provided'
        assert isinstance(ids[1], ObjectId) and ids[1] == ids[2]
        assert puts.count(('S1', key)) == 1


def test_a_failed_save_discards_each_stored_file_once(upload):
    """The failure cleanup deletes what was put, each id exactly once."""
    puts, _saved, deleted, run = upload
    run(fail_on_save=True)

    assert len(deleted) == len(puts) == len(set(deleted))


def test_the_log_line_reports_files_and_mib(upload, caplog):
    puts, _saved, _deleted, run = upload
    with caplog.at_level(logging.INFO):
        run()

    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith('Putting files in GridFS took'))
    assert f'{len(puts)} files' in line
    assert 'MiB' in line and 's/MiB' in line


def test_discard_deletes_a_repeated_id_once():
    from caper.project_version_cleanup import discard_unrecorded_gridfs_files
    deleted = []
    file_id = ObjectId()

    assert discard_unrecorded_gridfs_files(deleted.append, [file_id, file_id]) == 1
    assert deleted == [file_id]
