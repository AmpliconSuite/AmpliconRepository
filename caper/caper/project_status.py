"""
One authority for "what is this project document".

Every consumer used to answer that question itself.  The audit behind
``docs/project-version-history-and-provenance-spec.md`` found 72 call sites
asking it in 21 distinct query shapes, and both production incidents it
records were the same failure: **a predicate that lives in the application was
re-derived somewhere else, and drifted.**

  * ``cleanup_orphaned_projects.py`` classified 84 of 345 production documents
    as orphaned.  14 of them resolved by URL at that moment and 77 still held a
    GridFS payload.  The script's protection rules covered ``delete=False`` and
    ``delete=True AND current=True`` but not ``delete=True AND current=False``,
    which ``get_one_project()`` falls back to twice.
  * ``purge-local-db.py`` kept a hand-written GridFS key set that had drifted
    8 keys behind the application, which made 80,170 live files look like
    garbage.

This module exists so that predicate lives in exactly one place.  It provides
the same rule in three forms, and a test proves all three agree:

  ``classify(doc)``      in-memory, for code holding a loaded document
  ``STATUS_QUERIES``     Mongo filters, for code that must select in the database
  ``matches(doc, q)``    a restricted evaluator, for code holding a document but
                         wanting one of the legacy filters below

No schema change comes with this module.  It reads the fields that exist today
(``delete``, ``current``, ``version_deleted_from_history``, ``payload_purged``)
and reports what they mean.  When a stored ``status`` field is added, it gets
added here and a validator asserts stored == ``classify(doc)``.


The three traps
---------------

**1. ``delete: True`` does not mean deleted.**  It means "not the live
document".  103 production documents are in that state and 89 of them are
referenced as another project's history.  ``get_one_project()``
(``utils.py:683``) resolves them by ``_id`` at line 722 and by ``project_name``
at line 736, so they are reachable by URL today.  That is ``SUPERSEDED``, and
deleting one destroys a live link and its payload.

**2. Field absence is load-bearing.**  ``{'current': False}`` does not match a
document that has no ``current`` field, and 70 production documents have none
— all 70 still holding a tarfile.  Every query here decides that deliberately,
and ``classify()`` decides it the same way.  Comparisons use ``is True`` /
``is False`` rather than ``==`` because that is what MongoDB does: ``{'x':
False}`` matches a BSON boolean false and nothing else, while Python's ``==``
would also accept ``0``.

**3. ``linkid`` is not a foreign key.**  ``prepare_project_linkid()``
(``utils.py:1238``) is a one-line display alias for templates.  Nothing filters
on top-level ``linkid``, and nothing here treats it as an identity.


Status and reachability are independent axes
--------------------------------------------

``classify()`` says what a document *means*; ``is_reachable_by_url()`` says
whether the application can still serve it.  Today those disagree for a real
population: the 39 documents that are ``delete=False, current=False`` are
``DETACHED`` — the schema cannot say whether they are drafts, abandoned
uploads or unlinked predecessors — and they are reachable by URL all the same.
The intended end state is one where ``DETACHED`` means "reachable by
nothing".  Getting there is a migration, not a classification, and it is
deliberately not part of this change.  Never infer one axis from the
other; ask the function you actually mean.
"""

# ---------------------------------------------------------------------------
# The five statuses
# ---------------------------------------------------------------------------

LIVE = 'LIVE'
"""Current head of a chain, not deleted.  Reachable by URL, payload retained."""

SUPERSEDED = 'SUPERSEDED'
"""An earlier version of a chain.  Reachable by URL -- old links must keep
working -- and payload retained.  This is the class that cleanup deleted."""

SOFT_DELETED = 'SOFT_DELETED'
"""User-deleted, recoverable from the admin page.  Not reachable by URL;
payload retained."""

TOMBSTONE = 'TOMBSTONE'
"""Version removed from history and payload purged.  Its URL still resolves,
to a redirect."""

DETACHED = 'DETACHED'
"""None of the above: a document whose meaning cannot be determined from the
schema.  109 of 345 production documents are in this state -- 70 that are
``delete=True`` with no ``current`` field, and 39 that are ``delete=False,
current=False``.  It is a named state rather than "everything else" so that
population is countable and reviewable instead of invisible.

Deciding the fate of those documents is a human call, and nothing here makes
it."""

ALL_STATUSES = (LIVE, SUPERSEDED, SOFT_DELETED, TOMBSTONE, DETACHED)


# ---------------------------------------------------------------------------
# The one table everything below is derived from
# ---------------------------------------------------------------------------

# The legacy (delete, current) flag pair each status carries.  Everything in
# this module -- the classifier, the Mongo filters, the values written on a
# status change -- is derived from this table and from _TOMBSTONE_MARKERS, so
# there is no second copy to drift.  A status is added here or not at all.
_STATUS_FLAGS = {
    LIVE:         {'delete': False, 'current': True},
    SUPERSEDED:   {'delete': True,  'current': False},
    SOFT_DELETED: {'delete': True,  'current': True},
}

# What makes a document a tombstone.  Both markers are required, matching the
# definition of a tombstone -- version removed from history, payload purged --
# and the protection rule already in cleanup_orphaned_projects.py.
#
# 'redirect_to_project' is deliberately NOT required: transitions T7 and T8
# produce a tombstone with nowhere to redirect to, and demanding the field
# would misclassify those the moment they exist.
#
# A document carrying 'version_deleted_from_history' WITHOUT 'payload_purged'
# is a partial state: removed from history but still holding its whole GridFS
# payload, and still resolvable.  It is not a tombstone, and treating it as one
# would invite a payload deletion that has already happened.  It falls through
# to the flag rules below -- SUPERSEDED, which retains everything -- and
# PARTIAL_TOMBSTONE_QUERY exists to count it.  0 documents on prod, measured
# 2026-08-27.
#
# The one code path that used to produce it -- deleting a project's only
# version -- now writes a complete tombstone through
# build_deleted_version_tombstone() like every other deletion.  The query stays:
# documents outlive the code that wrote them, and hand-repair is still a thing
# that happens.
_TOMBSTONE_MARKERS = {'version_deleted_from_history': True, 'payload_purged': True}

# For callers that need to clear or project the markers rather than test them.
# Named from the same dict the predicate reads, so a third marker would reach
# every one of them at once.
TOMBSTONE_MARKER_FIELDS = tuple(_TOMBSTONE_MARKERS)

# The two flag fields, for callers that need to name them without asserting a
# value -- a projection that fetches them so classify() has something to read,
# for one.  Derived from _STATUS_FLAGS for the same reason as above, and it
# keeps 'delete'/'current' from being spelled by hand outside this module,
# which is what the grep guard in tests/test_project_status_guard.py enforces.
STATUS_FLAG_FIELDS = tuple(dict.fromkeys(
    field for flags in _STATUS_FLAGS.values() for field in flags))


def _flag_matches(doc, field, expected):
    """Mirror MongoDB equality for the boolean flags, including absence.

    ``{'delete': False}`` matches a BSON boolean false and nothing else -- not
    a missing field, not ``0``.  ``doc.get(field) == expected`` would accept
    ``0`` for ``False``; ``is`` does not.
    """
    return doc.get(field) is expected


def _matches_flags(doc, flags):
    return all(_flag_matches(doc, field, expected) for field, expected in flags.items())


def is_tombstone(doc):
    """True when *doc* carries both tombstone markers.  See _TOMBSTONE_MARKERS."""
    return _matches_flags(doc, _TOMBSTONE_MARKERS)


def is_empty_project(doc):
    """True when *doc* has no sample results to show.

    Document-level emptiness, which is not the chain-level EMPTY of T6: a
    project is EMPTY when every member of its chain is a tombstone, and this
    says only that *this* version has nothing to render.  The two coincide on
    the version a visitor lands on after the last one is deleted, which is how
    an emptied project gets an empty page instead of a broken one.

    One function because it was two.  project_page() asked for the 'EMPTY?'
    flag *or* absent runs; edit_project_page() asked only for the flag.  A
    tombstone has no runs and no flag, so the first said empty and the second
    said not-empty and then read a field the document does not carry -- the
    same predicate spelled twice, disagreeing on the state that terminal
    deletion had just made reachable.
    """
    return bool(doc.get('EMPTY?')) or not doc.get('runs')


def classify(doc):
    """Return the status of a loaded project document.

    Pure and document-local: it reads no other document and issues no query, so
    it says nothing about whether *doc* is referenced as another project's
    history.  In the target model ``SUPERSEDED`` is defined by chain membership;
    until lineage pointers exist there is nothing on the document to read, so
    the flags are the whole story.  ``STATUS_QUERIES`` encodes the
    same rule, and a test asserts the two agree over every document in the
    database.
    """
    if is_tombstone(doc):
        return TOMBSTONE
    for status, flags in _STATUS_FLAGS.items():
        if _matches_flags(doc, flags):
            return status
    return DETACHED


# ---------------------------------------------------------------------------
# The same rule as Mongo filters
# ---------------------------------------------------------------------------

def _not_tombstone():
    """The filter form of ``not is_tombstone(doc)``.

    ``$nor`` because the negation of "both markers set" is "at least one is
    not", which no single-field operator expresses.  It costs nothing here:
    the collection is 345 documents, and MongoDB still uses an index for the
    equality terms it is combined with, applying this as a residual filter.
    """
    return {'$nor': [dict(_TOMBSTONE_MARKERS)]}


STATUS_QUERIES = {
    TOMBSTONE: dict(_TOMBSTONE_MARKERS),
    # Everything the four above do not match.  Written as the complement rather
    # than as its own condition so that the five queries partition the
    # collection by construction -- no document can fall in two, and none can
    # fall in none.
    DETACHED: {'$nor': [dict(_TOMBSTONE_MARKERS)] +
                       [dict(flags) for flags in _STATUS_FLAGS.values()]},
}
for _status, _flags in _STATUS_FLAGS.items():
    STATUS_QUERIES[_status] = dict(_flags)
    STATUS_QUERIES[_status].update(_not_tombstone())
del _status, _flags

# STATUS_QUERIES holds shared dicts: pass one straight to find() when it is the
# whole filter, but never mutate one, and use status_query() to add constraints
# rather than {**STATUS_QUERIES[LIVE], ...} -- the spread silently drops a term
# when a key collides, which is what combine() exists to prevent.


# ---------------------------------------------------------------------------
# Legacy predicates that are not a single status
# ---------------------------------------------------------------------------
# Call sites need these because they encode decisions the codebase has already
# made.  They are exported so that those decisions are stated once, with their
# consequences written down, instead of being re-typed as a literal at each of
# the places that needs them.

NOT_DELETED_QUERY = {'delete': _STATUS_FLAGS[LIVE]['delete']}
"""``{'delete': False}`` -- the gate on the resolver's first three steps
(``utils.py:692``, ``:703``, ``:711``).

This is **not** "not deleted" in the intuitive sense.  It matches ``LIVE``
plus every ``DETACHED`` document that carries ``delete=False`` -- 39 on prod,
23 of them holding a tarfile -- and it misses ``SUPERSEDED``, which is
reachable too."""

PRIOR_VERSION_QUERY = dict(_STATUS_FLAGS[SUPERSEDED])
"""``{'delete': True, 'current': False}`` -- the resolver's fallback
(``utils.py:722`` and ``:736``, logging "had to use previous project ids!").

Matches a prior version of a chain whether it still holds its payload
(``SUPERSEDED``) or has been reduced to a redirect (``TOMBSTONE``).
Deliberately not ``STATUS_QUERIES[SUPERSEDED]``: excluding tombstones here
would stop deleted-version URLs redirecting, which is the one thing tombstones
exist to do."""

DELETE_FLAG_QUERY = {'delete': _STATUS_FLAGS[SOFT_DELETED]['delete']}
"""``{'delete': True}`` -- everything not live, used by
``get_one_deleted_project()`` (``utils.py:762``).  ``SOFT_DELETED``,
``SUPERSEDED`` and ``TOMBSTONE`` together, plus the 70 ``DETACHED`` documents
with no ``current`` field."""

MISSING_CURRENT_QUERY = dict(DELETE_FLAG_QUERY, current={'$exists': False})
"""``{'delete': True, 'current': {'$exists': False}}`` -- the documents that
carry the ``delete`` half of the pair and have never had the other half.

The shape the delete path leaves on a project whose document predates commit
``951eb25`` (2024-04-10), which introduced the ``current`` flag: 70 on prod, 51
on dev when measured on 2026-08-27.  It is not a dead era -- one was deleted in
2025 -- because it is what deleting an old project still produces today.

``{'current': False}`` does not match a document with no ``current`` field, so
these are ``DETACHED``: invisible to ``STATUS_QUERIES[SOFT_DELETED]`` and
therefore to the admin restore page, and missed by ``HEAD_VERSION_QUERY``.
Used by ``backfill_project_status.py`` to find them; nothing in the application
should need it, and its population should only ever shrink."""

HEAD_VERSION_QUERY = {'current': _STATUS_FLAGS[LIVE]['current']}
"""``{'current': True}`` -- the head of a chain regardless of delete state, so
``LIVE`` and ``SOFT_DELETED`` together.

Misses the 70 documents that have no ``current`` field at all.  Account
deletion uses this deliberately: a soft-deleted project's membership still has
to be scrubbed."""

PARTIAL_TOMBSTONE_QUERY = {
    'version_deleted_from_history': True,
    'payload_purged': {'$ne': True},
}
"""Documents removed from history whose payload was never purged -- the state
``delete_project_version()`` leaves when it deletes the sole version of a
project (``views.py:3012``).  Its log line says "project fully
removed"; the document stays resolvable through ``utils.py:722`` and its whole
GridFS payload is still stored and still billed.

0 documents on prod, so the path is latent rather than damaging.  Exported so
it stays countable.  Fixing it means routing every deletion path through one
tombstone-creation routine, which is a change to write paths."""


def status_query(status, *extra, **fields):
    """``STATUS_QUERIES[status]`` combined with additional constraints.

    >>> status_query(LIVE, private={'$in': [False, 'public']})
    """
    if status not in STATUS_QUERIES:
        raise ValueError(f"Unknown project status: {status!r}")
    return combine(STATUS_QUERIES[status], *extra, **fields)


def combine(*queries, **fields):
    """Merge query fragments without losing a constraint.

    Plain ``{**a, **b}`` silently drops one side when both touch the same key,
    and ``$nor`` sits on four of the five status queries, so that collision is
    not hypothetical -- it is one keystroke away from turning a status filter
    back into the bare flag test this module exists to replace.  When keys
    collide the fragments go under ``$and``, which is always correct.
    """
    parts = [dict(query) for query in queries if query]
    if fields:
        parts.append(dict(fields))
    if not parts:
        return {}

    merged = {}
    for part in parts:
        if any(key in merged for key in part):
            return {'$and': parts}
        merged.update(part)
    return merged


def status_flags(status):
    """The field values that put a document *into* *status*.

    For ``$set`` payloads and for the document literals that create a project,
    so that a status change is written from the same table it is read from.
    A stored ``status`` field, when there is one, gets added here -- once.

    ``DETACHED`` is not writable.  It is a description of documents whose
    meaning was lost, not a state anything should deliberately create.
    """
    if status == TOMBSTONE:
        return dict(_STATUS_FLAGS[SUPERSEDED], status=TOMBSTONE, **_TOMBSTONE_MARKERS)
    if status in _STATUS_FLAGS:
        return dict(_STATUS_FLAGS[status], status=status)
    if status == DETACHED:
        raise ValueError(
            "DETACHED is a diagnosis, not a state to write. A document becomes "
            "DETACHED by losing the flags that gave it meaning.")
    raise ValueError(f"Unknown project status: {status!r}")


def status_after(doc, **changes):
    """The status *doc* would have once *changes* are applied to it.

    For the write sites that set half the flag pair on purpose -- a soft delete
    sets 'delete' and leaves 'current' alone, a restore clears 'delete' and
    leaves 'current' alone -- where the resulting status depends on the value
    already on the document and so cannot come from status_flags() alone.

    Without this, the stored 'status' field goes stale the first time anyone
    deletes a project, and a stored field that lies is worse than an absent
    one: every reader that trusts it is now wrong, and the validator's I2 turns
    from an invariant into a permanent finding.

        >>> new_val = {'$set': {'delete': True,
        ...                     'status': status_after(project, delete=True)}}
    """
    return classify({**doc, **changes})


# ---------------------------------------------------------------------------
# Reachability -- mirrors get_one_project()
# ---------------------------------------------------------------------------

def is_reachable_by_url(doc):
    """True when ``get_one_project()`` can still return *doc* for its own id.

    Mirrors the resolver's five steps (``utils.py:692``, ``:703``, ``:711``,
    ``:722``, ``:736``).  Only the two ``_id`` steps can decide the question
    for a specific document: the three name steps return whichever document
    happens to carry that name, and a live project sharing a name says nothing
    about this one.  That loses no reachability, because a document the name
    steps would return already passes an ``_id`` step -- step 3 needs
    ``delete=False``, which step 1 also matches, and step 5 needs the same
    flags as step 4.  So the two ``_id`` steps are exactly the answer, not a
    conservative approximation of it.

    Independent of ``classify()``.  A ``DETACHED`` document can be reachable
    (39 on prod are), and a ``SOFT_DELETED`` one is not.  See the module
    docstring.
    """
    return matches(doc, REACHABLE_BY_URL_QUERY)


REACHABLE_BY_URL_QUERY = {'$or': [dict(NOT_DELETED_QUERY),      # utils.py:692
                                  dict(PRIOR_VERSION_QUERY)]}   # utils.py:722
"""The filter form of ``is_reachable_by_url()`` -- the two ``_id`` steps of the
resolver, which are the only two that can answer the question for a named
document.  See ``is_reachable_by_url()`` for why the name steps add nothing."""


def resolver_queries(project_id=None, project_name=None):
    """The queries ``get_one_project()`` issues, in order, as ``(line, filter)``.

    For code that must ask the database "could the resolver still return this?"
    rather than reason about a loaded document -- cleanup tooling asks exactly
    that immediately before deleting anything, so a drift between the rules it
    used to select a document and the resolver costs nothing.

    ``project_id`` that is not a valid ObjectId simply drops the ``_id`` steps,
    the same way the resolver's ``try``/``except`` does.
    """
    from bson import ObjectId

    oid = None
    if project_id is not None:
        try:
            oid = ObjectId(str(project_id))
        except Exception:
            oid = None

    queries = []
    if oid is not None:
        queries.append(('utils.py:692', combine(NOT_DELETED_QUERY, _id=oid)))
    if project_name:
        queries.append(('utils.py:703', combine(NOT_DELETED_QUERY, alias_name=project_name)))
        queries.append(('utils.py:711', combine(NOT_DELETED_QUERY, project_name=project_name)))
    if oid is not None:
        queries.append(('utils.py:722', combine(PRIOR_VERSION_QUERY, _id=oid)))
    if project_name:
        queries.append(('utils.py:736', combine(PRIOR_VERSION_QUERY, project_name=project_name)))
    return queries


# ---------------------------------------------------------------------------
# Reading previous_versions[], which has had two encodings
# ---------------------------------------------------------------------------

CURRENT_ENCODING = 'linkid'
"""An entry is ``{'date': ..., 'linkid': '<hex id>'}``, plus optional tool
version fields.  191 of the 196 entries on dev use this."""

LEGACY_JSON_ENCODING = 'legacy-json'
"""An entry is a *string* holding JSON -- either a one-element list or a bare
object -- whose reference is keyed ``link`` rather than ``linkid``.

Written by the code that predates April 2024, when the serialisation was
changed to store the array directly ("was being saved as an array, holding
another array, holding a json dumped string").  The documents written before
that were never migrated: 5 remain on dev, all created between February and
April 2024, and the two variants (list-wrapped and bare object) sit side by
side because the wrapping changed too.

Nothing in the application reads this encoding.  ``previous_versions()`` coerces
the string to ``{'linkid': '<the whole JSON text>'}``, which renders as a link
to ``/project/[{"date": ...}]`` and matches no query.  The reference is intact;
only the reader is missing."""


def iter_previous_versions(doc):
    """Yield ``(entry, encoding)`` for every version *doc* names as an ancestor.

    The one place that knows how ``previous_versions[]`` is written.  Both
    encodings are decoded here and both come back in the same shape -- a dict
    with ``linkid`` as a string, plus whatever else the entry carried -- so a
    caller gets a usable id either way and decides separately whether it cares
    about the encoding.  That separation is the reason this is a function
    rather than an inline loop: read as ``entry['linkid']`` alone, five dev
    documents look like references to a document that does not exist, and they
    are all references to a document that does.

    An entry in no recognised encoding yields ``encoding=None`` with its raw
    text as ``linkid``, rather than being skipped.  A reference that cannot be
    read is a finding; dropping it silently is how the history table came to be
    shorter than the history.
    """
    import json

    entries = doc.get('previous_versions')
    if not entries:
        return
    if not isinstance(entries, list):     # never seen; do not iterate a str
        entries = [entries]

    for entry in entries:
        if isinstance(entry, dict):
            if entry.get('linkid'):
                found = dict(entry)
                found['linkid'] = str(found['linkid'])
                yield found, CURRENT_ENCODING
            elif entry.get('link'):       # an unwrapped legacy object
                found = dict(entry)
                found['linkid'] = str(found.pop('link'))
                yield found, LEGACY_JSON_ENCODING
            continue

        if not isinstance(entry, str):
            yield {'linkid': repr(entry)}, None
            continue

        try:
            parsed = json.loads(entry)
        except ValueError:
            # A bare id string.  Not a format anything is known to have
            # written, but it is readable, so read it.
            yield {'linkid': entry}, CURRENT_ENCODING
            continue

        for item in (parsed if isinstance(parsed, list) else [parsed]):
            if isinstance(item, dict) and (item.get('link') or item.get('linkid')):
                found = dict(item)
                found['linkid'] = str(found.pop('link', None) or found['linkid'])
                yield found, LEGACY_JSON_ENCODING
            else:
                yield {'linkid': entry}, None


def iter_lineage_references(doc):
    """``(linkid, encoding)`` for every version *doc* names, ids only."""
    for entry, encoding in iter_previous_versions(doc):
        yield entry['linkid'], encoding


# ---------------------------------------------------------------------------
# In-memory evaluation of the filters above
# ---------------------------------------------------------------------------
#
# This is a second implementation of a slice of the query language, which is
# exactly the kind of thing this spec exists to distrust.  It is here anyway
# because the alternative is worse: the test suite already carried two ad-hoc
# matchers of its own, each with a different idea of what a query means, and
# the resolver they stood in for is the thing that must not be guessed at.  One
# evaluator, small enough to read, that raises on anything it does not
# implement, and that is checked against a real MongoDB over every fixture in
# tests/test_project_status.py, is the version of this that stays honest.
#
# It implements only what this repository's queries actually use: equality
# (BSON-strict for booleans), $in, $ne, $exists, $or/$and/$nor, dotted paths,
# and MongoDB's rule that a query against an array field matches when any
# element matches.  Everything else raises.

def _values_at(doc, path):
    """Every value MongoDB would consider at *path*.

    Follows dotted paths and descends into arrays of subdocuments, so
    ``previous_versions.linkid`` yields one value per entry -- the lineage
    query the history pages and the tombstone redirect both depend on.
    """
    current = [doc]
    for part in path.split('.'):
        found = []
        for value in current:
            if isinstance(value, dict):
                if part in value:
                    found.append(value[part])
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, dict) and part in item:
                        found.append(item[part])
        current = found
    return current


def _equals(value, wanted):
    """MongoDB equality: type-bracketed for booleans, so ``0`` is not ``False``."""
    if isinstance(wanted, bool) or isinstance(value, bool):
        return value is wanted
    return value == wanted


def _field_matches(doc, path, wanted):
    """Equality against a field, including MongoDB's match-any-element rule."""
    values = _values_at(doc, path)
    if not values:
        # An absent field reads as null, which is what makes {'x': None} match
        # a document with no 'x'.
        values = [None]
    for value in values:
        if _equals(value, wanted):
            return True
        if isinstance(value, (list, tuple)) and any(_equals(v, wanted) for v in value):
            return True
    return False


def matches(doc, query):
    """Evaluate a query against a loaded document.  See the note above."""
    if not isinstance(query, dict):
        raise ValueError(f"Not a query: {query!r}")

    for key, condition in query.items():
        if key == '$nor':
            if any(matches(doc, sub) for sub in condition):
                return False
        elif key == '$or':
            if not any(matches(doc, sub) for sub in condition):
                return False
        elif key == '$and':
            if not all(matches(doc, sub) for sub in condition):
                return False
        elif key.startswith('$'):
            raise ValueError(f"matches() does not implement {key!r}")
        elif isinstance(condition, dict) and any(k.startswith('$') for k in condition):
            operators = set(condition)
            if operators == {'$ne'}:
                # $ne is the negation of the whole equality test, arrays
                # included -- not a comparison against one value.
                if _field_matches(doc, key, condition['$ne']):
                    return False
            elif operators == {'$in'}:
                if not any(_field_matches(doc, key, wanted)
                           for wanted in condition['$in']):
                    return False
            elif operators == {'$exists'}:
                if bool(_values_at(doc, key)) is not bool(condition['$exists']):
                    return False
            else:
                raise ValueError(
                    f"matches() does not implement {sorted(operators)} on {key!r}")
        elif not _field_matches(doc, key, condition):
            return False
    return True
