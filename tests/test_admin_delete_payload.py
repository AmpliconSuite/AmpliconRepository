"""The admin hard-delete path must remove the whole payload.

`admin_permanent_delete_project()` used to walk `project['runs']` one level deep
against eight hardcoded field names. `GRIDFS_FILE_KEYS` holds 35. Measured on
prod 2026-08-31 over the 72 SOFT_DELETED and TOMBSTONE documents an admin would
actually be deleting:

    ids the canonical walk finds : 133,655
    ids the old admin walk found :  17,175
    ids it would have stranded   : 116,480   (42.04 GiB of 66.29 GiB)

Almost all of the gap was spelling. Prod documents overwhelmingly carry the
underscore forms and the hardcoded list had only the space-separated ones.
"""

import os
import re

import pytest
from bson import ObjectId

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# `tarfile` is exempt: it is a top-level project field that the admin views
# legitimately read for other reasons -- a size lookup at views_admin.py:941 and
# an existence check at :1348, neither of which is a deletion key list. The
# defect this guard exists for was in the *per-sample feature* names, which have
# no business being spelled out anywhere but the canonical list.
READ_FOR_OTHER_REASONS = {'tarfile'}


def _canonical_keys():
    from caper.project_version_cleanup import GRIDFS_FILE_KEYS
    return set(GRIDFS_FILE_KEYS) - READ_FOR_OTHER_REASONS


@pytest.mark.integration
def test_admin_delete_uses_the_canonical_key_list():
    """No second key list anywhere in the admin views.

    The guard is on the *spelling literals*, not on the import, because the way
    this comes back is somebody re-adding a convenient short list next to the
    code that needs one.
    """
    source = open(os.path.join(REPO_ROOT, 'caper', 'caper', 'views_admin.py')).read()
    offenders = sorted(k for k in _canonical_keys() if "'%s'" % k in source
                       or '"%s"' % k in source)
    assert not offenders, (
        "views_admin.py names GridFS field(s) %r directly. Use "
        "iter_gridfs_file_ids()/delete_gridfs_payload_for_project() instead -- "
        "a second key list is what stranded 116,480 files on prod." % offenders)
    assert 'delete_gridfs_payload_for_project' in source


@pytest.mark.integration
def test_the_canonical_walk_finds_the_underscore_spellings():
    """The specific shape that was being missed, top to bottom."""
    from caper.project_version_cleanup import iter_gridfs_file_ids

    ids = {k: ObjectId() for k in (
        'Sample_metadata_JSON', 'AA_directory', 'CNV_BED_file',
        'cnvkit_directory', 'Feature_BED_file', 'AA_PNG_file', 'AA_PDF_file')}
    tarfile_id = ObjectId()
    project = {
        '_id': ObjectId(),
        'project_name': 'underscore spellings',
        'tarfile': tarfile_id,
        'runs': {'SAMPLE_A': [dict(ids, **{'Not a file': 'ignore me'})]},
    }
    found = set(iter_gridfs_file_ids(project))
    missing = {k for k, v in ids.items() if v not in found}
    assert not missing, 'canonical walk missed %r' % sorted(missing)
    assert tarfile_id in found, 'the tarfile is payload too'


@pytest.mark.integration
def test_delete_removes_every_referenced_file_including_nested_ones():
    """A recorder in place of GridFS, so this needs no database.

    Also covers nesting: the old walk only looked one level into `runs`, so a
    file held anywhere else was invisible to it.
    """
    from caper.project_version_cleanup import delete_gridfs_payload_for_project

    nested_id, run_id, tar_id = ObjectId(), ObjectId(), ObjectId()
    project = {
        '_id': ObjectId(),
        'tarfile': tar_id,
        'runs': {'S1': [{'AA_directory': run_id}]},
        'extra': {'deeper': {'Reconstruction_directory': nested_id}},
    }
    removed = []
    count = delete_gridfs_payload_for_project(removed.append, project)
    assert set(removed) == {nested_id, run_id, tar_id}
    assert count == 3


@pytest.mark.integration
def test_protected_ids_are_honoured():
    """Versions do not share ids on either database today (measured 2026-08-31,
    0 of 1.55M), but nothing enforces that, so the parameter has to work."""
    from caper.project_version_cleanup import delete_gridfs_payload_for_project

    keep, go = ObjectId(), ObjectId()
    project = {'_id': ObjectId(), 'runs': {'S': [{'AA_directory': keep,
                                                  'CNV_BED_file': go}]}}
    removed = []
    delete_gridfs_payload_for_project(removed.append, project,
                                      protected_file_ids={keep})
    assert removed == [go]


@pytest.mark.integration
def test_a_failed_delete_does_not_abandon_the_rest():
    """One unreadable file must not leave the remaining payload behind."""
    from caper.project_version_cleanup import delete_gridfs_payload_for_project

    bad, good = ObjectId(), ObjectId()
    project = {'_id': ObjectId(), 'runs': {'S': [{'AA_directory': bad,
                                                  'CNV_BED_file': good}]}}
    seen = []

    def flaky(file_id):
        seen.append(file_id)
        if file_id == bad:
            raise RuntimeError('gridfs is having a day')

    deleted = delete_gridfs_payload_for_project(flaky, project)
    assert set(seen) == {bad, good}, 'must attempt every file'
    assert deleted == 1
