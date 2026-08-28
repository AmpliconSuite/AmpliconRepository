"""
The awkward project documents, built locally instead of found in production.

Every bug in this area has needed production data to find.  Not because the states are exotic -- there are eight
of them and they are all describable in a sentence -- but because **no
development database has ever contained one**.  A laptop holds twenty-four
healthy documents: one version each, every flag present, every payload intact.
Code that mishandles a superseded version passes every test on that database
and destroys fourteen live projects on the other one.

The obvious place to put these is dev.  A laptop is better in the two ways
that matter: it costs nothing to get wrong, and it runs inside the test suite,
so the states are checked on every commit rather than the next time someone
remembers.  Dev also shares a DocumentDB cluster with production, and a seeder
pointed at it writes documents into a database real people read -- so this one
refuses to run anywhere but a local mongo.  See ``_assert_local``.

Two consumers, one catalogue:

  * ``tests/test_awkward_states.py`` builds the shapes, asserts
    ``classify()`` calls each one what this file says it is, and pins
    ``validate_project_lineage.py``'s findings against ``violates`` below.
  * ``python tests/awkward_states.py --execute`` seeds them into the local
    database so they can be clicked through in the browser, and ``--purge``
    takes them out again.

The catalogue is the single copy.  A shape added here is covered by the tests
and visible in the browser without being written down anywhere else -- which is
the property this codebase keeps losing.  Nearly every defect in this area has
been a list or a predicate maintained in two places.


What "violates" means
---------------------

Six of these shapes are *supposed* to fail an invariant -- that is what makes
them worth having.  Each one declares which, so the validator can be tested
against a database whose defects are known in advance.  Run against a real
one, a finding is either a real problem or a bug in the finder, and the output
cannot tell you which.  A shape with no ``violates`` entry must produce no finding
at all; an unexpected finding fails the test just as loudly as a missed one.
"""

import json
import os
import sys

# Both entry points need <repo>/caper on the path: pytest gets it from the root
# conftest, running this file directly gets it from here.  Inserted rather than
# appended for the same reason conftest does it -- the repo root is also on the
# path and also contains a directory called 'caper'.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_REPO_ROOT, 'caper') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'caper'))

from bson import ObjectId                                  # noqa: E402

from caper.project_status import (                         # noqa: E402
    DETACHED, LIVE, SOFT_DELETED, SUPERSEDED, TOMBSTONE, status_flags,
)

# Stamped on every document and every GridFS file this module writes.  Purging
# selects on it, so nothing that was not seeded here can be removed by --purge
# even if a name collides.
MARKER = 'awkward_fixture'

# Placeholders the seeder replaces with real GridFS ids.  Kept as sentinels in
# the catalogue so a shape can say "this one holds a payload" without the
# catalogue needing a database handle to say it.
PAYLOAD = '<<payload>>'
"""A GridFS file that exists.  Substituted with the id of a file actually
written, so ``tarfile`` names something ``fs.files`` can resolve."""

MISSING_PAYLOAD = '<<missing-payload>>'
"""A GridFS id with no ``fs.files`` row -- a document naming bytes that are
gone.  Production has none, and the point of the fixture is to keep it that
way: it is the shape that proves the check can fail."""

# Fixed dates so history ordering is deterministic.  Format matches get_date().
_DATES = ['2024-01-15 09:00:00', '2024-03-02 09:00:00',
          '2024-06-11 09:00:00', '2024-09-30 09:00:00']


class Shape:
    """One awkward state: the documents that make it, and what they mean.

    ``build`` receives a function that mints ObjectIds and returns an ordered
    ``[(key, document)]``.  Keys are local to the shape and are what
    ``expect``/``violates`` refer to, so a shape can talk about "the head" and
    "the orphan" without knowing what ids it will be given.
    """

    def __init__(self, name, mirrors, build, expect, why, violates=()):
        self.name = name
        self.mirrors = mirrors
        self.build = build
        self.expect = expect
        self.why = why
        self.violates = violates

    def __repr__(self):
        return f'<Shape {self.name}>'


def _document(name, flags, **extra):
    """A project document with the fields every consumer assumes are present.

    Deliberately small: no ``runs``, because the awkward states are all about
    the status and lineage fields, and a realistic 690 KB document would make
    the fixtures slow to build and hard to read.  Shapes that need a
    payload say so with ``PAYLOAD``.
    """
    doc = {
        'project_name': name,
        'description': f'awkward-state fixture: {name}',
        'private': 'private',
        'project_members': ['pytest_test_user@example.com'],
        # Both, because both exist in the wild: finished documents carry
        # 'creator', the placeholder written at the start of aggregation
        # carries 'owner' (views.py:3796). Whether they are chain-level or
        # version-level -- which of them a promotion should carry forward --
        # is an open question, and nothing here settles it.
        'creator': 'pytest_test_user@example.com',
        'owner': 'pytest_test_user',
        'date': _DATES[0],
        'date_created': _DATES[0],
        'sample_count': 1,
        'views': 0,
        'downloads': 0,
        'FINISHED?': True,
        MARKER: True,
    }
    doc.update(flags)
    doc.update(extra)
    return doc


def _history_entry(doc_id, date):
    """One ``previous_versions[]`` entry, in the shape the write paths produce.

    ``views.py:3748`` and ``views_apis.py:316`` both build this literal; the
    keys here are their intersection.  ``linkid`` is a string, which is why
    the lineage queries compare against ``str(_id)``.  It is a display alias,
    not a foreign key: nothing enforces that it names anything.
    """
    return {
        'date': date,
        'linkid': str(doc_id),
        'ASP_version': 'NA',
        'AA_version': 'NA',
        'AC_version': 'NA',
        'aggregator_version': 'NA',
    }


# ---------------------------------------------------------------------------
# The catalogue: the eight states production actually contains, plus the four
# the validator needs in order to have anything to find.
#
# The population counts in `mirrors` were measured in August 2026 and will
# drift.  They are there to say which shapes are common and which are rare, not
# to be accurate.
# ---------------------------------------------------------------------------

def _superseded_referenced(mint):
    old, new = mint(), mint()
    return [
        ('prior', _document('AwkwardChain_Referenced', status_flags(SUPERSEDED),
                            _id=old, date=_DATES[0], tarfile=PAYLOAD)),
        ('head', _document('AwkwardChain_Referenced', status_flags(LIVE),
                           _id=new, date=_DATES[1], tarfile=PAYLOAD,
                           previous_versions=[_history_entry(old, _DATES[0])])),
    ]


def _superseded_unreferenced(mint):
    return [
        ('orphan', _document('AwkwardChain_Unreferenced', status_flags(SUPERSEDED),
                             _id=mint(), tarfile=PAYLOAD)),
    ]


def _detached_no_current(mint):
    # 'current' is absent rather than False, which is the whole shape: no
    # equality query can reach it and no $set has ever written it.
    return [
        ('doc', _document('Awkward_NoCurrentField', {'delete': True},
                          _id=mint(), tarfile=PAYLOAD)),
    ]


def _detached_both_false(mint):
    return [
        ('doc', _document('Awkward_BothFalse', {'delete': False, 'current': False},
                          _id=mint(), tarfile=PAYLOAD)),
    ]


def _tombstone_triple(mint):
    old, new = mint(), mint()
    return [
        ('tombstone', _document('AwkwardChain_Tombstoned', status_flags(TOMBSTONE),
                                _id=old, date=_DATES[0],
                                redirect_to_project=str(new),
                                delete_user='pytest_test_user',
                                delete_date=_DATES[1])),
        ('head', _document('AwkwardChain_Tombstoned', status_flags(LIVE),
                           _id=new, date=_DATES[1], tarfile=PAYLOAD)),
    ]


def _dangling_lineage(mint):
    head = mint()
    gone = ObjectId()                 # minted but never inserted
    return [
        ('head', _document('AwkwardChain_Dangling', status_flags(LIVE),
                           _id=head, date=_DATES[1], tarfile=PAYLOAD,
                           previous_versions=[_history_entry(gone, _DATES[0])])),
    ]


def _legacy_json_lineage(mint):
    """The encoding used before April 2024, in both of its variants.

    Verbatim shapes from the five dev documents that still carry it -- a JSON
    string rather than a document, keyed ``link`` rather than ``linkid``, and
    wrapped in a list in three cases and not in the other two.  The referrers
    here are SUPERSEDED rather than DETACHED because that is the reachable
    case: a SUPERSEDED document renders its own page, so this is the shape a
    person can actually arrive at.
    """
    list_head, list_prior = mint(), mint()
    bare_head, bare_prior = mint(), mint()
    return [
        ('list_prior', _document('AwkwardChain_LegacyList',
                                 {'delete': False, 'current': False},
                                 _id=list_prior, date=_DATES[0], tarfile=PAYLOAD)),
        ('list_head', _document(
            'AwkwardChain_LegacyList', status_flags(SUPERSEDED),
            _id=list_head, date=_DATES[1], tarfile=PAYLOAD,
            previous_versions=[json.dumps(
                [{'date': _DATES[0], 'link': str(list_prior)}])])),

        ('bare_prior', _document('AwkwardChain_LegacyBare',
                                 {'delete': False, 'current': False},
                                 _id=bare_prior, date=_DATES[0], tarfile=PAYLOAD)),
        ('bare_head', _document(
            'AwkwardChain_LegacyBare', status_flags(SUPERSEDED),
            _id=bare_head, date=_DATES[1], tarfile=PAYLOAD,
            previous_versions=[json.dumps(
                {'date': _DATES[0], 'link': str(bare_prior)})])),
    ]


def _live_also_history(mint):
    live, head = mint(), mint()
    return [
        ('live', _document('Awkward_LiveAndHistory', status_flags(LIVE),
                           _id=live, date=_DATES[0], tarfile=PAYLOAD)),
        ('head', _document('AwkwardChain_ClaimsLiveDoc', status_flags(LIVE),
                           _id=head, date=_DATES[1], tarfile=PAYLOAD,
                           previous_versions=[_history_entry(live, _DATES[0])])),
    ]


def _name_collision(mint):
    return [
        ('detached', _document('Awkward_NameCollision',
                               {'delete': False, 'current': False},
                               _id=mint(), date=_DATES[0], tarfile=PAYLOAD)),
        ('live', _document('Awkward_NameCollision', status_flags(LIVE),
                           _id=mint(), date=_DATES[1], tarfile=PAYLOAD)),
    ]


def _partial_tombstone(mint):
    return [
        ('doc', _document('Awkward_PartialTombstone',
                          dict(status_flags(SUPERSEDED),
                               version_deleted_from_history=True),
                          _id=mint(), tarfile=PAYLOAD)),
    ]


def _purged_but_still_holding(mint):
    return [
        ('doc', _document('Awkward_PurgedButHolding', status_flags(TOMBSTONE),
                          _id=mint(), tarfile=PAYLOAD,
                          redirect_to_project=str(ObjectId()))),
    ]


def _missing_payload(mint):
    return [
        ('doc', _document('Awkward_MissingPayload', status_flags(LIVE),
                          _id=mint(), tarfile=MISSING_PAYLOAD)),
    ]


def _no_delete_field(mint):
    doc = _document('Awkward_NoDeleteField', {'current': True}, _id=mint(),
                    tarfile=PAYLOAD)
    return [('doc', doc)]


def _healthy(mint):
    return [
        ('live', _document('Awkward_PlainLive', status_flags(LIVE),
                           _id=mint(), tarfile=PAYLOAD)),
        ('soft_deleted', _document('Awkward_SoftDeleted', status_flags(SOFT_DELETED),
                                   _id=mint(), tarfile=PAYLOAD)),
    ]


SHAPES = (
    Shape(
        name='superseded_referenced',
        mirrors='89 prod documents',
        build=_superseded_referenced,
        expect={'prior': SUPERSEDED, 'head': LIVE},
        why="An earlier version of a live chain. Reachable by URL through the "
            "resolver's fourth and fifth steps, payload retained. This is the "
            "class cleanup_orphaned_projects.py deleted."),

    Shape(
        name='superseded_unreferenced',
        mirrors='14 prod documents',
        build=_superseded_unreferenced,
        expect={'orphan': SUPERSEDED},
        why="Carries the flags of a prior version but no live document lists "
            "it. Reachable by URL all the same, which is why 'nothing points "
            "at it' is not a licence to delete it."),

    Shape(
        name='detached_no_current_field',
        mirrors='70 prod documents',
        build=_detached_no_current,
        expect={'doc': DETACHED},
        why="delete=True and no 'current' field at all. It looks soft-deleted "
            "and gets described that way, but classify() cannot agree: "
            "{'current': False} does not match a missing field, so the "
            "document matches no status rule and is DETACHED. All 70 on "
            "production still hold a tarfile."),

    Shape(
        name='detached_both_false',
        mirrors='39 prod documents',
        build=_detached_both_false,
        expect={'doc': DETACHED},
        why="delete=False, current=False. The schema cannot say "
            "whether it is a draft, an abandoned upload or an unlinked "
            "predecessor. Reachable by URL through the resolver's first step."),

    Shape(
        name='tombstone_triple',
        mirrors='2 prod documents',
        build=_tombstone_triple,
        expect={'tombstone': TOMBSTONE, 'head': LIVE},
        why="Version removed from history and payload purged, retained so the "
            "old URL redirects. Both markers present, no GridFS ids left."),

    Shape(
        name='dangling_lineage_reference',
        mirrors='2 prod / 1 dev document',
        build=_dangling_lineage,
        expect={'head': LIVE},
        violates=(('I6', 'head'),),
        why="A live head whose previous_versions[] names a document that is "
            "not in the collection. History rendering silently drops the "
            "entry, so the chain looks shorter than it was."),

    Shape(
        name='legacy_json_lineage_entry',
        mirrors='5 dev documents (3 list-wrapped, 2 bare)',
        build=_legacy_json_lineage,
        expect={'list_head': SUPERSEDED, 'list_prior': DETACHED,
                'bare_head': SUPERSEDED, 'bare_prior': DETACHED},
        violates=(('I19', 'list_head'), ('I19', 'bare_head')),
        why="previous_versions[] holds a JSON *string* keyed 'link', the "
            "format written before April 2024. The reference is intact -- the "
            "document it names is right there -- but nothing reads it: the "
            "history table renders a link to /project/[{\"date\": ...}] and "
            "{'previous_versions.linkid': id} matches nothing, so both "
            "documents look unreferenced to every caller that asks that way. "
            "Not a dangling reference, which is why it gets its own invariant."),

    Shape(
        name='live_also_referenced_as_history',
        mirrors='3 dev documents',
        build=_live_also_history,
        expect={'live': LIVE, 'head': LIVE},
        violates=(('I7', 'live'),),
        why="One document is the live head of its own chain and is also listed "
            "in another chain's history. Both pages render it; "
            "promotion or deletion from either side corrupts the other."),

    Shape(
        name='name_collision_detached_vs_live',
        mirrors='12 prod documents',
        build=_name_collision,
        expect={'detached': DETACHED, 'live': LIVE},
        why="A detached document shares project_name with a live project. "
            "Resolving by name returns whichever the first step matches -- "
            "here the detached one, since it carries delete=False. Lineage must "
            "not be inferred from this: a name match is not evidence."),

    Shape(
        name='partial_tombstone',
        mirrors='0 prod documents (latent)',
        build=_partial_tombstone,
        expect={'doc': SUPERSEDED},
        why="version_deleted_from_history without payload_purged: the state "
            "delete_project_version() leaves when it removes the sole version "
            "of a project. Its log says 'project fully removed' while the "
            "whole GridFS payload is still stored and still billed. Not a "
            "tombstone -- PARTIAL_TOMBSTONE_QUERY exists to count it."),

    Shape(
        name='purged_but_still_holding_payload',
        mirrors='0 prod documents',
        build=_purged_but_still_holding,
        expect={'doc': TOMBSTONE},
        violates=(('I8', 'doc'), ('I14', 'doc')),
        why="Claims payload_purged=True while still naming a GridFS file. "
            "Nothing produces this today; it exists so I8 and I14 have "
            "something to fail on, because an invariant that has never been "
            "seen to fail has not been tested."),

    Shape(
        name='reference_to_missing_gridfs_file',
        mirrors='0 prod documents (I12 -- keep it that way)',
        build=_missing_payload,
        expect={'doc': LIVE},
        violates=(('I12', 'doc'),),
        why="A live document naming a tarfile with no fs.files row: a project "
            "whose download button 500s. Prod has none, which is the only "
            "invariant here that is currently clean and worth keeping so."),

    Shape(
        name='no_delete_field',
        mirrors='0 prod documents',
        build=_no_delete_field,
        expect={'doc': DETACHED},
        why="No 'delete' field at all. Not a state production holds, but "
            "nothing in the schema forbids it and every query has to have an "
            "answer for it -- {'delete': False} does not match, and neither "
            "does {'delete': True}, so it falls to DETACHED."),

    Shape(
        name='healthy_baseline',
        mirrors='the ordinary case',
        build=_healthy,
        expect={'live': LIVE, 'soft_deleted': SOFT_DELETED},
        why="A plain live project and a genuine soft-delete (delete=True, "
            "current=True), so that all five statuses are represented and a "
            "check that flags everything is caught flagging these too."),
)

SHAPES_BY_NAME = {shape.name: shape for shape in SHAPES}


# ---------------------------------------------------------------------------
# Building and seeding
# ---------------------------------------------------------------------------

class Plan:
    """What ``build_all()`` produced: documents, and how to read them back.

    ``ids[(shape, key)]`` is the ObjectId a shape's document was given, so a
    test can say ``plan.id_of('dangling_lineage_reference', 'head')`` and get
    the document the ``violates`` entry is talking about.
    """

    def __init__(self, documents, ids, expected, violations, keys):
        self.documents = documents
        self.ids = ids
        self.expected = expected
        self.violations = violations
        self.keys = keys

    def id_of(self, shape_name, key):
        return self.ids[(shape_name, key)]

    def __len__(self):
        return len(self.documents)


def build_all(shapes=SHAPES):
    """Build every shape's documents without touching a database.

    Pure, so the catalogue can be checked -- names unique, keys consistent,
    every ``violates`` naming a key that exists -- before anything is written.
    """
    documents, ids, expected, violations, shape_keys = [], {}, {}, [], {}

    for shape in shapes:
        built = shape.build(ObjectId)
        keys = [key for key, _doc in built]
        shape_keys[shape.name] = keys
        if len(set(keys)) != len(keys):
            raise ValueError(f"{shape.name}: duplicate document keys {keys}")
        if set(shape.expect) - set(keys):
            raise ValueError(
                f"{shape.name}: expect names {sorted(set(shape.expect) - set(keys))}, "
                f"which the shape does not build")

        for key, doc in built:
            ids[(shape.name, key)] = doc['_id']
            documents.append(doc)
            if key in shape.expect:
                expected[doc['_id']] = shape.expect[key]

        for invariant, key in shape.violates:
            if key not in keys:
                raise ValueError(
                    f"{shape.name}: violates names key {key!r}, which the shape "
                    f"does not build")
            violations.append((invariant, shape.name, ids[(shape.name, key)]))

    return Plan(documents, ids, expected, violations, shape_keys)


def _substitute_payloads(doc, write_file):
    """Replace the payload sentinels with GridFS ids, recursively."""
    if isinstance(doc, dict):
        return {key: _substitute_payloads(value, write_file)
                for key, value in doc.items()}
    if isinstance(doc, list):
        return [_substitute_payloads(value, write_file) for value in doc]
    if doc == PAYLOAD:
        return str(write_file())
    if doc == MISSING_PAYLOAD:
        # An id shaped like every other one, naming nothing.  Never written, so
        # there is no file to leak and nothing to clean up.
        return str(ObjectId())
    return doc


def documents_with_plain_ids(plan):
    """*plan*'s documents with the payload sentinels replaced by bare ObjectIds.

    For consumers that need documents rather than files: the query tests in
    ``tests/test_project_status.py`` care what ``tarfile`` looks like to a
    filter, not whether the bytes behind it exist, and writing a GridFS file per
    fixture to satisfy them would be slow and beside the point.  The result is
    safe to insert into a scratch collection.
    """
    return [_substitute_payloads(doc, ObjectId) for doc in plan.documents]


def seed(db, plan):
    """Insert *plan*'s documents, writing a real GridFS file for each PAYLOAD.

    Returns the number of documents inserted.  Idempotent only in the sense
    that ``purge`` reverses it exactly; running it twice seeds two batches,
    which is occasionally what you want and never what you get by accident,
    since the ids are minted fresh each time.
    """
    import gridfs

    fs = gridfs.GridFS(db)

    def write_file():
        return fs.put(b'awkward-state fixture payload, not a real tarball\n',
                      filename='awkward_fixture.tar.gz',
                      metadata={MARKER: True})

    documents = [_substitute_payloads(doc, write_file) for doc in plan.documents]
    db['projects'].insert_many(documents)
    return len(documents)


def purge(db):
    """Remove every document and GridFS file this module wrote.

    Selects on ``MARKER`` alone, so it cannot reach a document it did not
    create even if one shares a fixture's project name.  Returns
    ``(documents, files)``.
    """
    import gridfs

    fs = gridfs.GridFS(db)
    files = 0
    for row in db['fs.files'].find({f'metadata.{MARKER}': True}, {'_id': 1}):
        fs.delete(row['_id'])
        files += 1
    removed = db['projects'].delete_many({MARKER: True}).deleted_count
    return removed, files


# ---------------------------------------------------------------------------
# Target guard
# ---------------------------------------------------------------------------

def _assert_local(uri, db_name):
    """Refuse to write anywhere but a mongo on this machine.

    This module is the only part of this work that writes anything, and dev
    and production are two databases on one DocumentDB cluster reached with
    credentials that differ by one environment variable.  A locality check
    is the one guard that cannot be satisfied by a mistake: a seeder aimed at
    a shared cluster writes fixture documents into a database real people read,
    and 'Awkward_NameCollision' would then be a real project on a real site.

    There is no override flag on purpose.  Seeding dev is a deliberate act that
    should be written deliberately, not enabled by a flag someone finds in
    --help at the wrong moment.
    """
    from pymongo import uri_parser

    hosts = uri_parser.parse_uri(uri)['nodelist']
    local = all(host in ('localhost', '127.0.0.1', 'mongodb', '::1')
                for host, _port in hosts)
    if not local:
        raise SystemExit(
            f"refusing to write to database {db_name!r}: it is not on a local "
            f"mongo, and this seeder only writes to a local one. See "
            f"_assert_local in {__file__}.")
    return f'{db_name} (local)'


def _connect():
    import pymongo

    uri = os.environ.get('DB_URI_SECRET')
    db_name = os.environ.get('DB_NAME')
    if not uri or not db_name:
        raise SystemExit(
            "DB_URI_SECRET and DB_NAME must be set. Run:\n"
            "    set -a; source caper/config.sh; set +a")
    label = _assert_local(uri, db_name)
    return pymongo.MongoClient(uri)[db_name], label


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(
        description="Seed the awkward project states into the local database.")
    parser.add_argument('--execute', action='store_true',
                        help='actually write the documents (default: report only)')
    parser.add_argument('--purge', action='store_true',
                        help='remove every document this module previously wrote')
    parser.add_argument('--only', action='append', metavar='SHAPE',
                        help='build only this shape (repeatable)')
    args = parser.parse_args(argv)

    shapes = SHAPES
    if args.only:
        unknown = sorted(set(args.only) - set(SHAPES_BY_NAME))
        if unknown:
            raise SystemExit(f"unknown shape(s): {', '.join(unknown)}\n"
                             f"known: {', '.join(SHAPES_BY_NAME)}")
        shapes = tuple(SHAPES_BY_NAME[name] for name in args.only)

    if args.purge:
        db, label = _connect()
        documents, files = purge(db)
        print(f'{label}: removed {documents} document(s) and {files} GridFS file(s)')
        return 0

    plan = build_all(shapes)

    print(f'{len(plan)} documents in {len(shapes)} shapes\n')
    for shape in shapes:
        print(f'  {shape.name}   [{shape.mirrors}]')
        for key in plan.keys[shape.name]:
            status = shape.expect.get(key, '(unclassified)')
            broken = [inv for inv, k in shape.violates if k == key]
            note = f'   violates {", ".join(broken)}' if broken else ''
            print(f'      {key:<14} {status}{note}')
    print()

    if not args.execute:
        print('report only. Pass --execute to write these to the local database.')
        return 0

    db, label = _connect()
    written = seed(db, plan)
    print(f'{label}: inserted {written} document(s)')
    print(f'to remove them again: python tests/awkward_states.py --purge')
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
