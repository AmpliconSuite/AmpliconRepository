"""Deleting a project must fit inside the web request, or stop cleanly.

Measured on prod 2026-09-01 across three admin hard deletes, a delete costs
about ``96.4 * GiB + 0.035 * files`` seconds.  gunicorn kills a sync worker at
900 s (`gunicorn_config.py`), which put the four largest soft-deleted projects
-- 1,590 to 2,189 s predicted -- permanently beyond the browser: the worker died
partway through and whatever the payload loop had reached was already gone.

The property under test is that stopping early is **resumable, never lossy**:
the project document and its S3 object outlive a stopped pass, so every file
left behind is still named by something.
"""

import time

import pytest
from bson import ObjectId

from caper.project_version_cleanup import (
    delete_gridfs_payload_for_project, delete_payload_within_deadline,
)


def _project(n_files, key='AA_directory'):
    ids = [ObjectId() for _ in range(n_files)]
    return {
        '_id': ObjectId(),
        'project_name': 'deadline subject',
        'runs': {'sample_1': [{'Sample_name': 'sample_1', key: i} for i in ids]},
    }, ids


class _Clock:
    """A clock that advances one second per reading."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def test_no_deadline_deletes_the_whole_payload():
    project, ids = _project(5)
    removed = []
    assert delete_gridfs_payload_for_project(removed.append, project) == 5
    assert set(removed) == set(ids)


def test_a_deadline_stops_the_pass_and_reports_what_is_left():
    project, ids = _project(10)
    removed = []
    clock = _Clock()

    outcome = delete_payload_within_deadline(
        removed.append, project, deadline=3.0, now=clock)

    assert outcome.deleted == len(removed)
    assert outcome.remaining, 'the deadline did not stop the pass'
    assert outcome.deleted + len(outcome.remaining) == 10
    assert set(removed).isdisjoint(outcome.remaining)


def test_every_file_is_either_deleted_or_reported():
    """The union must be the whole payload: a file in neither set is a leak."""
    project, ids = _project(25)
    removed = []
    outcome = delete_payload_within_deadline(
        removed.append, project, deadline=5.0, now=_Clock())

    assert set(removed) | set(outcome.remaining) == set(ids)


def test_an_expired_deadline_still_deletes_one_file():
    """Otherwise a project whose deadline has passed can never be deleted at all."""
    project, _ = _project(4)
    removed = []
    outcome = delete_payload_within_deadline(
        removed.append, project, deadline=-1.0, now=_Clock())

    assert outcome.deleted == 1
    assert len(outcome.remaining) == 3


def test_a_second_pass_finishes_what_the_first_left():
    """The resumability claim, end to end."""
    project, ids = _project(12)
    store = set(ids)

    def delete(file_id):
        store.discard(file_id)

    first = delete_payload_within_deadline(delete, project, deadline=4.0, now=_Clock())
    assert store, 'nothing left to resume; widen the test'

    # The document is untouched, so it still names everything that is left.
    second = delete_gridfs_payload_for_project(delete, project)

    assert store == set()
    assert second == len(first.remaining) + first.deleted


def test_protected_files_are_never_counted_as_remaining():
    project, ids = _project(6)
    removed = []
    outcome = delete_payload_within_deadline(
        removed.append, project, protected_file_ids={ids[0]},
        deadline=2.0, now=_Clock())

    assert ids[0] not in removed
    assert ids[0] not in outcome.remaining


def test_a_failing_delete_is_not_reported_as_remaining():
    """Remaining means 'not reached'. Retrying a file that raises is not the fix."""
    project, ids = _project(3)
    seen = []

    def flaky(file_id):
        seen.append(file_id)
        raise RuntimeError('GridFS is unhappy')

    outcome = delete_payload_within_deadline(flaky, project)

    assert len(seen) == 3
    assert outcome.deleted == 0
    assert outcome.remaining == []


def test_the_underscore_spelling_is_still_walked():
    """The deadline path must not quietly reintroduce a narrower key set."""
    project, ids = _project(4, key='Sample_metadata_JSON')
    removed = []
    delete_payload_within_deadline(removed.append, project)
    assert set(removed) == set(ids)
