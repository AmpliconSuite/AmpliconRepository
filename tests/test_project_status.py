"""
The Phase 0 cross-check: classify(), STATUS_QUERIES and matches() must agree.

This is the mechanism of the phase, not a nicety.  Both production incidents in
docs/project-version-history-and-provenance-spec.md §2 were one predicate
maintained in two places, drifting.  project_status.py collapses that pair; if
its three forms can disagree, the bug is rebuilt one level up.

Three layers, deliberately:

  1. ``test_truth_table_*`` -- exhaustive over every combination of the four
     flags, no database.  Covers states production does not have, so a future
     backfill cannot introduce one that nothing has ever classified.
  2. ``test_mongo_agrees_*`` -- the same fixtures inserted into a real MongoDB,
     because the semantics at stake are MongoDB's: ``{'current': False}`` does
     not match a missing field, and ``{'delete': False}`` does not match ``0``.
     A pure-Python test cannot prove agreement with a database.
  3. ``test_classify_agrees_with_status_queries_over_the_whole_database`` --
     the literal §9 requirement, over whatever documents actually exist.

Layer 3 is only worth as much as the database it runs against.  On a laptop it
sees 24 healthy documents and proves nothing; the states that matter live on
the servers.  To run it there, once this branch is deployed::

    amprepo-ssh.sh dev 'docker exec -w /srv amplicon-dev env
        STATUS_CHECK_EXPECT_DB=caper-dev /opt/venv/bin/python -m pytest
        tests/test_project_status.py -m integration -s'

It reads and never writes, so it is safe to run against either server -- but
see ``_assert_known_target`` for why it refuses to run without being told which
one it is pointed at.
"""

import itertools
import os

import pytest
from bson import ObjectId

from caper.project_status import (
    ALL_STATUSES,
    DELETE_FLAG_QUERY,
    DETACHED,
    HEAD_VERSION_QUERY,
    LIVE,
    NOT_DELETED_QUERY,
    PARTIAL_TOMBSTONE_QUERY,
    PRIOR_VERSION_QUERY,
    REACHABLE_BY_URL_QUERY,
    SOFT_DELETED,
    STATUS_QUERIES,
    SUPERSEDED,
    TOMBSTONE,
    classify,
    combine,
    is_reachable_by_url,
    matches,
    resolver_queries,
    status_flags,
    status_query,
)

# Absent is a value here, because on prod it is one: 70 documents have no
# 'current' field at all and that absence is what keeps them unreachable.
ABSENT = object()

# 0 and 1 are included because Python's `==` accepts them for False/True while
# MongoDB's equality does not.  If classify() ever loosens to `==`, these rows
# are what fails.
FLAG_VALUES = (True, False, ABSENT, 0, 1)


def _doc(**flags):
    return {key: value for key, value in flags.items() if value is not ABSENT}


def _all_flag_combinations():
    for delete, current, vdfh, purged in itertools.product(FLAG_VALUES, repeat=4):
        yield _doc(delete=delete, current=current,
                   version_deleted_from_history=vdfh, payload_purged=purged)


ALL_QUERIES = {
    'STATUS_QUERIES[%s]' % status: query for status, query in STATUS_QUERIES.items()
}
ALL_QUERIES.update({
    'NOT_DELETED_QUERY': NOT_DELETED_QUERY,
    'PRIOR_VERSION_QUERY': PRIOR_VERSION_QUERY,
    'DELETE_FLAG_QUERY': DELETE_FLAG_QUERY,
    'HEAD_VERSION_QUERY': HEAD_VERSION_QUERY,
    'PARTIAL_TOMBSTONE_QUERY': PARTIAL_TOMBSTONE_QUERY,
    'REACHABLE_BY_URL_QUERY': REACHABLE_BY_URL_QUERY,
})


# ---------------------------------------------------------------------------
# 1 -- exhaustive, offline
# ---------------------------------------------------------------------------

def test_the_five_statuses_partition_every_flag_combination():
    """Exactly one status query matches each document.  No overlap, no gap.

    DETACHED is written as the complement of the other four precisely so this
    holds by construction; the test is here to catch someone rewriting it as a
    condition of its own.
    """
    for doc in _all_flag_combinations():
        hits = [status for status in ALL_STATUSES
                if matches(doc, STATUS_QUERIES[status])]
        assert len(hits) == 1, f"{doc} matched {hits}"


def test_classify_agrees_with_status_queries_on_every_flag_combination():
    for doc in _all_flag_combinations():
        by_query = [status for status in ALL_STATUSES
                    if matches(doc, STATUS_QUERIES[status])]
        assert classify(doc) == by_query[0], (
            f"classify() and STATUS_QUERIES disagree on {doc}")


def test_the_documented_production_populations_classify_as_documented():
    """Spec §2.3's observed state table, row by row.

    These six rows are all of production.  If a change to this module moves a
    row to a different status, it changes what every cleanup script does to
    345 real documents.
    """
    assert classify({'delete': False, 'current': True}) == LIVE               # 119
    assert classify({'delete': True, 'current': False}) == SUPERSEDED         # 103
    assert classify({'delete': True}) == DETACHED                             # 70
    assert classify({'delete': False, 'current': False}) == DETACHED          # 39
    assert classify({'delete': True, 'current': True}) == SOFT_DELETED        # 12
    assert classify({'delete': True, 'current': False,                        # 2
                     'version_deleted_from_history': True,
                     'payload_purged': True,
                     'redirect_to_project': str(ObjectId())}) == TOMBSTONE


def test_absence_of_current_is_not_the_same_as_current_false():
    """Spec D2.  The 70 documents that hold a tarfile and are unreachable only
    because a field is missing.  Backfilling 'current' on its own would move
    all 70 from DETACHED to SUPERSEDED and make them reachable at once."""
    absent = {'delete': True}
    explicit = {'delete': True, 'current': False}

    assert classify(absent) != classify(explicit)
    assert not matches(absent, PRIOR_VERSION_QUERY)
    assert matches(explicit, PRIOR_VERSION_QUERY)


def test_integer_flags_are_not_booleans():
    """MongoDB's equality is type-bracketed; Python's `==` is not."""
    assert classify({'delete': 0, 'current': 1}) == DETACHED
    assert classify({'delete': False, 'current': True}) == LIVE


def test_a_partial_tombstone_is_superseded_not_a_tombstone():
    """Spec D13: delete_project_version()'s sole-version path removes a version
    from history without purging its payload.  Calling that a TOMBSTONE would
    tell a future cleanup the payload is already gone; it is not."""
    partial = {'delete': True, 'current': False,
               'version_deleted_from_history': True}

    assert classify(partial) == SUPERSEDED
    assert matches(partial, PARTIAL_TOMBSTONE_QUERY)
    assert not matches({'delete': True, 'current': False,
                        'version_deleted_from_history': True,
                        'payload_purged': True}, PARTIAL_TOMBSTONE_QUERY)


def test_a_tombstone_without_a_redirect_is_still_a_tombstone():
    """Transitions T7 and T8 produce a tombstone with nowhere to redirect to."""
    assert classify({'delete': True, 'current': False,
                     'version_deleted_from_history': True,
                     'payload_purged': True,
                     'redirect_to_project': None}) == TOMBSTONE


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

def test_superseded_documents_are_reachable_by_url():
    """Spec D1.  103 production documents; deleting one breaks a live link."""
    assert is_reachable_by_url({'delete': True, 'current': False})


def test_soft_deleted_documents_are_not_reachable_by_url():
    assert not is_reachable_by_url({'delete': True, 'current': True})


def test_detached_documents_can_be_reachable():
    """The 39 delete=False/current=False documents resolve today (spec D3), and
    the 70 with no 'current' field do not.  Both are DETACHED: status and
    reachability are independent axes in Phase 0."""
    reachable = {'delete': False, 'current': False}
    unreachable = {'delete': True}

    assert classify(reachable) == classify(unreachable) == DETACHED
    assert is_reachable_by_url(reachable)
    assert not is_reachable_by_url(unreachable)


def test_reachability_matches_the_resolver_id_steps_on_every_combination():
    """is_reachable_by_url() must agree with resolver_queries()' two _id steps
    for every flag combination -- that equivalence is the argument for why the
    name steps can be left out."""
    for doc in _all_flag_combinations():
        id_steps = [query for line, query in resolver_queries(project_id=ObjectId())
                    if line in ('utils.py:692', 'utils.py:722')]
        by_steps = any(matches(doc, {k: v for k, v in query.items() if k != '_id'})
                       for query in id_steps)
        assert is_reachable_by_url(doc) is by_steps, doc


def test_resolver_queries_are_the_five_documented_steps():
    steps = resolver_queries(project_id=ObjectId(), project_name='CCLE')
    assert [line for line, _ in steps] == [
        'utils.py:692', 'utils.py:703', 'utils.py:711',
        'utils.py:722', 'utils.py:736',
    ]


def test_resolver_queries_drops_id_steps_for_a_non_objectid():
    steps = resolver_queries(project_id='not-an-object-id', project_name='CCLE')
    assert [line for line, _ in steps] == [
        'utils.py:703', 'utils.py:711', 'utils.py:736']


# ---------------------------------------------------------------------------
# Composition and writes
# ---------------------------------------------------------------------------

def test_combine_does_not_drop_a_colliding_constraint():
    """The whole point.  A plain dict merge would silently discard one $nor and
    turn a status filter back into the bare flag test."""
    merged = combine(STATUS_QUERIES[LIVE], STATUS_QUERIES[SUPERSEDED])

    assert '$and' in merged
    assert not matches({'delete': False, 'current': True}, merged)
    assert not matches({'delete': True, 'current': False}, merged)


def test_combine_keeps_a_flat_query_when_nothing_collides():
    merged = combine(STATUS_QUERIES[LIVE], private='public')
    assert merged['private'] == 'public'
    assert merged['delete'] is False


def test_status_query_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        status_query('ARCHIVED')


def test_status_flags_round_trip_through_classify():
    """What status_flags() writes is what classify() reads back."""
    for status in (LIVE, SUPERSEDED, SOFT_DELETED, TOMBSTONE):
        assert classify(status_flags(status)) == status


def test_detached_cannot_be_written():
    with pytest.raises(ValueError):
        status_flags(DETACHED)


def test_matches_refuses_operators_it_does_not_implement():
    """A partial evaluator that guesses is worse than no evaluator."""
    with pytest.raises(ValueError):
        matches({'a': 1}, {'a': {'$gt': 0}})
    with pytest.raises(ValueError):
        matches({'a': 1}, {'$where': 'true'})


# ---------------------------------------------------------------------------
# 2 -- the same rules, evaluated by a real MongoDB
# ---------------------------------------------------------------------------

@pytest.fixture
def status_fixture_collection():
    """A scratch collection holding one document per awkward state (spec §10).

    Separate from 'projects' on purpose: this test writes, and Phase 0 writes
    nothing to real project documents.
    """
    from caper.utils import db_handle

    name = f'phase0_status_crosscheck_{ObjectId()}'
    collection = db_handle[name]
    try:
        yield collection
    finally:
        collection.drop()


def test_mongo_agrees_with_matches_on_every_flag_combination(status_fixture_collection):
    """The in-memory evaluator against the database's own semantics.

    matches() is a second implementation of a slice of the query language, so
    it is exactly the kind of thing this spec exists to distrust.  This is how
    it stays honest.
    """
    docs = []
    for doc in _all_flag_combinations():
        doc = dict(doc, _id=ObjectId())
        docs.append(doc)
    status_fixture_collection.insert_many(docs)

    for label, query in ALL_QUERIES.items():
        from_mongo = {d['_id'] for d in status_fixture_collection.find(query, {'_id': 1})}
        in_memory = {d['_id'] for d in docs if matches(d, query)}
        assert from_mongo == in_memory, (
            f"{label}: MongoDB and matches() disagree on "
            f"{ {str(i) for i in from_mongo ^ in_memory} }")


def test_mongo_agrees_with_matches_on_the_shapes_the_app_queries(status_fixture_collection):
    """matches() implements more than the status flags -- $in, dotted paths,
    array-contains -- because the fakes in the test suite delegate to it and
    the resolver's lineage lookups use all three.  Every one of those is a
    place it could quietly disagree with MongoDB, so every one is checked here.

    The fixtures are the shapes this codebase actually stores: project_members
    as an array, previous_versions as an array of subdocuments, visibility as
    either a legacy boolean or a string.
    """
    alice, bob = 'alice@example.com', 'bob@example.com'
    prior, other = ObjectId(), ObjectId()

    docs = [
        {'_id': ObjectId(), 'project_members': [alice, bob], 'private': 'public',
         'previous_versions': [{'linkid': str(prior)}], 'delete': False, 'current': True},
        {'_id': ObjectId(), 'project_members': [bob], 'private': False,
         'previous_versions': [], 'delete': False, 'current': True},
        {'_id': ObjectId(), 'project_members': [], 'private': True,
         'delete': True, 'current': False},
        {'_id': ObjectId(), 'project_members': [alice], 'private': 'hidden_public',
         'previous_versions': [{'linkid': str(other)}, {'linkid': str(prior)}],
         'delete': True},
        # No project_members and no previous_versions at all.
        {'_id': ObjectId(), 'private': 'public', 'delete': False, 'current': False},
    ]
    status_fixture_collection.insert_many(docs)

    queries = [
        {'project_members': alice},
        {'project_members': {'$in': [alice, bob]}},
        {'project_members': {'$in': ['nobody@example.com']}},
        {'previous_versions.linkid': str(prior)},
        {'previous_versions.linkid': {'$exists': True}},
        {'private': {'$in': [False, 'public']}},
        {'private': {'$in': [True, 'private', 'hidden_public']}},
        {'project_members': {'$ne': alice}},
        {'$or': [{'project_members': alice}, {'private': 'public'}]},
        combine(STATUS_QUERIES[LIVE],
                **{'previous_versions.linkid': str(prior)}),
        status_query(LIVE, private={'$in': [False, 'public']}),
    ]

    for query in queries:
        from_mongo = {d['_id'] for d in status_fixture_collection.find(query, {'_id': 1})}
        in_memory = {d['_id'] for d in docs if matches(d, query)}
        assert from_mongo == in_memory, (
            f"{query}: MongoDB and matches() disagree on "
            f"{ {str(i) for i in from_mongo ^ in_memory} }")


def test_mongo_agrees_with_classify_on_every_flag_combination(status_fixture_collection):
    docs = [dict(doc, _id=ObjectId()) for doc in _all_flag_combinations()]
    status_fixture_collection.insert_many(docs)

    for status in ALL_STATUSES:
        from_mongo = {d['_id'] for d in
                      status_fixture_collection.find(STATUS_QUERIES[status], {'_id': 1})}
        in_memory = {d['_id'] for d in docs if classify(d) == status}
        assert from_mongo == in_memory, f"{status}: disagreement"

    counted = sum(status_fixture_collection.count_documents(STATUS_QUERIES[s])
                  for s in ALL_STATUSES)
    assert counted == len(docs), "the five status queries do not partition the collection"


# ---------------------------------------------------------------------------
# 3 -- over every document that actually exists (spec §9, §13)
# ---------------------------------------------------------------------------

def _assert_known_target():
    """Name the database out loud before reading it.  Spec D10.

    Dev and prod are two databases on one DocumentDB cluster, so the target is
    decided entirely by whichever config.sh was sourced -- there is nothing in
    a connection failure to tell you that you reached the wrong one.  These
    tests only read, so the risk is a misleading *result* rather than damage:
    numbers measured against prod, reported as dev, acted on later.

    Against a non-local host the tests therefore refuse to guess.  Set

        STATUS_CHECK_EXPECT_DB=<name>

    to state which database you meant; a mismatch fails rather than skips,
    because being connected somewhere other than where you think you are is
    the thing worth stopping for.  The connection string itself is never read
    here and never printed -- only the host list, and only to decide local vs
    remote.

    Returns a label naming both the database and whether it was reached
    locally or over the network, which the caller prints alongside its counts
    so the report says what it measured.
    """
    from pymongo import uri_parser

    from caper.utils import collection_handle

    db_name = collection_handle.database.name
    hosts = uri_parser.parse_uri(os.environ['DB_URI_SECRET'])['nodelist']
    local = all(host in ('localhost', '127.0.0.1', 'mongodb', '::1')
                for host, _port in hosts)
    where = 'local' if local else 'remote'

    # The name on its own does not identify the target: the dev server's
    # database is *also* called 'caper-dev', so a laptop with the docker mongo
    # running satisfies STATUS_CHECK_EXPECT_DB=caper-dev while measuring 24
    # documents that prove nothing.  Both halves go into the label, and the
    # label is what the census line prints, so a result cannot later be read as
    # evidence about a database it did not come from.
    expected = os.environ.get('STATUS_CHECK_EXPECT_DB')
    if expected is not None:
        assert db_name == expected, (
            f"connected to database {db_name!r}, but STATUS_CHECK_EXPECT_DB "
            f"says {expected!r}. Check which config.sh is sourced.")
    elif not local:
        pytest.skip(
            f"refusing to measure a remote database ({db_name!r}) without being "
            "told which one was intended: set STATUS_CHECK_EXPECT_DB to the "
            "database name you mean to read.")

    return f"{db_name} ({where})"


@pytest.mark.integration
def test_classify_agrees_with_status_queries_over_the_whole_database():
    """Read-only.  The literal Phase 0 requirement.

    A local database holding only healthy projects proves little on its own --
    which is the point of the two layers above.  This one is what gets run
    against dev, where the awkward states live.
    """
    db_name = _assert_known_target()

    from caper.utils import collection_handle

    projection = {'delete': 1, 'current': 1,
                  'version_deleted_from_history': 1, 'payload_purged': 1}
    documents = list(collection_handle.find({}, projection))
    if not documents:
        pytest.skip(f"no project documents in {db_name!r}")

    # The census belongs in the output whether or not the assertions hold: a
    # run that passes over 24 uniformly healthy documents and one that passes
    # over 345 including the 109 ambiguous ones are very different evidence,
    # and only this line tells them apart afterwards.
    census = {s: sum(1 for d in documents if classify(d) == s) for s in ALL_STATUSES}
    print(f"\n{db_name}: {len(documents)} documents -- "
          + ", ".join(f"{s} {census[s]}" for s in ALL_STATUSES))

    for status in ALL_STATUSES:
        from_mongo = {d['_id'] for d in
                      collection_handle.find(STATUS_QUERIES[status], {'_id': 1})}
        in_memory = {d['_id'] for d in documents if classify(d) == status}
        assert from_mongo == in_memory, (
            f"{status}: classify() and STATUS_QUERIES disagree on "
            f"{sorted(str(i) for i in from_mongo ^ in_memory)}")

    counted = sum(collection_handle.count_documents(STATUS_QUERIES[s])
                  for s in ALL_STATUSES)
    assert counted == len(documents), (
        "the five status queries do not partition the projects collection")


@pytest.mark.integration
def test_every_legacy_query_agrees_with_matches_over_the_whole_database():
    """The named legacy predicates, not just the five statuses.

    NOT_DELETED_QUERY and PRIOR_VERSION_QUERY are the ones the resolver
    actually uses, so they are the ones a cleanup script must not get wrong.
    """
    db_name = _assert_known_target()

    from caper.utils import collection_handle

    projection = {'delete': 1, 'current': 1,
                  'version_deleted_from_history': 1, 'payload_purged': 1}
    documents = list(collection_handle.find({}, projection))
    if not documents:
        pytest.skip(f"no project documents in {db_name!r}")

    for label, query in ALL_QUERIES.items():
        from_mongo = {d['_id'] for d in collection_handle.find(query, {'_id': 1})}
        in_memory = {d['_id'] for d in documents if matches(d, query)}
        assert from_mongo == in_memory, f"{label}: disagreement"


@pytest.mark.integration
def test_the_flags_are_booleans_or_absent():
    """Read-only.  The precondition the behaviour-preservation proof rests on.

    Everything in layer 4 below compares an old spelling against a new one over
    documents *I chose*, so it can only prove agreement on shapes I thought to
    write down.  The shapes I cannot fabricate my way to confidence about are
    the ones nobody intended: a flag holding 1 instead of True, or the string
    "false", or None.

    Those are exactly where the two spellings come apart.  Python's
    ``doc.get('delete', False)`` is truthiness, so 1 is deleted and 0 is not;
    Mongo's ``{'delete': True}`` is type-bracketed, so 1 matches nothing.  Every
    place Phase 0 replaced a truthiness read with ``matches()`` -- both passes
    of schema_validate.py -- is a behaviour change on such a document and a
    no-op on every other.  The spec says there are none (§2.3: `delete` bool on
    all 345, `current` bool on 275 and absent on 70), and absence is fine
    because both spellings agree there.  This is that claim, checked rather
    than inherited.

    A failure here does not mean the resolver is wrong.  It means a document
    exists that neither spelling handles the way anyone assumed, and the fix is
    a decision about that document, not a patch to this module.
    """
    db_name = _assert_known_target()

    from caper.utils import collection_handle

    # Scanned in Python rather than asked as {'$not': {'$type': 'bool'}}:
    # DocumentDB's $not is narrower than Mongo's, and a server-side operator
    # that quietly matches nothing would turn this check into a test that
    # always passes -- the exact failure mode Phase 0 exists to prevent.
    fields = ('delete', 'current', 'version_deleted_from_history', 'payload_purged')
    projection = {field: 1 for field in fields}

    offenders = {}
    for doc in collection_handle.find({}, projection):
        for field in fields:
            if field in doc and not isinstance(doc[field], bool):
                offenders.setdefault(field, []).append(
                    (str(doc['_id']), repr(doc[field])))

    assert not offenders, (
        f"in {db_name}, these documents hold a non-boolean status flag, so "
        f"truthiness and Mongo's type-bracketed equality disagree about them: "
        f"{offenders}")


# The five steps of get_one_project() as their query literals stood at
# 5fb238a~1, immediately before Phase 0 replaced them, plus the two inside
# resolve_redirect_tombstone().  Transcribed rather than derived: deriving them
# from project_status would let one edit move both sides at once, which is the
# whole failure this comparison exists to detect.
_PRE_PHASE_0_STEPS = (
    ('1 _id', lambda key, oid: {'_id': oid, 'delete': False},
     lambda key, oid: combine(NOT_DELETED_QUERY, _id=oid)),
    ('2 alias_name', lambda key, oid: {'alias_name': key, 'delete': False},
     lambda key, oid: combine(NOT_DELETED_QUERY, alias_name=key)),
    ('3 project_name', lambda key, oid: {'project_name': key, 'delete': False},
     lambda key, oid: combine(NOT_DELETED_QUERY, project_name=key)),
    ('4 _id/prior',
     lambda key, oid: {'_id': oid, 'current': False, 'delete': True},
     lambda key, oid: combine(PRIOR_VERSION_QUERY, _id=oid)),
    ('5 project_name/prior',
     lambda key, oid: {'project_name': key, 'current': False, 'delete': True},
     lambda key, oid: combine(PRIOR_VERSION_QUERY, project_name=key)),
)


@pytest.mark.integration
def test_the_pre_phase_0_query_literals_select_the_same_documents():
    """Read-only.  The differential the phase is actually claiming.

    Layer 4 below compares old spelling against new over fixtures *I* wrote, so
    it proves agreement only on states I thought of.  This runs the same
    comparison over every lookup key the database really contains -- every
    ObjectId, every project_name, every alias_name -- and compares the whole
    match set of each query rather than the document the resolver happens to
    pick, which makes it independent of natural ordering and strictly stronger
    than comparing return values.

    On dev this is 484 keys x 5 steps plus the redirect pair: 2,062 query pairs,
    all agreeing.  It is the evidence that Phase 0 changed no behaviour, and it
    is worth more than any amount of soak time, because a soak only exercises
    the documents someone happens to visit.

    One asymmetry it is *designed* to catch, and did not on dev:
    resolve_redirect_tombstone()'s second query gained a tombstone exclusion
    (status_query(LIVE, ...) carries the $nor; the old literal did not).  A
    document that is delete=False, current=True and carries both tombstone
    markers would be a redirect target for the old spelling and not for the
    new.  There are none on dev.  If this ever fails on step R2, that is what
    happened, and the new behaviour is the intended one -- redirecting to a
    purged payload helps nobody -- but it is a real difference, not a no-op.
    """
    db_name = _assert_known_target()

    from caper.utils import collection_handle

    documents = list(collection_handle.find({}, {
        '_id': 1, 'project_name': 1, 'alias_name': 1, 'redirect_to_project': 1}))
    if not documents:
        pytest.skip(f"no project documents in {db_name}")

    def ids(query):
        return frozenset(d['_id'] for d in collection_handle.find(query, {'_id': 1}))

    keys = {(str(doc['_id']), doc['_id']) for doc in documents}
    for doc in documents:
        for field in ('project_name', 'alias_name'):
            if isinstance(doc.get(field), str) and doc[field]:
                keys.add((doc[field], None))

    disagreements = []
    pairs = 0
    for key, oid in sorted(keys, key=lambda pair: pair[0]):
        for label, old, new in _PRE_PHASE_0_STEPS:
            if oid is None and '_id' in label:
                continue        # a name is not an ObjectId; the old code threw
            pairs += 1
            before, after = ids(old(key, oid)), ids(new(key, oid))
            if before != after:
                disagreements.append(
                    f"step {label}, key {key!r}: "
                    f"{sorted(str(i) for i in before ^ after)}")

    for doc in documents:
        redirect_to = doc.get('redirect_to_project')
        if not redirect_to:
            continue
        rid = str(redirect_to)
        for label, before, after in (
                ('R1 redirect _id',
                 ids({'_id': ObjectId(rid), 'delete': False}),
                 ids(combine(NOT_DELETED_QUERY, _id=ObjectId(rid)))),
                ('R2 redirect backlink',
                 ids({'current': True, 'delete': False,
                      'previous_versions.linkid': rid}),
                 ids(status_query(LIVE, **{'previous_versions.linkid': rid})))):
            pairs += 1
            if before != after:
                disagreements.append(
                    f"step {label}, tombstone {doc['_id']}: "
                    f"{sorted(str(i) for i in before ^ after)}")

    print(f"\n{db_name}: {pairs} query pairs compared across "
          f"{len(keys)} lookup keys")
    assert not disagreements, (
        f"in {db_name}, the pre-Phase-0 literals and project_status disagree:\n"
        + "\n".join(disagreements[:40]))


# ---------------------------------------------------------------------------
# 4 -- the rewrite is behaviour-preserving
# ---------------------------------------------------------------------------
#
# Phase 0 routes 72 call sites through this module and changes no behaviour.
# "Changes no behaviour" is a claim about query results, so it is checked as
# one: every literal that was replaced, run against the same documents as its
# replacement, over a fixture set built from the §10 table -- one document per
# awkward state production actually holds.
#
# Left column: exactly what the code said before this change.
# Right column: what it says now.

REPLACED_QUERIES = [
    # utils.py -- the resolver and its neighbours
    ("utils.py:190/593/692/703/711/774/1100/1111/1119/1232 and views.py",
     {'delete': False},
     lambda: NOT_DELETED_QUERY),
    ("utils.py:722/736/1131/1145 -- the fallback every cleanup tool missed",
     {'current': False, 'delete': True},
     lambda: PRIOR_VERSION_QUERY),
    ("utils.py:762 -- get_one_deleted_project",
     {'delete': True},
     lambda: DELETE_FLAG_QUERY),
    ("utils.py:203/671 and search/site_stats/views/views_admin/views_apis",
     {'delete': False, 'current': True},
     lambda: STATUS_QUERIES[LIVE]),
    ("utils.py:1012/1069 -- reverse lineage lookup",
     {'current': True},
     lambda: HEAD_VERSION_QUERY),
    ("views_admin.py:773 -- the admin permanent-delete page",
     {'delete': True, 'current': True},
     lambda: STATUS_QUERIES[SOFT_DELETED]),
    ("project_version_cleanup.py:161 -- tombstone retargeting",
     {'version_deleted_from_history': True, 'payload_purged': True},
     lambda: STATUS_QUERIES[TOMBSTONE]),
    ("cleanup_orphaned_projects.py:266 -- the 70 with no 'current' field",
     {'delete': True, 'current': {'$exists': False}},
     lambda: combine(DELETE_FLAG_QUERY, current={'$exists': False})),
]


def _fixture_documents():
    """One document per row of the spec's §10 fixture table, plus the six
    production state combinations of §2.3."""
    head = ObjectId()
    prior = ObjectId()
    return [
        # §2.3 row 1 -- LIVE (119 on prod)
        {'_id': head, 'project_name': 'live', 'delete': False, 'current': True,
         'previous_versions': [{'linkid': str(prior)}], 'tarfile': ObjectId()},
        # §2.3 row 2 -- SUPERSEDED referenced by a head (89 on prod)
        {'_id': prior, 'project_name': 'superseded-referenced',
         'delete': True, 'current': False, 'tarfile': ObjectId()},
        # SUPERSEDED reachable but referenced by nothing (14 on prod)
        {'_id': ObjectId(), 'project_name': 'superseded-orphan',
         'delete': True, 'current': False, 'tarfile': ObjectId()},
        # §2.3 row 3 -- delete=True with NO 'current' field (70 on prod)
        {'_id': ObjectId(), 'project_name': 'no-current-field',
         'delete': True, 'tarfile': ObjectId()},
        # §2.3 row 4 -- delete=False, current=False (39 on prod)
        {'_id': ObjectId(), 'project_name': 'detached-reachable',
         'delete': False, 'current': False, 'tarfile': ObjectId()},
        # §2.3 row 5 -- SOFT_DELETED (12 on prod)
        {'_id': ObjectId(), 'project_name': 'soft-deleted',
         'delete': True, 'current': True, 'tarfile': ObjectId()},
        # §2.3 row 6 -- complete tombstone (2 on prod)
        {'_id': ObjectId(), 'project_name': 'tombstone',
         'delete': True, 'current': False,
         'version_deleted_from_history': True, 'payload_purged': True,
         'redirect_to_project': str(head)},
        # D13 -- removed from history, payload never purged (0 on prod, latent)
        {'_id': ObjectId(), 'project_name': 'partial-tombstone',
         'delete': True, 'current': False,
         'version_deleted_from_history': True, 'tarfile': ObjectId()},
        # D5 -- a dangling previous_versions.linkid (2 prod / 6 dev)
        {'_id': ObjectId(), 'project_name': 'dangling-history',
         'delete': False, 'current': True,
         'previous_versions': [{'linkid': str(ObjectId())}]},
        # D6 -- live and also referenced as history (3 on dev, 0 on prod)
        {'_id': ObjectId(), 'project_name': 'live-and-superseded',
         'delete': False, 'current': True},
        # D4 -- a detached document sharing a name with a live project
        {'_id': ObjectId(), 'project_name': 'live',
         'delete': True, 'tarfile': ObjectId()},
        # No 'delete' field at all -- not a production state, but nothing in
        # the schema forbids it and every query has to have an answer for it.
        {'_id': ObjectId(), 'project_name': 'no-delete-field', 'current': True},
    ]


@pytest.fixture
def spec_fixture_collection(status_fixture_collection):
    status_fixture_collection.insert_many(_fixture_documents())
    return status_fixture_collection


@pytest.mark.parametrize('label,before,after',
                         [(label, before, after) for label, before, after in REPLACED_QUERIES],
                         ids=[label.split(' --')[0] for label, _, _ in REPLACED_QUERIES])
def test_the_rewrite_selects_exactly_what_the_old_literal_did(
        spec_fixture_collection, label, before, after):
    """No behaviour change, checked rather than asserted.

    A difference here is not necessarily a bug -- STATUS_QUERIES excludes
    tombstones where some old literals did not -- but it is always a decision
    someone has to have made on purpose, and this is where it surfaces.
    """
    old_ids = {d['_id'] for d in spec_fixture_collection.find(before, {'_id': 1})}
    new_ids = {d['_id'] for d in spec_fixture_collection.find(after(), {'_id': 1})}

    assert old_ids == new_ids, (
        f"{label}: routing changed which documents are selected. "
        f"only before: {sorted(str(i) for i in old_ids - new_ids)}; "
        f"only after: {sorted(str(i) for i in new_ids - old_ids)}")


def test_the_one_deliberate_difference_is_the_tombstone_exclusion(spec_fixture_collection):
    """STATUS_QUERIES[SUPERSEDED] is narrower than the literal it replaced.

    {'delete': True, 'current': False} matches tombstones too, which is why
    the resolver's fallback uses PRIOR_VERSION_QUERY and not this -- excluding
    them there would stop deleted-version URLs redirecting.  Nothing else in
    the rewrite narrows anything, and this test is what says so out loud.
    """
    literal = {'delete': True, 'current': False}
    literal_ids = {d['_id'] for d in spec_fixture_collection.find(literal, {'_id': 1})}
    superseded_ids = {d['_id'] for d in
                      spec_fixture_collection.find(STATUS_QUERIES[SUPERSEDED], {'_id': 1})}
    tombstone_ids = {d['_id'] for d in
                     spec_fixture_collection.find(STATUS_QUERIES[TOMBSTONE], {'_id': 1})}

    assert literal_ids - superseded_ids == tombstone_ids
    assert tombstone_ids, "fixture set no longer contains a tombstone"


def test_the_resolver_reaches_every_state_the_application_serves(spec_fixture_collection):
    """Spec I10 and D1, on the fixture set: every LIVE and every SUPERSEDED
    document resolves by _id through the queries get_one_project() issues."""
    for doc in spec_fixture_collection.find({}):
        expected = classify(doc) in (LIVE, SUPERSEDED, TOMBSTONE) or is_reachable_by_url(doc)
        if not expected:
            continue
        hit = None
        for _line, query in resolver_queries(doc['_id'], doc.get('project_name')):
            found = spec_fixture_collection.find_one(query, {'_id': 1})
            if found is not None and found['_id'] == doc['_id']:
                hit = found
                break
        assert hit is not None, (
            f"{doc.get('project_name')} ({classify(doc)}) is served by the "
            f"application but no resolver step reaches it")
