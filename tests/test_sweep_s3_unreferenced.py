"""Tests for sweep_s3_unreferenced.py.

Every test here is one of the four ways §2.3b says this goes wrong. They are
tests rather than comments because the first estimate of "unreferenced" was a
good estimate and would have been a bad delete list, and the difference between
those two things is entirely in these edge cases.
"""

import datetime
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import sweep_s3_unreferenced as sweep  # noqa: E402


@pytest.mark.integration
def test_ids_are_found_anywhere_in_the_key():
    """Correction 1. The leading-segment version missed 198 real tarballs."""
    pid = '63e550ad8e4821057769102a'
    for key in ('%s/%s.tar.gz' % (pid, pid),
                'jens/dev1/%s/%s.tar.gz' % (pid, pid),
                'jens/dev1%s/%s.tar.gz' % (pid, pid),   # the missing-separator bug
                'batch_downloads/someone/%s.zip' % pid):
        assert pid in sweep.object_ids_in(key), key


@pytest.mark.integration
def test_a_key_with_no_id_is_unclassifiable_not_garbage():
    """Correction 1's other half. Not knowing must not read as 'delete it'."""
    state, oid = sweep.classify('tmp/scratch/notes.txt', referenced=set())
    assert state == 'unclassifiable'
    assert oid is None


@pytest.mark.integration
def test_a_referenced_id_anywhere_protects_the_object():
    pid = '63e550ad8e4821057769102a'
    state, _ = sweep.classify('jens/dev1/%s/%s.tar.gz' % (pid, pid), {pid})
    assert state == 'referenced'


@pytest.mark.integration
def test_case_is_not_a_way_to_smuggle_an_id_past_the_check():
    pid = '63E550AD8E4821057769102A'
    state, _ = sweep.classify('%s/%s.tar.gz' % (pid, pid), {pid.lower()})
    assert state == 'referenced'


@pytest.mark.integration
def test_hex_runs_of_the_wrong_length_are_not_object_ids():
    """23 and 25 hex characters are not ObjectIds and must not be treated as one."""
    assert sweep.object_ids_in('a' * 23 + '/x.tar.gz') == set()
    assert sweep.object_ids_in('a' * 25 + '/x.tar.gz') == set()
    assert sweep.object_ids_in('a' * 24 + '/x.tar.gz') == {'a' * 24}


@pytest.mark.integration
def test_the_id_that_names_the_object_is_the_repeated_one():
    """The key carries the project id twice and may carry others once."""
    owner = '63e550ad8e4821057769102a'
    other = '63e550ad8e4821057769ffff'
    state, oid = sweep.classify(
        '%s/%s.tar.gz.from-%s' % (owner, owner, other), referenced=set())
    assert state == 'unreferenced'
    assert oid == owner


@pytest.mark.integration
def test_object_ids_date_themselves():
    """The one provenance axis that survives on this bucket."""
    # 2023-02-09T22:00:13Z
    assert sweep.created_at('63e550ad8e4821057769102a').year == 2023
    assert sweep.created_at('63e550ad8e4821057769102a') < datetime.datetime(
        2025, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.mark.integration
def test_a_missing_database_is_refused_not_assumed_empty(tmp_path, capsys):
    """Correction 2. This is the failure that nearly cost 80,170 GridFS files
    in a different script: a key list one database short, believed complete."""
    ids = tmp_path / 'one.ids'
    ids.write_text('63e550ad8e4821057769102a\n')
    rc = sweep.main.__wrapped__ if hasattr(sweep.main, '__wrapped__') else None
    argv = sys.argv
    sys.argv = ['sweep', '--ids-file', str(ids)]   # only one, default requires 2
    try:
        assert sweep.main() == 2
    finally:
        sys.argv = argv
    assert 'refusing to run' in capsys.readouterr().out


@pytest.mark.integration
def test_ids_file_ignores_anything_that_is_not_an_object_id(tmp_path):
    p = tmp_path / 'x.ids'
    p.write_text('63e550ad8e4821057769102a\n# a comment\n\nnot-an-id\n'
                 '63E550AD8E4821057769102B\n')
    ids, per_file = sweep.load_ids([str(p)])
    assert ids == {'63e550ad8e4821057769102a', '63e550ad8e4821057769102b'}
    assert per_file[str(p)] == 2
