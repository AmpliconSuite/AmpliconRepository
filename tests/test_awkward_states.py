"""
The awkward states, and the validator that is supposed to find them.

``tests/awkward_states.py`` builds the twelve document shapes a development
database has never contained.  This file is the reason that is worth doing: it
seeds them into the local mongo and asserts that ``classify()`` calls each one
what the catalogue says it is, and that
``validate_project_lineage.py`` reports exactly the violations the catalogue
declares -- no more and no fewer.

The "no more" half is the one that matters.  A validator is easy to write so
that it flags something about every document; the finding then means nothing,
and the report gets skimmed.  Here the healthy shapes are seeded alongside the
broken ones, and a finding about a healthy one fails the test.

**Everything is scoped to the documents these tests create.**  The local
database has other documents in it -- twenty-four real projects, plus whatever
the UI seed script left -- and some of them have genuine findings of their own
(the seed script's fabricated version history is three dangling references).
Asserting on the whole database would make these tests pass or fail based on
what the developer happened to have loaded, which is the definition of a flaky
test.  So every assertion below compares the findings *about fixture ids*
against the catalogue, and ignores the rest.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import validate_project_lineage as validator                    # noqa: E402
from awkward_states import (                                    # noqa: E402
    MARKER, SHAPES, build_all, purge, seed,
)
from caper.project_status import (                              # noqa: E402
    ALL_STATUSES, STATUS_QUERIES, classify,
)


# ---------------------------------------------------------------------------
# Layer 1 -- the catalogue alone, no database
# ---------------------------------------------------------------------------

def test_the_catalogue_builds_and_is_internally_consistent():
    """build_all() validates the catalogue as it goes; this proves it runs.

    Shape names unique, every ``expect`` and ``violates`` key naming a document
    the shape actually builds, every id distinct.  Those checks live in
    ``build_all`` rather than here because the seeder needs them too -- a
    catalogue error should stop a write, not only a test run.
    """
    plan = build_all()

    names = [shape.name for shape in SHAPES]
    assert len(set(names)) == len(names), 'duplicate shape name'

    ids = [doc['_id'] for doc in plan.documents]
    assert len(set(ids)) == len(ids), 'two shapes were given the same _id'
    assert len(plan.documents) == len(plan.expected), \
        'every seeded document should have a declared status'


def test_classify_calls_each_shape_what_the_catalogue_says():
    """The declared status is what ``classify()`` actually returns.

    Pure -- no database -- so a catalogue whose ``expect`` has drifted from the
    flags its ``build`` writes fails immediately and without a mongo running.
    """
    plan = build_all()
    for doc in plan.documents:
        assert classify(doc) == plan.expected[doc['_id']], (
            f"{doc['project_name']}: catalogue says "
            f"{plan.expected[doc['_id']]}, classify() says {classify(doc)}")


def test_every_status_is_represented():
    """All five statuses occur in the catalogue.

    Without this, a shape could be dropped and the fixture set would quietly
    stop covering a state -- which is precisely the condition that let every
    one of these bugs reach production: a database with only LIVE documents in
    it.
    """
    covered = set(build_all().expected.values())
    missing = set(ALL_STATUSES) - covered
    assert not missing, f'no fixture produces {sorted(missing)}'


def test_the_catalogue_covers_the_states_that_occur_for_real():
    """The eight states that occur in the real databases each have a shape.

    Named individually rather than counted, so that renaming a shape without
    replacing what it stood for is a failure rather than a silent gap. The
    comments are the populations measured in August 2026; they will drift, and
    the names are what the test is actually about.
    """
    required = {
        'superseded_referenced',            # 89 prod
        'superseded_unreferenced',          # 14 prod
        'detached_no_current_field',        # 70 prod
        'detached_both_false',              # 39 prod
        'tombstone_triple',                 # 2 prod
        'dangling_lineage_reference',       # 2 prod / 6 dev
        'live_also_referenced_as_history',  # 3 dev
        'name_collision_detached_vs_live',  # 12 prod
    }
    missing = required - {shape.name for shape in SHAPES}
    assert not missing, f'real-world states with no shape: {sorted(missing)}'


# ---------------------------------------------------------------------------
# Layer 2 -- the source-level invariant, no database
# ---------------------------------------------------------------------------

# The one place outside project_version_cleanup.py that still writes a tombstone
# marker by hand.  delete_project_version()'s sole-version path sets
# 'version_deleted_from_history' without purging the payload and without going
# through build_deleted_version_tombstone, so the document keeps its whole
# GridFS payload while the log line says the project was fully removed. Fixing
# it means changing a write path, which this change deliberately does not do --
# but it is pinned, so a *second* one cannot appear without this test failing.
KNOWN_HAND_WRITTEN_TOMBSTONES = {
    (os.path.join('caper', 'caper', 'views.py'),
     "'version_deleted_from_history': True,"),
}


def test_the_only_hand_written_tombstone_is_the_known_one():
    """I18: exactly one routine creates tombstones.

    Keyed on file and line text rather than line number, for the same reason
    the grep guard is: line numbers move for unrelated reasons, and a check
    that cries wolf gets switched off.  The text changing is exactly when
    somebody should look again.
    """
    found = {(path, text)
             for path, _number, text in validator.i18_hand_written_tombstones()}

    new = found - KNOWN_HAND_WRITTEN_TOMBSTONES
    assert not new, (
        "a tombstone marker is written by hand somewhere new:\n  "
        + "\n  ".join(f'{path}: {text}' for path, text in sorted(new))
        + "\n\nTombstones are created in one place, project_version_cleanup."
          "build_deleted_version_tombstone(). If this line is a query rather "
          "than a write, use STATUS_QUERIES[TOMBSTONE].")

    fixed = KNOWN_HAND_WRITTEN_TOMBSTONES - found
    assert not fixed, (
        "one of the known hand-written tombstones is gone -- good. Remove it "
        f"from KNOWN_HAND_WRITTEN_TOMBSTONES: {sorted(fixed)}")


def test_i18_refuses_to_pass_when_it_cannot_read_the_source():
    """A check that examined nothing must not report ``ok``.

    Found the hard way: the first run against dev pointed the source walk at a
    directory that did not exist, so ``os.walk`` yielded nothing, the finding
    list came back empty, and the validator printed ``ok I18`` -- a green tick
    for a check that had not looked at a single file. That is the shape of both
    incidents this work exists to prevent, reproduced inside the tool built
    to prevent them.
    """
    real_root = validator._REPO_ROOT
    validator._REPO_ROOT = os.path.join(real_root, 'no-such-directory')
    try:
        with pytest.raises(validator.Unavailable):
            validator.i18_hand_written_tombstones()
    finally:
        validator._REPO_ROOT = real_root

    # ...and it does read files when pointed somewhere real, so the check is
    # not passing by raising on everything.
    assert validator.i18_hand_written_tombstones() is not None


def test_the_seeder_refuses_a_database_that_is_not_local():
    """The seeder is the only thing in this work that writes.

    Dev and production are two databases on one DocumentDB cluster differing
    by one environment variable, so the guard has to hold for a target it was
    never told about rather than for a name it recognises.
    """
    import awkward_states

    with pytest.raises(SystemExit) as raised:
        awkward_states._assert_local(
            'mongodb://user:pw@amprepo.cluster-abc.us-east-1.docdb.amazonaws.com:27017/',
            'caper-dev')
    assert 'refusing to write' in str(raised.value)

    # ...and allows a local one, so the guard is not passing by refusing
    # everything.
    assert awkward_states._assert_local('mongodb://localhost:27017/', 'caper-dev')


# ---------------------------------------------------------------------------
# Layer 3 -- seeded into a real database
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(mongo_collection):
    """Seed the catalogue into the local database, and take it out again.

    Torn down in a ``finally`` so a failing assertion cannot leave fixture
    documents behind: ``purge`` selects on MARKER alone, so it removes exactly
    what ``seed`` wrote and can never reach a real project even if one shares a
    fixture's name.
    """
    database = mongo_collection.database
    plan = build_all()
    try:
        seed(database, plan)
        yield plan, database
    finally:
        purge(database)


@pytest.mark.integration
def test_seeded_documents_classify_as_declared(seeded):
    """Round-trip: what BSON gives back is still what the catalogue promised.

    Distinct from the pure test above, and the difference is the point -- these
    documents pass through MongoDB's type system on the way, and the whole
    module rests on a boolean staying a boolean.  A ``delete`` that came back
    as ``1`` would classify differently, and nothing in memory would show it.
    """
    plan, database = seeded
    for doc in database['projects'].find({MARKER: True}):
        assert classify(doc) == plan.expected[doc['_id']], (
            f"{doc['project_name']} came back from mongo classifying as "
            f"{classify(doc)}, not {plan.expected[doc['_id']]}")


@pytest.mark.integration
def test_status_queries_select_the_awkward_documents(seeded):
    """``STATUS_QUERIES`` and ``classify()`` agree on states a laptop has never held.

    ``tests/test_project_status.py`` runs this comparison over the whole
    database, which on a developer machine means twenty-four LIVE documents and
    proves nothing about the other four states.  With the catalogue seeded it
    proves something: every branch of the table, including the ``$nor`` in
    ``DETACHED`` and ``TOMBSTONE``, gets a document to be right or wrong about.
    """
    plan, database = seeded
    fixture_ids = set(plan.expected)

    for status in ALL_STATUSES:
        from_mongo = {doc['_id'] for doc in
                      database['projects'].find(STATUS_QUERIES[status], {'_id': 1})
                      } & fixture_ids
        in_memory = {doc_id for doc_id, expected in plan.expected.items()
                     if expected == status}
        assert from_mongo == in_memory, (
            f'{status}: the query and the catalogue disagree on '
            f'{sorted(str(i) for i in from_mongo ^ in_memory)}')


@pytest.mark.integration
def test_the_validator_finds_exactly_the_declared_violations(seeded):
    """Every ``violates`` entry is found, and nothing else about a fixture is.

    This is what the catalogue is for.  Run against dev or prod, a validator
    finding is either a real defect or a bug in the finder, and there is no way
    to tell which from the output.  Run against a database whose defects were
    written down in advance, both directions are checkable: a missed violation
    fails, and so does a healthy document being flagged.
    """
    plan, database = seeded
    snapshot = validator.Snapshot(database['projects'], database['fs.files'])

    fixture_ids = set(plan.expected)
    found = set()
    for invariant in validator.INVARIANTS:
        if invariant.check is None:
            continue
        for finding in invariant.check(snapshot):
            if finding.doc_id in fixture_ids:
                found.add((finding.invariant, finding.doc_id))

    declared = {(inv, doc_id) for inv, _shape, doc_id in plan.violations}

    missed = declared - found
    assert not missed, (
        'the validator did not report violations the catalogue declares: '
        + ', '.join(f'{inv} on {doc_id}' for inv, doc_id in sorted(missed, key=str)))

    spurious = found - declared
    assert not spurious, (
        'the validator reported findings against fixtures declared healthy: '
        + ', '.join(f'{inv} on {doc_id} ({snapshot.name(doc_id)})'
                    for inv, doc_id in sorted(spurious, key=str)))


@pytest.mark.integration
def test_purge_removes_exactly_what_seed_wrote(mongo_collection):
    """Nothing the seeder writes survives, and nothing else is touched.

    Deliberately not using the ``seeded`` fixture: the thing under test is the
    teardown, so it has to run in the body where its effect can be measured.
    """
    database = mongo_collection.database
    plan = build_all()

    before_documents = database['projects'].count_documents({})
    before_files = database['fs.files'].count_documents({})

    seed(database, plan)
    assert database['projects'].count_documents({}) == before_documents + len(plan)

    removed_documents, removed_files = purge(database)
    assert removed_documents == len(plan)
    assert database['projects'].count_documents({}) == before_documents
    assert database['fs.files'].count_documents({}) == before_files, (
        f'purge left {database["fs.files"].count_documents({}) - before_files} '
        f'GridFS file(s) behind (it removed {removed_files})')
    assert database['projects'].count_documents({MARKER: True}) == 0
