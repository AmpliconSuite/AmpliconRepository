"""
A feature file the aggregator reports as "Not Provided" is stored as that
placeholder without an upload attempt or a warning.

The ingestion loop used to open ``results/Not Provided`` as a path, fail, and
log "GridFS upload failed" for it.  A Replace of a 333-feature project whose
run produced no cycles images logged 666 of these on dev (2026-10-09), every
one for a file that never existed, which buries the warnings that matter.
"""

import io
import json
import logging
import tarfile

from bson.objectid import ObjectId

from caper import views


def _add_file(tar, name, body):
    info = tarfile.TarInfo(name)
    info.size = len(body)
    tar.addfile(info, io.BytesIO(body))


def _run_extraction(tmp_path, monkeypatch, feature):
    tar_path = str(tmp_path / 'project.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tar:
        _add_file(tar, 'results/run.json',
                  json.dumps({'runs': {'sample_1': [feature]}}).encode())
        _add_file(tar, 'results/s1/cnv.bed', b'chr1\t1\t2\n')
        _add_file(tar, 'results/s1/aa.tar.gz', b'not really a tarball')

    uploaded = []
    parsed = {}

    # The loop rewrites the features parsed from run.json, not the dict
    # handed in here, so keep hold of those.
    real_samples_to_dict = views.samples_to_dict

    def keep_runs(fh):
        parsed.update(real_samples_to_dict(fh))
        return parsed

    monkeypatch.setattr(views, 'samples_to_dict', keep_runs)

    def fake_put(fs, fileobj, **kwargs):
        uploaded.append(kwargs['feature_key'])
        return ObjectId()

    monkeypatch.setattr(views, 'put_with_backlink', fake_put)
    # The fake project id fails the document update after the upload loop,
    # and the error path discards what was uploaded; there is nothing real
    # to delete.
    monkeypatch.setattr(views, 'discard_unrecorded_gridfs_files',
                        lambda delete, ids: len(ids))

    dest = tmp_path / 'dest'
    dest.mkdir()
    views.extract_project_files(tarfile, tar_path, str(dest),
                                'NOT_A_REAL_OBJECT_ID', None, None, [])
    return uploaded, parsed['sample_1'][0]


def test_placeholder_files_are_not_uploaded_or_warned_about(tmp_path, monkeypatch, caplog):
    feature = {
        'Feature ID': 'sample_1_amplicon1_ecDNA_1',
        'Oncogenes': [],
        'CNV BED file': 's1/cnv.bed',
        'AA directory': 's1/aa.tar.gz',
        'Cycles PNG file': 'Not Provided',
        'Cycles PDF file': 'Not Provided',
        'Graph PNG file': '',
        'Graph PDF file': None,
    }
    with caplog.at_level(logging.WARNING):
        uploaded, stored = _run_extraction(tmp_path, monkeypatch, feature)

    assert sorted(uploaded) == ['AA directory', 'CNV BED file']
    assert 'GridFS upload failed' not in caplog.text
    assert stored['Cycles PNG file'] == 'Not Provided'
    assert stored['Cycles PDF file'] == 'Not Provided'
    assert stored['Graph PNG file'] == 'Not Provided'
    assert stored['Graph PDF file'] == 'Not Provided'
    assert isinstance(stored['CNV BED file'], ObjectId)


def test_a_named_file_that_is_missing_still_warns(tmp_path, monkeypatch, caplog):
    feature = {
        'Feature ID': 'sample_1_amplicon1_ecDNA_1',
        'Oncogenes': [],
        'CNV BED file': 's1/cnv.bed',
        'AA directory': 's1/aa.tar.gz',
        'Cycles PNG file': 's1/missing.png',
    }
    with caplog.at_level(logging.WARNING):
        _uploaded, stored = _run_extraction(tmp_path, monkeypatch, feature)

    assert "GridFS upload failed for 'Cycles PNG file'" in caplog.text
    assert stored['Cycles PNG file'] == 'Not Provided'
