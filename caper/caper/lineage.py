"""A project's version chain: reading it from the lineage pointers, and writing
them when a version is created, deleted or promoted.

``project_status`` says what one document *is*.  This says which documents
belong together, and in what order.  Every query that used to ask
``{'previous_versions.linkid': <id>}`` -- a scan of an array on every document
in the collection -- becomes an equality match on ``version_chain_id``.

Django-free and handle-free on purpose.  The collection is passed in, so the
standalone scripts, the validator and the differential harness read a chain the
same way the site does, and the tests can hand it a dictionary.

**Both encodings are still written.**  Every reader here falls back to nothing
rather than guessing: a document with no ``version_chain_id`` -- 26 of them on
dev, in the six chains the backfill refused to order -- makes
``chain_members()`` return ``None``, and the caller uses the
``previous_versions[]`` path it used before.  A document without pointers must
never render an empty history; it renders the old one.  The write half keeps
that property by refusing to half-pointer a chain: a new version whose
predecessor has no pointers is left without them too, so the pair falls back
together (see ``predecessor_chain``).

Tombstones written *before* this module existed are not in their chains, and
that is measured rather than assumed.  A deleted non-head version was removed
from the head's ``previous_versions[]`` and given a ``redirect_to_project``, so
connected components over that array could not see it: on production,
2026-08-27, both TOMBSTONE documents sit in single-member chains of their own
and redirect across a chain boundary into the chain they were deleted from.
Reading history from pointers alone would drop two deleted versions from two
projects' history pages, so callers keep the separate ``redirect_to_project``
lookup they already had.  Deletions written from here keep the tombstone in its
chain with its ordinal intact; retrofitting the two older ones is a data change
and a separate decision.
"""

import logging
from collections import namedtuple

from bson import ObjectId

from .project_status import TOMBSTONE_MARKER_FIELDS, is_tombstone

# The pointer fields, plus every field the functions below ask a question of. A
# project document averages 690 KB on production; a caller that only needs to
# know which member is the head must not pay for the payload of every member to
# find out.
#
# The tombstone markers are here because plan_deletion() asks is_tombstone() of
# these documents, and a predicate is only as good as the fields it was given.
# Projected away, both markers read as absent, every tombstone looks like a
# surviving version, and deleting a head promotes one that was itself deleted --
# measured on dev on 2026-08-28, where deleting the head of a three-version
# chain promoted the tombstone left by the previous deletion instead of the one
# surviving version.
#
# The tests missed it for a reason worth keeping: they deleted heads, and they
# deleted middles, but never a head from a chain that already held a tombstone,
# which is the only shape where the two differ. A transition tested from a
# clean chain is a transition tested once.
#
# So: a field this module reads belongs in this projection.
POINTER_PROJECTION = {field: 1 for field in
                      ('version_chain_id', 'version_ordinal', 'is_latest',
                       'previous_version_id', 'next_version_id', 'linkid')
                      + TOMBSTONE_MARKER_FIELDS}


def has_pointers(doc):
    """True when *doc* carries the lineage pointers written by the backfill."""
    return bool(doc) and doc.get('version_chain_id') is not None


def _ordinal(doc):
    """Sort key that tolerates a missing or non-integer ordinal.

    I4 asserts ordinals are unique and contiguous from 1; this is what happens
    when that assertion is false in front of a user.  A chain that cannot be
    ordered is still rendered, in a stable order, rather than raising on a page
    load -- and the validator is where the fact that it is broken belongs.
    """
    ordinal = doc.get('version_ordinal')
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return (0, ordinal, str(doc.get('_id')))
    return (1, 0, str(doc.get('_id')))


def chain_members(collection, doc, projection=None):
    """Every version of *doc*'s project, oldest first.  ``None`` if unpointered.

    One indexed equality match, where the array-based reader needed a scan of
    ``previous_versions.linkid`` across the collection.  Includes *doc* itself,
    and includes members of every status: a superseded version and a tombstone
    both occupy their place in the chain.
    """
    chain_id = doc.get('version_chain_id') if doc else None
    if chain_id is None:
        return None
    members = list(collection.find({'version_chain_id': chain_id}, projection))
    if not members:
        # The document names a chain the collection does not have. Not this
        # module's call to decide; hand back the one member that is certainly
        # in it so a page still renders, and let the validator say so.
        return [doc]
    members.sort(key=_ordinal)
    return members


def chains_for(collection, docs, projection=None):
    """chain id -> its members, oldest first, for every chain *docs* belong to.

    One query for a whole page of projects.  The list endpoint serialises every
    project a user can see, and asking for each one's chain separately would be
    a query per row -- the read amplification this codebase has already had to
    go and fix twice.  Documents with no chain id contribute nothing and are
    simply absent from the result, which is how a caller tells it must fall
    back for that row.
    """
    wanted = {doc.get('version_chain_id') for doc in docs
              if doc.get('version_chain_id') is not None}
    if not wanted:
        return {}
    grouped = {}
    for member in collection.find({'version_chain_id': {'$in': list(wanted)}},
                                  projection):
        grouped.setdefault(member['version_chain_id'], []).append(member)
    for members in grouped.values():
        members.sort(key=_ordinal)
    return grouped


def head(members):
    """The current version among *members*: the one flagged ``is_latest``.

    Falls back to the highest ordinal when the flag is absent or duplicated,
    because a history page with no current version is worse than one whose
    current version was picked by ordinal.  I3 and I5 are what stop that
    fallback from ever being what runs.
    """
    if not members:
        return None
    flagged = [doc for doc in members if doc.get('is_latest') is True]
    if len(flagged) == 1:
        return flagged[0]
    return members[-1]


def is_head(doc, members):
    current = head(members)
    return current is not None and current.get('_id') == doc.get('_id')


def ancestors(members, doc):
    """The members before *doc* in the chain, oldest first."""
    if doc is None:
        return list(members)
    key = _ordinal(doc)
    return [member for member in members if _ordinal(member) < key]


def latest_version(collection, doc, projection=None):
    """The head of *doc*'s chain, or ``None`` when there are no pointers.

    Replaces the reverse lookup ``{'previous_versions.linkid': str(id)}``, which
    is a collection scan and which misses any reference stored in the pre-April
    2024 encoding -- the two documents it cannot see are exactly the ones whose
    history was already unreachable.
    """
    members = chain_members(collection, doc, projection)
    if members is None:
        return None
    return head(members)


def chain_ids(collection, doc):
    """The ids of every member of *doc*'s chain, as strings.

    For the tombstone lookup: a deleted version redirects to whichever member
    was the head when it was deleted, which is not always the head now.
    """
    members = chain_members(collection, doc, {'_id': 1})
    if members is None:
        return None
    return [str(member['_id']) for member in members]


def resolve_id(value):
    """*value* as an ObjectId, or ``None`` if it is not one.

    Lineage pointers are ObjectIds and ``previous_versions[].linkid`` holds
    strings; anything crossing between them goes through here rather than
    through a bare ``ObjectId()`` that raises on a page load.
    """
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Writing
#
# One rule governs everything below, and it is the one the spec's worked
# transitions turn on:
#
#     Pointers are structure.  is_latest is position.  status is state.
#
# A deletion therefore never rewrites previous_version_id or next_version_id and
# never renumbers an ordinal.  The deleted version stays a node of the chain
# whose payload is gone -- that is what keeps /project/<id> resolving, and what
# lets a history table say "version 2 deleted, version 3 current" instead of
# silently rendering two versions where there were three.  Only is_latest moves,
# and it moves backwards along a chain that did not change.
#
# Two consequences, both deliberate, both of which the validator had to be
# corrected for:
#
#   * After a head deletion the promoted version has is_latest=True *and* a
#     next_version_id (pointing at the tombstone that used to follow it).  So
#     is_latest is not derivable from next_version_id, and any check coupling
#     them is wrong.  I3 and I16 are what enforce exactly-one-head.
#   * previous_versions[] does not record deleted versions, so once a chain
#     contains a tombstone the array is a strict subset of the pointer lineage.
#     I11 compares the two across the compatibility window and must exclude
#     tombstones, or every deletion would look like a divergence.
# ---------------------------------------------------------------------------

POINTER_FIELDS = ('version_chain_id', 'previous_version_id', 'next_version_id',
                  'version_ordinal', 'is_latest')


def new_chain_fields(new_id):
    """Pointers for the first version of a project: a chain of one.

    The chain is named by its oldest member's id rather than by a minted one,
    so the value is derivable from the data and a rebuild computes the same
    thing.  It is stable because versions are appended at the other end.
    """
    return {'version_chain_id': new_id,
            'previous_version_id': None,
            'next_version_id': None,
            'version_ordinal': 1,
            'is_latest': True}


def next_ordinal(members):
    """One past the highest ordinal in *members*.

    Ordinals are never reused and never renumbered, so this counts from the
    high-water mark rather than from ``len(members)``: a chain that has had a
    version deleted still has that version in it, holding its ordinal.
    """
    ordinals = [doc.get('version_ordinal') for doc in members]
    highest = max((o for o in ordinals
                   if isinstance(o, int) and not isinstance(o, bool)),
                  default=len(members))
    return highest + 1


def plan_new_version(members, new_id):
    """Transition T1: (fields for the new head, (predecessor id, its fields)).

    *members* is the chain being extended, oldest first, or ``[]`` for a
    project that has no history yet.  The predecessor is the highest-ordinal
    member -- never the last element of ``previous_versions[]``, which nothing
    orders, and never the newest date, which production has ties on.

    The predecessor may be a TOMBSTONE.  That is transition T9, re-populating an
    emptied project, and it needs no special case: a deleted version is a
    legitimate ``previous_version_id``, and the history then reads "version 1
    deleted, version 2 current", which is both true and unrepresentable in the
    array encoding.
    """
    if not members:
        return new_chain_fields(new_id), None

    predecessor = members[-1]
    chain_id = predecessor.get('version_chain_id')
    new_fields = {
        'version_chain_id': chain_id,
        'previous_version_id': predecessor['_id'],
        'next_version_id': None,
        'version_ordinal': next_ordinal(members),
        'is_latest': True,
    }
    return new_fields, (predecessor['_id'],
                        {'next_version_id': new_id, 'is_latest': False})


def predecessor_chain(collection, previous_versions):
    """The chain a new version extends: ``[]``, a member list, or ``None``.

    Three outcomes, and the third is the one that matters:

    ``[]``      nothing to extend -- a brand new project, which gets a chain of
                one.
    a list      the members of the chain to append to, oldest first.
    ``None``    *refuse*.  The references resolve to documents that carry no
                pointers, or to more than one chain.  Writing pointers on the
                new version alone would strand its predecessors: the new
                document would read its history from a chain of one and render
                a project with no history, while the array it inherited names
                every version it has.  Leaving it unpointered keeps the pair on
                the array path together, and ``backfill_project_status.py`` is
                what gives such a chain pointers.

    Measured 2026-08-27: 0 documents on prod are unpointered, 26 on dev, so on
    production this refusal is unreachable and on dev it is the six chains the
    backfill declined to order.
    """
    # Imported here rather than at module scope: project_status imports nothing
    # from this module and must keep it that way, but the decoder for the two
    # previous_versions[] encodings lives there and must not be copied.
    from .project_status import iter_lineage_references

    references = [linkid for linkid, _encoding
                  in iter_lineage_references({'previous_versions':
                                              previous_versions or []})]
    if not references:
        return []

    ids = [oid for oid in (resolve_id(ref) for ref in references) if oid is not None]
    if not ids:
        return None

    candidates = list(collection.find({'_id': {'$in': ids}}, POINTER_PROJECTION))
    chains = {doc.get('version_chain_id') for doc in candidates
              if doc.get('version_chain_id') is not None}
    if not chains:
        logging.info(
            'lineage: %d predecessor(s) carry no version_chain_id, so the new '
            'version is left unpointered and reads its history from '
            'previous_versions[]', len(references))
        return None
    if len(chains) > 1:
        logging.warning(
            'lineage: previous_versions[] names %d different chains (%s); '
            'refusing to guess which one the new version extends',
            len(chains), ', '.join(sorted(str(c) for c in chains)))
        return None

    chain_id = chains.pop()
    members = list(collection.find({'version_chain_id': chain_id},
                                   POINTER_PROJECTION))
    members.sort(key=_ordinal)
    return members


def link_new_version(collection, new_id, previous_versions=None):
    """Transition T1/T9: give a just-written document its place in the chain.

    Returns the pointer fields written on *new_id*, or ``{}`` when the chain
    was refused.  The predecessor is demoted before the new version is
    promoted, so the window between the two writes has no head rather than two
    -- ``head()`` falls back to the highest ordinal, which is the new version
    only once it is in the chain at all.
    """
    members = predecessor_chain(collection, previous_versions)
    if members is None:
        return {}

    new_fields, predecessor = plan_new_version(members, new_id)
    if predecessor is not None:
        predecessor_id, predecessor_fields = predecessor
        collection.update_one({'_id': predecessor_id},
                              {'$set': predecessor_fields})
    collection.update_one({'_id': new_id}, {'$set': new_fields})
    logging.info('lineage: %s is ordinal %d of chain %s', new_id,
                 new_fields['version_ordinal'], new_fields['version_chain_id'])
    return new_fields


def unlink_new_version(collection, new_id):
    """Undo ``link_new_version``: the predecessor becomes the head again.

    For the rollback path, where aggregation failed and the old version is
    being restored.  Without this the failed document keeps ``is_latest`` and
    the chain has two heads -- or one head that is a project the user was told
    had failed.
    """
    doc = collection.find_one({'_id': new_id}, POINTER_PROJECTION)
    if not has_pointers(doc):
        return {}

    collection.update_one({'_id': new_id},
                          {'$unset': {field: '' for field in POINTER_FIELDS}})
    predecessor_id = doc.get('previous_version_id')
    if predecessor_id is not None:
        collection.update_one(
            {'_id': predecessor_id},
            {'$set': {'next_version_id': None, 'is_latest': True}})
    return {'unlinked': new_id, 'restored_head': predecessor_id}


DeletionPlan = namedtuple(
    'DeletionPlan', 'victim_id promoted_id victim_keeps_head chain_emptied')
"""What a version deletion does to the chain's pointers.

``promoted_id`` is the member that becomes the head, or ``None`` when the
victim was not the head (T2) or when nothing survives to promote (T7, T8).
``victim_keeps_head`` is what ``is_latest`` the tombstone is written with:
True only in the terminal case, where the chain is emptied and the deleted
version is still its position-in-time -- an empty project has a current
version to render and restore into, it just has no payload behind it.
"""


def plan_deletion(members, victim_id):
    """Transitions T2, T3, T7 and T8, which differ only in what survives.

    Returns ``None`` when there are no pointers to plan against, which is the
    caller's signal to keep doing what it did before.

    Promotion targets the highest ``version_ordinal`` among members that are
    neither the victim nor already tombstones.  The code this replaces took
    ``previous_versions[-1]``: nothing orders that array, and production
    contains same-day version pairs where a date sort would tie as well.
    """
    if not members:
        return None
    victim_id = resolve_id(victim_id)
    victim = next((doc for doc in members if doc.get('_id') == victim_id), None)
    if victim is None:
        return None

    # Sorted here rather than trusted from the caller: this function's whole
    # job is to stop promotion depending on the order a list happened to
    # arrive in, and it would be a poor place to start depending on one.
    members = sorted(members, key=_ordinal)
    survivors = [doc for doc in members
                 if doc.get('_id') != victim_id and not is_tombstone(doc)]
    chain_emptied = not survivors

    if not is_head(victim, members):
        # T2: the head is untouched, the neighbours are untouched, and the
        # victim keeps the ordinal it has always had.
        return DeletionPlan(victim_id, None, False, chain_emptied)

    if survivors:
        # T3: is_latest moves backwards along a chain that does not change.
        return DeletionPlan(victim_id, survivors[-1]['_id'], False, False)

    # T7 / T8: nothing to promote and nowhere to redirect. The chain is EMPTY --
    # a derived property of its members, never a stored one.
    return DeletionPlan(victim_id, None, True, True)


def pointer_fields(doc, is_latest=None):
    """*doc*'s pointer fields, for a rewrite that must not lose them.

    A tombstone is written with ``replace_one``, so every field it should keep
    has to be named.  Before this existed the replacement dropped all five, and
    a deleted version fell out of its chain -- which is exactly why the two
    tombstones on production are in chains of their own.
    """
    if not has_pointers(doc):
        return {}
    fields = {field: doc.get(field) for field in POINTER_FIELDS}
    if is_latest is not None:
        fields['is_latest'] = is_latest
    return fields
