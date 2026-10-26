"""
Derived metadata must be storable on an old version, not just on the head.

The project page derives ``ecDNA_context``, ``sample_data`` and ``aggregate_df``
from a version's own archive the first time it is opened, and stores them so
the next visit does not have to. Both writes were gated on ``{'delete': False}``,
which no superseded version matches -- versioning marks every earlier version
``delete: True`` -- so for an old version the update matched nothing and the
page re-read the whole archive on every visit. On prod the old CCLE version
(507 MiB archive) took a median 14.5 s per view over 2026-10-19..25.
"""

import pytest


def _doc(name, status):
    from caper.project_status import status_flags
    return {'project_name': name, 'private': 'public', 'FINISHED?': True,
            'runs': {}, **status_flags(status)}


@pytest.fixture
def three_statuses(mongo_collection):
    from caper.project_status import LIVE, SUPERSEDED, TOMBSTONE
    ids = {}
    for status in (LIVE, SUPERSEDED, TOMBSTONE):
        ids[status] = mongo_collection.insert_one(
            _doc(f'OldVersionCache_{status}', status)).inserted_id
    try:
        yield ids
    finally:
        mongo_collection.delete_many({'_id': {'$in': list(ids.values())}})


@pytest.mark.integration
def test_ecDNA_context_is_stored_on_an_old_version(mongo_collection, three_statuses):
    from caper.project_status import LIVE, SUPERSEDED, TOMBSTONE
    from caper.utils import initialize_ecDNA_context

    for status, _id in three_statuses.items():
        initialize_ecDNA_context(mongo_collection.find_one({'_id': _id}))

    stored = {status: 'ecDNA_context' in mongo_collection.find_one({'_id': _id})
              for status, _id in three_statuses.items()}
    assert stored[LIVE]
    assert stored[SUPERSEDED], 'an old version must keep what was derived from its archive'
    assert not stored[TOMBSTONE], 'a tombstone has no archive to derive from'


@pytest.mark.integration
def test_page_metadata_write_filter_reaches_old_versions(mongo_collection, three_statuses):
    """The filter the project page's metadata save uses, against real documents."""
    from caper.project_status import (LIVE, OPENABLE_WITH_PAYLOAD_QUERY, SUPERSEDED,
                                      TOMBSTONE, combine)

    matched = {status: mongo_collection.count_documents(
                   combine(OPENABLE_WITH_PAYLOAD_QUERY, _id=_id))
               for status, _id in three_statuses.items()}
    assert matched == {LIVE: 1, SUPERSEDED: 1, TOMBSTONE: 0}
