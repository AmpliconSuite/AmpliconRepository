"""The derived chain view: one document per version chain, rebuilt from documents.

**Direction of authority is one-way, and that is the whole point.**  The project
documents own lineage -- ``previous_version_id``, ``next_version_id``,
``is_latest``, ``version_ordinal`` are written by feature code.  This collection
is a materialised view of them.  Feature code *reads* it; only the rebuild
command writes it; documents always win.  A test enforces the direction, because
a view that anything may write is a second source of truth, which is the failure
this whole area is being dug out of.

What it buys: the chain as an addressable thing (a project separate from its
versions), the whole lineage in one read instead of a walk, and a second copy to
check the documents against.  What it costs: it can go stale.  ``source_digest``
makes that cheap to detect -- recompute from the documents and compare one
string, no field-by-field diff -- and invariant I9 does exactly that.

Two fields are **authoritative here and nowhere else**, because they belong to
the project rather than to any version of it and must survive the deletion of
every version:

  ``canonical_name``   what the project is called
  ``retired``          whether it is closed to new versions

The rebuild seeds those on first insert and never overwrites them afterwards.
Everything else in the document is derived and is replaced wholesale on every
rebuild.
"""

import datetime
import hashlib

from .project_status import TOMBSTONE, classify

COLLECTION = 'project_version_chains'

# Rebuilt from the documents on every run; anything not listed here is either
# authoritative on the chain document or does not belong on it at all.
DERIVED_FIELDS = ('head_project_id', 'versions', 'rebuilt_at', 'source_digest')

# Set when the chain document is created and never touched again -- see the
# module docstring.
AUTHORITATIVE_FIELDS = ('canonical_name', 'retired')

PAYLOAD_PRESENT = 'present'
PAYLOAD_PURGED = 'purged'


def _ordinal(doc):
    """The document's ordinal as a sortable int, 0 when it has none.

    Booleans are excluded deliberately: ``isinstance(True, int)`` is True in
    Python, and an ordinal of ``True`` would sort as 1 and compare equal to a
    real ordinal 1.
    """
    value = doc.get('version_ordinal')
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def payload_state(doc):
    """Whether this version still has its payload."""
    return PAYLOAD_PURGED if doc.get('payload_purged') is True else PAYLOAD_PRESENT


def order_members(members):
    """Chain members in ordinal order, ties broken by ``_id`` so it is total.

    A stable, total order matters more than which order it is: the digest is
    computed over this sequence, so two runs that disagree about ties would
    report a chain as drifting when nothing had changed.
    """
    return sorted(members, key=lambda doc: (_ordinal(doc), str(doc['_id'])))


def member_entry(doc):
    """One version's entry in the chain document."""
    return {
        'project_id': doc['_id'],
        'ordinal': _ordinal(doc),
        'status': classify(doc),
        'payload_state': payload_state(doc),
        'date': doc.get('date'),
    }


def source_digest(members):
    """A digest of the (id, ordinal, status) tuples the view was built from.

    Recomputing this from the documents and comparing one string is how
    staleness is detected without a field-by-field diff. Only the three fields
    that define the chain's shape go in: ``date`` is carried on the entries for
    display and must not make the digest churn, and ``payload_state`` follows
    ``status`` for every transition that changes it.

    The separators are not decorative -- without them, ordinals 1 and 11 next to
    each other could produce the same byte string as 11 and 1.
    """
    parts = []
    for doc in order_members(members):
        parts.append(f'{doc["_id"]}\x1f{_ordinal(doc)}\x1f{classify(doc)}')
    return hashlib.sha256('\x1e'.join(parts).encode('utf-8')).hexdigest()


def is_empty_chain(members):
    """A chain is EMPTY iff every member is a tombstone. Derived, never stored.

    Returned rather than written into the chain document on purpose: storing it
    would be a second opinion about a question the members already answer, and
    it would go stale the first time a version was restored. I15 checks that
    nothing stores it.
    """
    members = list(members)
    return bool(members) and all(classify(doc) == TOMBSTONE for doc in members)


def head_of(members):
    """The ``is_latest`` member, or None when the chain has no head.

    Position, not state: the head of an emptied chain is a tombstone, and that
    is correct. None means the documents are wrong -- I3 and I16 own that -- so
    the view records the absence rather than inventing a head to paper over it.
    """
    heads = [doc for doc in members if doc.get('is_latest') is True]
    return heads[0] if len(heads) == 1 else None


def build_chain_document(chain_id, members):
    """The derived half of a chain document. Authoritative fields are not here.

    Returns only what a rebuild is allowed to overwrite, so a caller cannot
    accidentally clobber ``canonical_name`` by spreading this over the stored
    document.
    """
    ordered = order_members(members)
    head = head_of(ordered)
    return {
        '_id': chain_id,
        'head_project_id': head['_id'] if head is not None else None,
        'versions': [member_entry(doc) for doc in ordered],
        'rebuilt_at': datetime.datetime.now(datetime.timezone.utc),
        'source_digest': source_digest(ordered),
    }


def default_canonical_name(members):
    """The name to seed ``canonical_name`` with when a chain is first inserted.

    The head's name, falling back to the newest member that has one: a chain
    whose head is a tombstone can still have a name worth keeping, and that is
    exactly the case the field exists for.
    """
    ordered = order_members(members)
    head = head_of(ordered)
    if head is not None and head.get('project_name'):
        return head['project_name']
    for doc in reversed(ordered):
        if doc.get('project_name'):
            return doc['project_name']
    return None


def group_into_chains(documents):
    """``{chain_id: [member, ...]}`` for every document that names a chain.

    Documents with no ``version_chain_id`` are skipped rather than each made
    into a chain of one: they are I1's population, and inventing a chain for
    them here would hide that.
    """
    chains = {}
    for doc in documents:
        chain_id = doc.get('version_chain_id')
        if chain_id is None:
            continue
        chains.setdefault(chain_id, []).append(doc)
    return chains
