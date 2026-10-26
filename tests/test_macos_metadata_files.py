"""
macOS AppleDouble sidecars must not be read as the files they sit beside.

An archive made on a Mac can carry ``._<name>`` next to each real file (or
under ``__MACOSX/`` in a zip). The sidecar keeps the real file's suffix, so a
lookup by name matched it: the CCLE archive on dev has
``._ccle_hg38_ecDNA_context_calls.tsv``, and its first page view logged a UTF-8
decode ERROR while reading it (2026-10-26).
"""

import io
import tarfile

import pytest


@pytest.mark.parametrize('path, expected', [
    ('results/AA_outputs/x/._ccle_hg38_ecDNA_context_calls.tsv', True),
    ('__MACOSX/results/ccle_hg38_ecDNA_context_calls.tsv', True),
    ('._run.json', True),
    ('results/AA_outputs/x/ccle_hg38_ecDNA_context_calls.tsv', False),
    ('results/.hidden_but_real.tsv', False),
    ('results/run.json', False),
])
def test_is_macos_metadata_file(path, expected):
    from caper.utils import is_macos_metadata_file
    assert is_macos_metadata_file(path) is expected


def _tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.integration
def test_ecDNA_context_skips_the_appledouble_sidecar(mongo_collection, caplog):
    from caper.utils import fs_handle, initialize_ecDNA_context
    real = b'S1_amplicon1_ecDNA_1\tsimple\n'
    # What a sidecar holds: an AppleDouble header and xattrs, not UTF-8 text.
    sidecar = b'\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \xc2\x28\xff'
    tar_id = fs_handle.put(_tar_bytes({
        'results/AA_outputs/S1/S1_ecDNA_context_calls.tsv': real,
        'results/AA_outputs/S1/._S1_ecDNA_context_calls.tsv': sidecar,
    }))
    pid = mongo_collection.insert_one({'project_name': 'MacosSidecarTest', 'private': 'private',
                                       'delete': False, 'current': True, 'tarfile': tar_id}).inserted_id
    try:
        with caplog.at_level('ERROR'):
            initialize_ecDNA_context(mongo_collection.find_one({'_id': pid}))
        stored = mongo_collection.find_one({'_id': pid})['ecDNA_context']
        assert stored == {'S1_amplicon1_ecDNA_1': 'simple'}
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
    finally:
        mongo_collection.delete_one({'_id': pid})
        fs_handle.delete(tar_id)
