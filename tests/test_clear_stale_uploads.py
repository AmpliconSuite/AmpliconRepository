"""Tests for clear_stale_uploads.py.

Its own file rather than an addition to test_cleanup_orphaned_projects.py or
test_backfill_project_status.py: those cover scripts that classify and correct
documents, and every assertion here is about a *deletion* refusing to happen.
The seed population is also different -- one document per way "this placeholder
is dead" can be false.

Against a real MongoDB in a throwaway database, like the backfill tests: the
selection query mixes $exists, $regex and an equality term, and the delete is
preconditioned on that same query still matching.
"""

import datetime
import json
import os
import uuid

import pytest
from bson import ObjectId
from pymongo import MongoClient

from clear_stale_uploads import (
    STALE_UPLOAD_QUERY,
    apply_plan,
    build_reference_index,
    plan,
    reasons_to_keep,
    take,
)


@pytest.fixture
def projects():
    """A scratch collection inside the configured database.

    It used to be a scratch *database*, which cannot work against a
    least-privilege deployment: dev connects as ``caper_app_dev``, whose role
    is scoped to ``caper-dev``, so creating ``caper-stale-uploads-test-<hex>`` came back
    ``Authorization failure`` and took nine tests in this file with it on
    2026-08-31. The guard above did not catch it because ``admin.command
    ('ping')`` asks whether the server is reachable, not whether the write
    about to happen is allowed -- so the run failed loudly instead of skipping.

    A uniquely-named collection in the database we are already entitled to use
    needs no privilege the application does not have, which is the same choice
    ``status_fixture_collection`` and the ownership-survey fixtures already
    make. The name is unique per test, so parallel runs do not collide, and it
    is dropped in a ``finally``.
    """
    uri = os.getenv('DB_URI_SECRET')
    db_name = os.getenv('DB_NAME')
    if not uri or not db_name:
        pytest.skip('DB_URI_SECRET and DB_NAME must both be set')
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command('ping')
    except Exception as exc:
        pytest.skip('no MongoDB available: %s' % exc)
    collection = client[db_name]['stale_uploads_test_%s' % uuid.uuid4().hex[:8]]
    try:
        yield collection
    finally:
        collection.drop()


def old_oid(days):
    """An ObjectId whose embedded creation time is *days* ago.

    from_datetime() zeroes everything after the timestamp, so two ids built for
    the same day are equal. The timestamp is kept and the rest comes from a
    fresh id, which is what makes several documents of the same age possible.
    """
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return ObjectId(str(ObjectId.from_datetime(when))[:8] + str(ObjectId())[8:])


def placeholder(oid, name='Something', **extra):
    doc = {
        '_id': oid,
        'project_name': '%s (Processing...)' % name,
        'original_project_name': name,
        'owner': 'someone',
        'aggregation_in_progress': True,
        'delete': False,
        'current': True,
        'runs': {},
    }
    doc.update(extra)
    return doc


DEAD = old_oid(200)
YOUNG = old_oid(1)
WITH_RUNS = old_oid(200)
WITH_FILES = old_oid(200)
REFERENCED = old_oid(200)
REDIRECTED = old_oid(200)
REAL_PROJECT = old_oid(300)
NAMER = old_oid(100)
TOMBSTONE = old_oid(100)


def seed(projects):
    projects.insert_many([
        placeholder(DEAD, 'Dead'),
        placeholder(YOUNG, 'Started yesterday'),
        placeholder(WITH_RUNS, 'Has samples',
                    runs={'sample1': [{'Feature_ID': 'amplicon1'}]}),
        # The id is in a feature field, not in one of the placeholder's own
        # fields -- the walk has to find it wherever it is.
        placeholder(WITH_FILES, 'Has a file',
                    runs={'sample1': [{'AA_PNG_file': ObjectId()}]}),
        placeholder(REFERENCED, 'In a history'),
        placeholder(REDIRECTED, 'A redirect target'),
        # Not a placeholder at all: finished, owner unset. Must never be
        # selected, whatever else is true of it.
        {'_id': REAL_PROJECT, 'project_name': 'A real project',
         'delete': False, 'current': True, 'runs': {}},
        {'_id': NAMER, 'project_name': 'Names the referenced one',
         'delete': False, 'current': True,
         'previous_versions': [{'linkid': str(REFERENCED)}]},
        {'_id': TOMBSTONE, 'project_name': 'Points at the redirected one',
         'delete': True, 'current': False,
         'version_deleted_from_history': True, 'payload_purged': True,
         'redirect_to_project': str(REDIRECTED)},
    ])


def ids(docs):
    return {str(doc['_id']) for doc in docs}


def test_only_the_dead_placeholder_is_deletable(projects):
    seed(projects)

    deletable, held_back = plan(projects, min_age_days=7)

    assert ids(deletable) == {str(DEAD)}
    assert ids(doc for doc, _reasons in held_back) == {
        str(YOUNG), str(WITH_RUNS), str(WITH_FILES),
        str(REFERENCED), str(REDIRECTED),
    }


def test_each_held_back_document_says_why(projects):
    seed(projects)
    _deletable, held_back = plan(projects, min_age_days=7)
    reasons = {str(doc['_id']): ' '.join(rs) for doc, rs in held_back}

    assert 'may still be aggregating' in reasons[str(YOUNG)]
    assert 'holds 1 sample' in reasons[str(WITH_RUNS)]
    assert 'AA_PNG_file' in reasons[str(WITH_FILES)]
    assert 'history of' in reasons[str(REFERENCED)]
    assert 'redirect target of' in reasons[str(REDIRECTED)]


def test_a_finished_project_is_never_selected(projects):
    """The selection needs all three placeholder fingerprints.

    A project that finished has had `owner` unset, so it cannot match -- but
    the test is here because the query is the only thing standing between this
    script and a real project, and it is three terms rather than one for
    exactly that reason.
    """
    seed(projects)

    selected = {str(doc['_id']) for doc in projects.find(STALE_UPLOAD_QUERY)}

    assert str(REAL_PROJECT) not in selected
    assert str(NAMER) not in selected
    assert str(TOMBSTONE) not in selected


def test_age_is_read_from_the_object_id_not_from_date_created(projects):
    """15 of the 21 placeholders on prod have no date_created at all.

    A missing age must not read as old, so the age comes from the id, which
    every document has.
    """
    doc = placeholder(YOUNG, 'No date_created')
    assert 'date_created' not in doc

    assert reasons_to_keep(doc, {}, min_age_days=7)
    assert not reasons_to_keep(placeholder(DEAD), {}, min_age_days=7)


def test_executing_deletes_only_the_planned_document(projects):
    seed(projects)
    deletable, _held = plan(projects, min_age_days=7)
    before = projects.count_documents({})

    written, skipped = apply_plan(projects, deletable, execute=True)

    assert (written, skipped) == (1, 0)
    assert projects.count_documents({}) == before - 1
    assert projects.find_one({'_id': DEAD}) is None


def test_report_mode_deletes_nothing(projects):
    seed(projects)
    deletable, _held = plan(projects, min_age_days=7)
    before = projects.count_documents({})

    apply_plan(projects, deletable, execute=False)

    assert projects.count_documents({}) == before


def test_the_undo_record_restores_the_document(projects, tmp_path):
    """The inverse of a delete is an insert, so the whole document is kept."""
    seed(projects)
    deletable, _held = plan(projects, min_age_days=7)
    path = tmp_path / 'undo.jsonl'

    with open(path, 'x') as undo:
        apply_plan(projects, deletable, execute=True, undo=undo)

    lines = [json.loads(line) for line in open(path)]
    assert len(lines) == 1
    assert lines[0]['op'] == 'insert'
    assert lines[0]['document']['project_name'] == 'Dead (Processing...)'
    assert lines[0]['document']['_id'] == str(DEAD)


def test_a_placeholder_that_finished_between_plan_and_delete_is_kept(projects):
    """The precondition, which is the whole reason the delete is not a
    delete_many on the selection query.

    A background thread that wakes up and finishes the upload clears
    `owner` and `aggregation_in_progress`. If that lands after the plan is
    built, this must not remove the project it just produced.
    """
    seed(projects)
    deletable, _held = plan(projects, min_age_days=7)

    projects.update_one({'_id': DEAD}, {
        '$unset': {'owner': '', 'aggregation_in_progress': ''},
        '$set': {'project_name': 'Dead', 'runs': {'sample1': []}}})

    written, skipped = apply_plan(projects, deletable, execute=True)

    assert (written, skipped) == (0, 1)
    assert projects.find_one({'_id': DEAD}) is not None


def test_limit_stages_the_deletion(projects):
    seed(projects)
    second = old_oid(199)
    projects.insert_one(placeholder(second, 'Also dead'))

    deletable, _held = plan(projects, min_age_days=7)
    assert len(deletable) == 2

    apply_plan(projects, take(deletable, 1), execute=True)

    remaining, _held = plan(projects, min_age_days=7)
    assert len(remaining) == 1
    assert ids(remaining) == ids(deletable) - {str(deletable[0]['_id'])}


def test_build_reference_index_reads_both_lineage_encodings(projects):
    """previous_versions has two encodings and the reference has to be seen in
    either, or a placeholder inside an old chain looks unreferenced."""
    legacy_target = ObjectId()
    projects.insert_one({
        '_id': ObjectId(), 'project_name': 'Legacy encoding',
        'previous_versions': [json.dumps([
            {'date': '2024-03-14T20:49:33', 'link': str(legacy_target)}])],
    })

    index = build_reference_index(projects)

    assert str(legacy_target) in index
