"""Reading a project's version chain from the lineage pointers.

``project_status`` says what one document *is*.  This says which documents
belong together, and in what order.  It is the read half of the lineage model:
every query that used to ask ``{'previous_versions.linkid': <id>}`` -- a scan of
an array on every document in the collection -- becomes an equality match on
``version_chain_id``.

Django-free and handle-free on purpose.  The collection is passed in, so the
standalone scripts, the validator and the differential harness read a chain the
same way the site does, and the tests can hand it a dictionary.

**The array is still authoritative until the write paths move.**  Every reader
here falls back to nothing rather than guessing: a document with no
``version_chain_id`` -- 26 of them on dev, in the six chains the backfill
refused to order -- makes ``chain_members()`` return ``None``, and the caller
uses the ``previous_versions[]`` path it used before.  A document without
pointers must never render an empty history; it renders the old one.

Tombstones are the exception worth knowing about, and it is measured rather
than assumed.  A deleted non-head version is removed from the head's
``previous_versions[]`` and given a ``redirect_to_project``, so connected
components over that array cannot see it: on production, 2026-08-27, both
TOMBSTONE documents sit in single-member chains of their own and redirect
across a chain boundary into the chain they were deleted from.  The chain
pointers therefore do not yet reach them, and reading history from pointers
alone would drop two deleted versions from two projects' history pages.  So
callers keep the separate ``redirect_to_project`` lookup they already had.
Attaching tombstones to their chains is a data change, and data changes are the
write half of this phase.
"""

from bson import ObjectId

# The pointer fields themselves, and nothing else. A project document averages
# 690 KB on production; a caller that only needs to know which member is the
# head must not pay for the payload of every member to find out.
POINTER_PROJECTION = {field: 1 for field in
                      ('version_chain_id', 'version_ordinal', 'is_latest',
                       'previous_version_id', 'next_version_id', 'linkid')}


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
