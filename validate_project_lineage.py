#!/usr/bin/env python
"""
What has to be true about project version history, checked against a database.

Nineteen invariants, each reported as ok, FAIL or SKIP with the reason.

Declaring the ones that cannot run is the point.  A validator that quietly
implements the checkable invariants and prints "all checks passed" is the same
mistake as the cleanup script that protected three of the four states it needed
to: a confident statement about a list, made by something that only knew part
of the list.  Here every gap names what it is waiting for, so coverage grows
visibly as those things land instead of being something a reader has to
reconstruct.

**Which invariants can run is decided by the database, not by this docstring.**
An earlier version of this file carried the split as prose -- "seven can be
evaluated, the other twelve are about fields that do not exist yet" -- and as a
``needs='...which nothing writes yet'`` string on each unimplemented invariant.
Both went stale the moment the backfill ran.  On 2026-08-27 all 311 production
documents carried ``status``, ``version_chain_id``, ``version_ordinal``,
``is_latest`` and both pointers, and this file still reported eight invariants
as not checkable because a field "does not exist yet".  That is this codebase's
recurring defect exactly -- a fact maintained in two places -- committed by the
tool built to catch it.  So a checker that needs a field now asks the snapshot
whether the field is there, and says which documents lack it if only some do.

Nothing here writes.  There is no ``--execute`` because there is nothing to
execute -- every finding is something for a person to decide about.

Usage::

    set -a; source caper/config.sh; set +a
    python validate_project_lineage.py                      # local
    python validate_project_lineage.py --expect-db caper-dev --report

``--expect-db`` is required for any database not on this machine.  Dev and prod
are two databases on one DocumentDB cluster reached with credentials that
differ by one environment variable, and dev's database is called ``caper-dev``
-- the same name the local docker mongo uses -- so the name alone identifies
nothing.

There is a longer write-up of the problem this addresses in
``docs/project-version-history-and-provenance-spec.md``.  It is background, not
authority: everything this file needs in order to be correct is written here.
"""

import argparse
import os
import sys
from collections import defaultdict

# VALIDATOR_REPO_ROOT lets this run from outside the checkout -- copied into a
# container's /tmp to measure a database, without a stray file appearing in the
# server's working tree.  I18's error message already told people to set it; it
# was not read, which is the same defect as the skip reasons below.
_REPO_ROOT = os.environ.get('VALIDATOR_REPO_ROOT') or \
    os.path.dirname(os.path.abspath(__file__))
if os.path.join(_REPO_ROOT, 'caper') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'caper'))

from bson import ObjectId                                          # noqa: E402

from caper.project_status import (                                 # noqa: E402
    CURRENT_ENCODING, DETACHED, LEGACY_JSON_ENCODING, LIVE, NOT_DELETED_QUERY,
    PRIOR_VERSION_QUERY, SUPERSEDED, TOMBSTONE, classify,
    is_reachable_by_url, iter_lineage_references,
)
from caper.project_version_cleanup import iter_gridfs_file_ids     # noqa: E402

# Fields big enough to matter: a project document averages 690 KB on production
# and almost all of that is these three.  The invariants below need none of them
# except the GridFS ids inside 'runs', which get their own streaming pass.
_HEAVY_FIELDS = ('runs', 'aggregate_df', 'sample_data')

# GridFS ids are looked up in batches rather than one query per file: prod
# holds 1,065,019 files and a per-file round trip would take hours.
_GRIDFS_BATCH = 1000


class Unavailable(Exception):
    """A checker could not run at all, and must not be reported as passing.

    The distinction matters more than it looks.  I18 walks the source tree; on
    the first run against dev it was pointed at a directory that did not exist,
    found no files, and printed ``ok`` -- a check that had examined nothing
    reporting that everything was fine.  That is the exact shape of the two
    incidents this file exists to prevent, so a checker that cannot do its job
    now says so and lands in the SKIP column with the rest of the honest gaps.
    """


class Finding:
    """One invariant violation, tied to the document that violates it."""

    def __init__(self, invariant, doc_id, project_name, detail):
        self.invariant = invariant
        self.doc_id = doc_id
        self.project_name = project_name
        self.detail = detail

    def __repr__(self):
        return f'<{self.invariant} {self.doc_id} {self.detail}>'

    def line(self):
        if self.doc_id is None:          # a source-level finding (I18)
            return self.detail
        return f'{str(self.doc_id):<26} {(self.project_name or "?")[:34]:<36} {self.detail}'


class Snapshot:
    """Everything the checkers read, loaded once.

    Two passes over the collection, because the two things needed have opposite
    shapes: the status and lineage fields are tiny and wanted for every
    document at once, while the GridFS ids live inside the one field that makes
    a document large.  Loading both together would hold the whole collection --
    238 MB on prod -- in memory to read a few booleans.
    """

    def __init__(self, collection, fs_files, skip_gridfs=False, chain_view=None):
        self.collection = collection
        self.fs_files = fs_files
        self.chain_view = chain_view

        self.documents = list(collection.find({}, {field: 0 for field in _HEAVY_FIELDS}))
        self.by_id = {doc['_id']: doc for doc in self.documents}
        self.status = {doc['_id']: classify(doc) for doc in self.documents}

        # linkid string -> ids of the documents whose previous_versions[] names
        # it.  Built once; I6 and I7 both walk it from opposite ends.
        #
        # Decoded through iter_lineage_references rather than by reading
        # entry['linkid'] here, and that is not a detail: this loop used to do
        # the latter, and reported five dev documents as naming a document that
        # is not in the collection.  All five name a document that is.
        self.referenced_by = defaultdict(list)
        self.encodings = defaultdict(list)     # doc id -> encoding per reference
        for doc in self.documents:
            for linkid, encoding in iter_lineage_references(doc):
                self.referenced_by[linkid].append(doc['_id'])
                self.encodings[doc['_id']].append((linkid, encoding))

        # doc id -> the GridFS ids it names.  Streamed one document at a time so
        # the heavy field is never held for more than one document.
        self.gridfs_ids = {}
        self.gridfs_skipped = skip_gridfs
        if not skip_gridfs:
            cursor = collection.find({}, {field: 1 for field in _HEAVY_FIELDS +
                                          ('tarfile',)}).batch_size(10)
            for doc in cursor:
                ids = sorted(set(iter_gridfs_file_ids(doc)))
                if ids:
                    self.gridfs_ids[doc['_id']] = ids

    def present(self, field):
        """How many documents carry *field* at all, absent or null aside.

        Key presence, not truthiness: ``previous_version_id: None`` is the
        correct value on the first version of every chain, and ``is_latest:
        False`` on every version but one.  A checker asking "has the backfill
        run here" must not read either of those as a no.
        """
        return sum(1 for doc in self.documents if field in doc)

    def require(self, *fields):
        """Raise Unavailable if *none* of *fields* is on any document.

        The two failure modes are different and only one of them is a gap. A
        field on no document at all means the backfill has not run against this
        database, and a checker that reported "ok" over that would be asserting
        something about zero documents.  A field on some documents and not
        others is a finding -- a half-finished backfill is worse than an
        unstarted one -- so it runs, and the checker reports the absences.
        """
        counts = {field: self.present(field) for field in fields}
        if any(counts.values()):
            return
        raise Unavailable(
            f'{", ".join(fields)} -- on none of the {len(self.documents)} '
            f'documents in this database. Run backfill_project_status.py here.')

    def name(self, doc_id):
        doc = self.by_id.get(doc_id) or {}
        return doc.get('project_name')

    def chains(self):
        """version_chain_id -> its member documents, ordered by version_ordinal.

        Documents with no chain id are left out; I1 owns their absence.  The
        sort key tolerates a missing ordinal so I4 can report a chain that I3
        and I16 have already grouped, rather than raising here.
        """
        grouped = defaultdict(list)
        for doc in self.documents:
            chain_id = doc.get('version_chain_id')
            if chain_id is not None:
                grouped[chain_id].append(doc)
        for members in grouped.values():
            members.sort(key=lambda doc: (doc.get('version_ordinal') is None,
                                          doc.get('version_ordinal') or 0,
                                          str(doc['_id'])))
        return grouped

    def pointer_ancestors(self, doc_id):
        """The ids before *doc_id* in its chain, oldest last, by pointer walk.

        Stops on a cycle or a dangling pointer rather than looping: both are
        I5's findings, and a checker that hangs reports nothing at all.
        """
        seen, walk = [], self.by_id[doc_id].get('previous_version_id')
        while walk is not None and walk not in seen:
            seen.append(walk)
            doc = self.by_id.get(walk)
            if doc is None:
                break
            walk = doc.get('previous_version_id')
        return seen

    def array_ancestors(self, doc):
        """The ids named by this document's previous_versions[], as ObjectIds.

        References that do not resolve are dropped: a dangling entry is I6's
        finding and a legacy-encoded one is I19's, and reporting either here
        again would make one defect look like three.
        """
        resolved = []
        for linkid, _encoding in iter_lineage_references(doc):
            try:
                target = ObjectId(linkid)
            except Exception:
                continue
            if target in self.by_id:
                resolved.append(target)
        return resolved

    def ids_with_status(self, *statuses):
        return {doc_id for doc_id, status in self.status.items() if status in statuses}

    def missing_gridfs_files(self, file_ids):
        """The subset of *file_ids* with no row in ``fs.files``."""
        wanted = [ObjectId(str(f)) for f in file_ids]
        missing = set(wanted)
        for start in range(0, len(wanted), _GRIDFS_BATCH):
            batch = wanted[start:start + _GRIDFS_BATCH]
            for row in self.fs_files.find({'_id': {'$in': batch}}, {'_id': 1}):
                missing.discard(row['_id'])
        return missing


# ---------------------------------------------------------------------------
# The checks
#
# Each one that needs a field calls snap.require() first, so "cannot check
# this" is a measurement of the database in front of it rather than a sentence
# somebody wrote once.
# ---------------------------------------------------------------------------

_I1_FIELDS = ('status', 'current', 'delete', 'version_chain_id',
              'version_ordinal', 'is_latest')


def _check_i1(snap):
    """Every project document carries all six status and lineage fields.

    Absence is the whole point.  The head flag was absent on 70 production
    documents, and every reader that defaulted it to False read all 70 as "not
    the head" -- the field's absence was load-bearing, and no query could say
    so.  This is the invariant that stops that from coming back by a different
    field name.
    """
    snap.require('status', 'version_chain_id', 'version_ordinal', 'is_latest')
    findings = []
    for doc in snap.documents:
        missing = [field for field in _I1_FIELDS if field not in doc]
        if missing:
            findings.append(Finding(
                'I1', doc['_id'], doc.get('project_name'),
                f'absent: {", ".join(missing)}'))
    return findings


def _check_i2(snap):
    """The stored status equals the one classify() computes from the document.

    The single most valuable check here.  Both incidents in the spec were a
    reader deciding a document's state from flags, by a rule that had drifted
    from the rule the writer used.  Storing the status does not fix that on its
    own -- it adds a second copy, which is this codebase's recurring defect --
    and this is what makes the second copy safe: the two may never disagree
    silently.

    Documents with no stored status at all are I1's finding, counted here in
    one line rather than repeated per document.
    """
    snap.require('status')
    findings, unstored = [], 0
    for doc in snap.documents:
        stored = doc.get('status')
        if stored is None:
            unstored += 1
            continue
        computed = snap.status[doc['_id']]
        if stored != computed:
            findings.append(Finding(
                'I2', doc['_id'], doc.get('project_name'),
                f'stored status {stored!r}, but classify() says {computed}'))
    if unstored:
        findings.append(Finding(
            'I2', None, None,
            f'{unstored} document(s) store no status, so there was nothing to '
            f'compare; I1 lists them'))
    return findings


def _check_i3(snap):
    """Exactly one is_latest=True document per version_chain_id.

    Two heads means two documents claim to be the current version of one
    project, and whichever the resolver reaches first wins.  No head means the
    project has no current version and every URL for it resolves to a past one.
    """
    snap.require('version_chain_id', 'is_latest')
    findings = []
    for chain_id, members in sorted(snap.chains().items(), key=lambda kv: str(kv[0])):
        heads = [doc['_id'] for doc in members if doc.get('is_latest') is True]
        if len(heads) == 1:
            continue
        findings.append(Finding(
            'I3', members[0]['_id'], members[0].get('project_name'),
            f'chain {chain_id} has {len(heads)} is_latest members across '
            f'{len(members)} version(s)'
            + (f': {", ".join(str(h) for h in heads[:4])}' if heads else '')))
    return findings


def _check_i4(snap):
    """version_ordinal is unique and contiguous from 1 within each chain.

    A gap or a tie means the chain cannot be put in order, and "the previous
    version" stops having an answer.  Checked as a set against
    ``range(1, n+1)`` rather than by sorting and comparing neighbours, so a
    duplicate and a gap are both caught by the same comparison.
    """
    snap.require('version_chain_id', 'version_ordinal')
    findings = []
    for chain_id, members in sorted(snap.chains().items(), key=lambda kv: str(kv[0])):
        ordinals = [doc.get('version_ordinal') for doc in members]
        expected = list(range(1, len(members) + 1))
        if sorted(o for o in ordinals if isinstance(o, int)) == expected \
                and len(ordinals) == len(expected) \
                and all(isinstance(o, int) for o in ordinals):
            continue
        findings.append(Finding(
            'I4', members[0]['_id'], members[0].get('project_name'),
            f'chain {chain_id} has ordinals {ordinals}, expected '
            f'{expected} in some order'))
    return findings


def _check_i5(snap):
    """previous_version_id and next_version_id are mutual inverses.

    This used to also require ``is_latest`` to agree with ``next_version_id is
    None``, on the reasoning that a flag which can disagree with the pointer it
    is derived from is the same two-copies defect as a status that can disagree
    with classify().  That reasoning was wrong, and the write paths are what
    made it obvious: **is_latest is not derived from the pointers.**

    When the head version is deleted, the version before it is promoted.  The
    deleted version stays in the chain as a tombstone -- it keeps its ordinal
    and its neighbours keep pointing at it, because it is a node whose payload
    is gone rather than a node that is gone.  So the promoted head is
    is_latest=True *and* has a next_version_id.  Requiring the two to agree
    would have made every correct head deletion a violation.

    Pointers are structure, is_latest is position, status is state.  Exactly
    one head per chain is I3 and I16, and that is where it belongs.
    """
    snap.require('previous_version_id', 'next_version_id')
    findings = []
    for doc in snap.documents:
        doc_id = doc['_id']
        name = doc.get('project_name')

        nxt = doc.get('next_version_id')
        if nxt is not None:
            target = snap.by_id.get(nxt)
            if target is None:
                findings.append(Finding('I5', doc_id, name,
                                        f'next_version_id {nxt} is not in the collection'))
            elif target.get('previous_version_id') != doc_id:
                findings.append(Finding(
                    'I5', doc_id, name,
                    f'next_version_id {nxt}, but that document\'s '
                    f'previous_version_id is {target.get("previous_version_id")}'))

        prev = doc.get('previous_version_id')
        if prev is not None:
            target = snap.by_id.get(prev)
            if target is None:
                findings.append(Finding('I5', doc_id, name,
                                        f'previous_version_id {prev} is not in the collection'))
            elif target.get('next_version_id') != doc_id:
                findings.append(Finding(
                    'I5', doc_id, name,
                    f'previous_version_id {prev}, but that document\'s '
                    f'next_version_id is {target.get("next_version_id")}'))

    return findings


def _check_i11(snap):
    """The denormalised previous_versions[] agrees with the pointer lineage.

    The invariant Phase 2 rests on.  Read paths move onto the pointers while
    the array is still written, and this is the only thing standing between
    "the pointers are a faithful index of the array" and "the site now renders
    a history nobody has compared to the one it rendered yesterday".

    Both directions are reported, because they fail for different reasons.  An
    entry the pointers do not place before the document means the array claims
    an ancestor the chain does not have.  An ancestor the array does not name
    means the history table has been rendering short -- which is a defect that
    predates the pointers and that switching the read path *fixes*, so it is
    reported as its own detail rather than folded in with the other direction.

    **Tombstones are excluded from the comparison wherever they appear**: as
    the document being checked, as a pointer ancestor, and as an array entry.
    The array has no way to say "this version was deleted", so deleting a
    version removes it from the array while it stays in the chain holding its
    ordinal.  From the first deletion onwards the pointer lineage is therefore
    a strict superset of the array, by design and not by drift; comparing
    across the tombstones would report every correct deletion as a divergence.
    A tombstone's own array is not compared either -- it is written by
    ``replace_one`` and does not carry one.

    The array side was the exclusion this docstring claimed and the code did
    not make, and re-populating an emptied project is what found the gap.  T9
    builds a new version on top of a tombstone, and the array names it because
    the array is also what tells the write path which chain to extend -- so a
    correct T9 was reported as a divergence on dev, 2026-08-28.  Filtering one
    side and not the other does not compare two encodings of the same history;
    it compares one of them against a subset of the other.

    That asymmetry is the whole reason the pointers exist.  It is also why this
    invariant is scoped to the compatibility window: it holds the two encodings
    together where they can still be compared, which is over the versions that
    are still there.
    """
    snap.require('previous_version_id', 'version_chain_id')
    findings = []
    tombstones = snap.ids_with_status(TOMBSTONE)
    for doc in snap.documents:
        doc_id = doc['_id']
        if 'previous_version_id' not in doc:
            continue                      # I1 owns the absence
        if doc_id in tombstones:
            continue
        pointed = [i for i in snap.pointer_ancestors(doc_id) if i not in tombstones]
        named = [i for i in snap.array_ancestors(doc) if i not in tombstones]
        if set(pointed) == set(named):
            continue

        name = doc.get('project_name')
        extra = [i for i in named if i not in set(pointed)]
        absent = [i for i in pointed if i not in set(named)]
        if extra:
            findings.append(Finding(
                'I11', doc_id, name,
                f'previous_versions[] names {len(extra)} document(s) the '
                f'pointers do not place before it: '
                f'{", ".join(str(i) for i in extra[:4])}'))
        if absent:
            how = ('previous_versions[] is absent entirely'
                   if 'previous_versions' not in doc else
                   f'previous_versions[] names {len(named)}')
            findings.append(Finding(
                'I11', doc_id, name,
                f'pointers place {len(pointed)} document(s) before it but '
                f'{how}; missing: {", ".join(str(i) for i in absent[:4])}'))
    return findings


def _EMPTY_chains(snap):
    """The chain ids every one of whose members is a TOMBSTONE.

    Derived here and nowhere else, which is I15's actual content.
    """
    return {chain_id for chain_id, members in snap.chains().items()
            if members and all(snap.status[doc['_id']] == TOMBSTONE
                               for doc in members)}


# Anything that would amount to storing chain emptiness rather than deriving it.
_STORED_EMPTINESS_KEYS = ('chain_empty', 'is_empty', 'empty_chain')


def _check_i15(snap):
    """A chain is EMPTY iff every member is a TOMBSTONE -- and it is never stored.

    "Never stored" is the checkable half, and it is the half that matters: an
    emptiness flag on a document is a second opinion about a thing that already
    has an answer, and it goes stale the first time a version is restored.  The
    derivation itself has nothing to disagree with yet -- when the derived
    ``project_version_chains`` view lands in Phase 2 it will, and I9 is where
    that comparison belongs.

    The derived population is printed by --report so it is countable rather
    than merely asserted.
    """
    snap.require('version_chain_id')
    findings = []
    for doc in snap.documents:
        stored = [key for key in _STORED_EMPTINESS_KEYS if key in doc]
        if doc.get('status') == 'EMPTY':
            stored.append("status='EMPTY'")
        if stored:
            findings.append(Finding(
                'I15', doc['_id'], doc.get('project_name'),
                f'stores chain emptiness rather than deriving it: '
                f'{", ".join(stored)}'))

    # The chain view is the obvious place for someone to cache this, and it is
    # the one place where it would look reasonable -- the chain is exactly the
    # scope the property belongs to. It still must not be stored: the view is
    # rebuilt from documents, so a cached emptiness would be correct only until
    # a version was restored between rebuilds.
    if snap.chain_view is not None:
        for doc in snap.chain_view.find({}, {key: 1 for key in _STORED_EMPTINESS_KEYS}):
            stored = [key for key in _STORED_EMPTINESS_KEYS if key in doc]
            if stored:
                findings.append(Finding(
                    'I15', doc['_id'], None,
                    f'the chain view stores emptiness rather than deriving it '
                    f'from its members: {", ".join(stored)}'))
    return findings


def _check_i16(snap):
    """Every chain has exactly one is_latest member -- empty chains included.

    I3 says this for all chains, and where I3 passes this passes; it is
    separate because of the population it names.  A chain with no LIVE member
    is the one a future "tidy up the dead projects" pass would be tempted to
    leave headless, and T6 in the spec turns on the opposite: a project whose
    versions have all been deleted is an empty project, not an absent one, and
    it still has a current version to render, restore into and redirect to.

    is_latest is position and status is state.  A head that is a TOMBSTONE is
    correct, not a violation, and nothing here should be tempted to repair it.
    """
    snap.require('version_chain_id', 'is_latest')
    findings = []
    for chain_id, members in sorted(snap.chains().items(), key=lambda kv: str(kv[0])):
        if any(snap.status[doc['_id']] == LIVE for doc in members):
            continue                      # I3's population, checked there
        heads = [doc for doc in members if doc.get('is_latest') is True]
        if len(heads) != 1:
            findings.append(Finding(
                'I16', members[0]['_id'], members[0].get('project_name'),
                f'chain {chain_id} has no LIVE member and {len(heads)} '
                f'is_latest member(s) across {len(members)} version(s)'))
    return findings


def _check_i20(snap):
    """No chain is headed by a TOMBSTONE while a non-TOMBSTONE member survives.

    I16 deliberately allows a tombstone head, because that is what an emptied
    project is: every version deleted, the last one still holding the position
    a restore lands in.  What it cannot allow is a tombstone holding the head
    while a version that was never deleted sits beside it -- that is a project
    whose current version is one the user deleted, and whose surviving version
    is unreachable as the head.

    This exists because the data said so before any rule did.  Deleting the
    head of a three-version chain on dev, 2026-08-28, promoted the tombstone
    left by the previous deletion instead of the one surviving version:
    plan_deletion() asked is_tombstone() of documents fetched under a
    projection that dropped both markers, so every tombstone read as a
    survivor.  Every invariant then in place passed over the result -- exactly
    one head, ordinals contiguous, pointers mutual, payloads purged.  The shape
    was wrong and nothing was looking for it.
    """
    snap.require('version_chain_id', 'is_latest')
    findings = []
    for chain_id, members in sorted(snap.chains().items(), key=lambda kv: str(kv[0])):
        heads = [doc for doc in members if doc.get('is_latest') is True]
        if len(heads) != 1:
            continue                      # I3 and I16 own that
        head = heads[0]
        if snap.status[head['_id']] != TOMBSTONE:
            continue
        survivors = [doc for doc in members
                     if snap.status[doc['_id']] != TOMBSTONE]
        if survivors:
            findings.append(Finding(
                'I20', head['_id'], snap.name(head['_id']),
                f'chain {chain_id} is headed by a TOMBSTONE while '
                f'{len(survivors)} version(s) survive it, the newest being '
                f'{survivors[-1]["_id"]} (ordinal '
                f'{survivors[-1].get("version_ordinal")})'))
    return findings


def _check_i9(snap):
    """The derived chain view's source_digest matches the documents it came from.

    Unavailable until something writes the view.  Measured, not assumed: the
    collection is counted rather than declared missing, so the day the rebuild
    command lands this stops skipping on its own and says what it now needs.
    """
    if snap.chain_view is None or snap.chain_view.count_documents({}) == 0:
        raise Unavailable(
            'a derived project_version_chains collection. There are 0 chain '
            'documents in this database, so there is no second copy to compare '
            'the documents against yet.')

    from caper.version_chains import head_of, order_members, source_digest

    findings = []
    chains = snap.chains()
    stored = {doc['_id']: doc for doc in snap.chain_view.find(
        {}, {'source_digest': 1, 'head_project_id': 1})}

    for chain_id, members in sorted(chains.items(), key=lambda kv: str(kv[0])):
        view = stored.get(chain_id)
        if view is None:
            findings.append(Finding(
                'I9', chain_id, snap.name(members[0]['_id']) if members else None,
                f'chain {chain_id} has {len(members)} member document(s) but no '
                f'chain document; the view has not been rebuilt since it was '
                f'created'))
            continue
        expected = source_digest(members)
        if view.get('source_digest') != expected:
            findings.append(Finding(
                'I9', chain_id, snap.name(members[0]['_id']) if members else None,
                f'chain {chain_id} digest {str(view.get("source_digest"))[:12]}… '
                f'but the documents digest {expected[:12]}… -- the view is stale '
                f'and the documents win; rebuild it'))
            continue
        # The digest covers (id, ordinal, status). The head is the one derived
        # field it does not cover, and it is the field feature code reads most,
        # so it is compared directly rather than trusted.
        head = head_of(order_members(members))
        expected_head = head['_id'] if head is not None else None
        if view.get('head_project_id') != expected_head:
            findings.append(Finding(
                'I9', chain_id, snap.name(members[0]['_id']) if members else None,
                f'chain {chain_id} names head {view.get("head_project_id")} but '
                f'the documents say {expected_head}'))

    for chain_id in sorted(set(stored) - set(chains), key=str):
        findings.append(Finding(
            'I9', chain_id, None,
            f'chain document {chain_id} has no member documents left; the view '
            f'outlived the chain it describes'))
    return findings


def _check_i13(snap):
    """Every fs.files row naming a document is still referenced by that document.

    The reverse of I12, and the check that turns "is this file orphaned?" from
    a full scan of every project document into a lookup.  Needs the backlink
    Phase 2b writes; counted here rather than asserted absent.
    """
    if snap.gridfs_skipped:
        raise Unavailable('a run without --skip-gridfs')
    with_backlink = snap.fs_files.count_documents(
        {'metadata.project_id': {'$exists': True}})
    if with_backlink == 0:
        raise Unavailable(
            'metadata.project_id on fs.files rows. 0 of '
            f'{snap.fs_files.count_documents({})} files carry one, so no file '
            'can name the document it belongs to.')

    from caper.gridfs_backlinks import METADATA_FIELD, PROJECT_ID

    findings = []
    # Backlinks that name a document which does not exist. The reverse
    # direction -- a document naming a file with no row -- is I12's.
    known = {doc['_id'] for doc in snap.documents}
    for project_id in snap.fs_files.distinct(f'{METADATA_FIELD}.{PROJECT_ID}'):
        if project_id in known:
            continue
        count = snap.fs_files.count_documents(
            {f'{METADATA_FIELD}.{PROJECT_ID}': project_id})
        findings.append(Finding(
            'I13', project_id, None,
            f'{count} fs.files row(s) name project {project_id}, which has no '
            f'document; residue of a purge or permanent delete'))

    # A file whose document exists but no longer names it. Not automatically a
    # defect -- it is the residue a version edit leaves, and the report grades
    # it -- so only the tombstone case is reported here, where the payload was
    # supposed to have been purged and was not.
    for doc in snap.documents:
        if snap.status[doc['_id']] != TOMBSTONE:
            continue
        held = snap.fs_files.count_documents(
            {f'{METADATA_FIELD}.{PROJECT_ID}': doc['_id']})
        if held:
            findings.append(Finding(
                'I13', doc['_id'], snap.name(doc['_id']),
                f'TOMBSTONE still holding {held} labelled file(s); its payload '
                f'was supposed to have been purged'))
    return findings


def _check_i6(snap):
    """Every lineage reference resolves to a document that exists.

    About the target, not the encoding.  A reference written in the pre-April
    2024 JSON-string format still points at a real document, and saying so is
    the difference between "someone deleted a version" and "the reader is out
    of date" -- I19 reports the format separately.
    """
    findings = []
    for linkid, referrers in sorted(snap.referenced_by.items()):
        try:
            target = ObjectId(linkid)
        except Exception:
            for referrer in referrers:
                findings.append(Finding(
                    'I6', referrer, snap.name(referrer),
                    f'lineage reference {linkid!r} is not an ObjectId in any '
                    f'known encoding'))
            continue
        if target not in snap.by_id:
            for referrer in referrers:
                findings.append(Finding(
                    'I6', referrer, snap.name(referrer),
                    f'previous_versions[] names {linkid}, which is not in the collection'))
    return findings


def _check_i19(snap):
    """Every lineage reference is stored in the encoding the application reads.

    Separate from I6 because the consequence is different and so is the fix.  A
    dangling reference means a document is gone and the history is short by one
    entry.  A legacy-encoded reference means the document is right there and the
    reader cannot see it: previous_versions() turns the entry into
    ``{'linkid': '<the whole JSON text>'}``, the history table renders a link to
    /project/[{"date":...}], and the query the site uses to find a document's
    successors -- ``{'previous_versions.linkid': <id>}`` -- matches nothing, so
    both documents look unreferenced to every caller that asks that way,
    including the orphan check in check_project_flags.py.

    Nothing crashes.  That is why it survived two years.
    """
    findings = []
    for doc_id in sorted(snap.encodings, key=str):
        for linkid, encoding in snap.encodings[doc_id]:
            if encoding == CURRENT_ENCODING:
                continue
            if encoding == LEGACY_JSON_ENCODING:
                try:
                    target = ObjectId(linkid)
                except Exception:
                    target = None
                whereabouts = (
                    f'-> {snap.name(target)!r} ({snap.status[target]})'
                    if target in snap.by_id else '-> target missing too')
                findings.append(Finding(
                    'I19', doc_id, snap.name(doc_id),
                    f'lineage reference {linkid} is stored in the pre-April '
                    f'2024 JSON-string format {whereabouts}'))
            else:
                findings.append(Finding(
                    'I19', doc_id, snap.name(doc_id),
                    f'previous_versions[] entry is in no recognised encoding: '
                    f'{linkid[:60]!r}'))
    return findings


def _check_i7(snap):
    """No LIVE document is also listed in another document's history.

    A SUPERSEDED or TOMBSTONE document being referenced is the normal case --
    that is what a chain is.  A LIVE one being referenced means two chains
    claim it: its own page renders it as current, another project's history
    renders it as a past version, and deleting or promoting from either side
    corrupts the other.
    """
    findings = []
    for doc_id in sorted(snap.ids_with_status(LIVE), key=str):
        referrers = [r for r in snap.referenced_by.get(str(doc_id), []) if r != doc_id]
        if referrers:
            names = ', '.join(f'{r} ({snap.name(r)})' for r in referrers)
            findings.append(Finding(
                'I7', doc_id, snap.name(doc_id),
                f'LIVE, but listed in the history of: {names}'))
    return findings


def _check_i8(snap):
    """payload_purged implies TOMBSTONE and no GridFS ids left on the document."""
    findings = []
    for doc in snap.documents:
        if doc.get('payload_purged') is not True:
            continue
        status = snap.status[doc['_id']]
        if status != TOMBSTONE:
            findings.append(Finding(
                'I8', doc['_id'], doc.get('project_name'),
                f'payload_purged=True but classify() says {status}'))
        if not snap.gridfs_skipped:
            remaining = snap.gridfs_ids.get(doc['_id'], [])
            if remaining:
                findings.append(Finding(
                    'I8', doc['_id'], doc.get('project_name'),
                    f'payload_purged=True but still names {len(remaining)} GridFS '
                    f'file(s): {", ".join(str(f) for f in remaining[:4])}'))
    return findings


def _check_i10(snap):
    """get_one_project() resolves every LIVE and SUPERSEDED document by _id.

    Asked of the database rather than of ``is_reachable_by_url()``, which would
    only re-derive ``classify()`` and agree with itself.  Two queries -- the
    resolver's two ``_id`` steps, which are the only two that can answer the
    question for a named document -- and the answer is whether each id is in
    the union.
    """
    reachable = set()
    for query in (NOT_DELETED_QUERY, PRIOR_VERSION_QUERY):
        reachable.update(row['_id'] for row in snap.collection.find(query, {'_id': 1}))

    findings = []
    for doc_id in sorted(snap.ids_with_status(LIVE, SUPERSEDED), key=str):
        if doc_id not in reachable:
            findings.append(Finding(
                'I10', doc_id, snap.name(doc_id),
                f'{snap.status[doc_id]} but neither _id step of get_one_project() '
                f'returns it'))
        elif not is_reachable_by_url(snap.by_id[doc_id]):
            # The database says reachable and the in-memory mirror says not.
            # That is the drift this whole spec exists to catch, so it is a
            # finding in its own right rather than a disagreement to ignore.
            findings.append(Finding(
                'I10', doc_id, snap.name(doc_id),
                'the database resolves it but is_reachable_by_url() says it '
                'does not -- the two forms have drifted'))
    return findings


def _check_i21(snap):
    """No GridFS file is named by more than one document.

    Deletion assumes this. ``delete_gridfs_payload_for_project()`` deletes every
    file the project it is given names, and the permanent-delete path walks the
    document's runs and deletes each id it finds; neither asks whether anything
    else is using the file. That is correct only while nothing is shared, and
    nothing is: measured 2026-08-29, the distinct id count exactly equalled the
    (document, file) pair count on both databases -- 942,279 on prod, 602,577 on
    dev.

    So the safety of every deletion path rests on a measured fact rather than on
    a rule, and a measured fact needs something watching it. If sharing ever
    appears -- a deduplicating upload, a version that reuses its predecessor's
    files instead of re-storing them -- deleting one owner silently takes the
    file out from under the others, and this is what says so first.

    Ids are deduplicated per document before counting owners: one document
    naming the same file from two slots is not sharing, and counting it as such
    would make this fire on the ordinary case.
    """
    if snap.gridfs_skipped:
        return []
    owners = {}
    for doc_id, file_ids in snap.gridfs_ids.items():
        for file_id in {ObjectId(str(f)) for f in file_ids}:
            owners.setdefault(file_id, []).append(doc_id)

    findings = []
    for file_id, doc_ids in sorted(owners.items(), key=lambda kv: str(kv[0])):
        if len(doc_ids) < 2:
            continue
        named = ", ".join(f'{snap.name(d) or d}' for d in doc_ids[:4])
        findings.append(Finding(
            'I21', doc_ids[0], snap.name(doc_ids[0]),
            f'GridFS file {file_id} is named by {len(doc_ids)} documents '
            f'({named}); every deletion path assumes exactly one, so deleting '
            f'any one of them takes the file from the others'))
    return findings


def _check_i12(snap):
    """Every GridFS id named by a retained document exists in fs.files."""
    if snap.gridfs_skipped:
        return []
    wanted = {}
    for doc_id, file_ids in snap.gridfs_ids.items():
        if snap.status[doc_id] == TOMBSTONE:
            continue                      # I14's problem, not I12's
        for file_id in file_ids:
            wanted.setdefault(ObjectId(str(file_id)), []).append(doc_id)

    findings = []
    for missing in sorted(snap.missing_gridfs_files(wanted), key=str):
        for doc_id in wanted[missing]:
            findings.append(Finding(
                'I12', doc_id, snap.name(doc_id),
                f'{snap.status[doc_id]} document names GridFS file {missing}, '
                f'which has no fs.files row'))
    return findings


def _check_i14(snap):
    """No TOMBSTONE document has any GridFS id remaining.

    Half of I14.  The other half -- that no ``fs.files`` row points at a
    tombstone -- reads the backlink rather than the document, and I13 reports
    it: a tombstone still holding labelled files is a deletion that did not
    finish.
    """
    if snap.gridfs_skipped:
        return []
    findings = []
    for doc_id in sorted(snap.ids_with_status(TOMBSTONE), key=str):
        remaining = snap.gridfs_ids.get(doc_id, [])
        if remaining:
            findings.append(Finding(
                'I14', doc_id, snap.name(doc_id),
                f'TOMBSTONE still names {len(remaining)} GridFS file(s): '
                f'{", ".join(str(f) for f in remaining[:4])}'))
    return findings


# The two modules allowed to spell the tombstone markers as a literal:
# project_status.py defines the predicate, project_version_cleanup.py is the one
# routine that writes it.  Everything else must go through them.
_TOMBSTONE_OWNERS = (
    os.path.join('caper', 'caper', 'project_status.py'),
    os.path.join('caper', 'caper', 'project_version_cleanup.py'),
)

_TOMBSTONE_MARKER_LITERAL = None    # compiled on first use


def i18_hand_written_tombstones():
    """Every place outside the two owners that spells a tombstone marker as a
    dict key -- ``'version_deleted_from_history': ...`` or ``'payload_purged': ...``.

    Returns ``[(relative_path, line_number, text)]``.

    Deliberately blind to whether the dict is a filter or a ``$set``: both are
    the same mistake.  A query that hand-writes the tombstone predicate drifts
    from the definition exactly the way a write does, and this check found one
    of each -- a filter in ``utils.py`` that the grep guard walked past
    because it only looked for ``delete`` and ``current``, and the sole-version
    deletion path in ``views.py`` that builds a half-tombstone of its own
    instead of calling ``build_deleted_version_tombstone``.

    ``entry.setdefault('version_deleted_from_history', True)`` in
    ``utils.py`` is not matched and should not be: it marks a *history display
    entry*, a dict rendered in a template, not a project document.  The pattern
    requires the key-and-colon form, so that distinction is structural rather
    than a special case someone has to remember.
    """
    global _TOMBSTONE_MARKER_LITERAL
    if _TOMBSTONE_MARKER_LITERAL is None:
        import re
        _TOMBSTONE_MARKER_LITERAL = re.compile(
            r"""['"](?:version_deleted_from_history|payload_purged)['"]\s*:""")

    package = os.path.join(_REPO_ROOT, 'caper', 'caper')
    if not os.path.isdir(package):
        raise Unavailable(
            f'no application source at {package}. I18 reads code, not '
            f'documents, so it cannot be checked from a directory that does '
            f'not hold the checkout (set VALIDATOR_REPO_ROOT, or run this from '
            f'the repository).')

    found = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_REPO_ROOT, 'caper')):
        dirnames[:] = [d for d in dirnames if d not in ('__pycache__', 'migrations')]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            path = os.path.join(dirpath, filename)
            relative = os.path.relpath(path, _REPO_ROOT)
            if relative in _TOMBSTONE_OWNERS:
                continue
            with open(path, encoding='utf-8', errors='replace') as handle:
                for number, line in enumerate(handle, 1):
                    if _TOMBSTONE_MARKER_LITERAL.search(line):
                        found.append((relative, number, line.strip()))
    return found


def _check_i18(snap):
    """Exactly one routine creates tombstones.

    The only invariant here that reads source rather than documents, and it
    belongs in the validator anyway: "every deletion path calls the one
    tombstone builder" is not observable in the data.  A database whose
    deletion paths have drifted is byte-identical to one whose have not, right
    up to the day one of them runs.

    Takes *snap* only so every checker has one signature; it reads no documents.
    """
    return [Finding('I18', None, None,
                    f'{path}:{number} spells a tombstone marker by hand: {text}')
            for path, number, text in i18_hand_written_tombstones()]


# ---------------------------------------------------------------------------
# The invariant table -- all eighteen, in order
# ---------------------------------------------------------------------------

class Invariant:
    def __init__(self, ident, text, check=None, needs=None, partial=None):
        self.ident = ident
        self.text = text
        self.check = check
        self.needs = needs
        self.partial = partial


_POINTERS = 'previous_version_id / next_version_id'

INVARIANTS = (
    Invariant('I1', 'Every project document has status, current, delete, '
                    'version_chain_id, version_ordinal, is_latest -- all present',
              check=_check_i1),
    Invariant('I2', 'Stored status equals classify(doc) recomputed from the document',
              check=_check_i2),
    Invariant('I3', 'Exactly one is_latest=True document per version_chain_id',
              check=_check_i3),
    Invariant('I4', 'version_ordinal is unique and contiguous from 1 within a chain',
              check=_check_i4),
    Invariant('I5', f'{_POINTERS} are mutual inverses', check=_check_i5),
    Invariant('I6', 'Every lineage reference resolves to an existing document',
              check=_check_i6),
    Invariant('I7', 'No document is both LIVE and referenced as another chain\'s '
                    'member', check=_check_i7),
    Invariant('I8', 'payload purged implies TOMBSTONE and no GridFS ids remain',
              check=_check_i8),
    Invariant('I9', 'Chain document source_digest matches its members',
              check=_check_i9),
    Invariant('I10', 'get_one_project() resolves every LIVE and every SUPERSEDED '
                     'document by _id', check=_check_i10),
    Invariant('I11', 'previous_versions[] matches the lineage derived from pointers',
              check=_check_i11),
    Invariant('I12', 'Every GridFS id named by a retained document exists in fs.files',
              check=_check_i12),
    Invariant('I13', 'Every fs.files row naming an existing document is still '
                     'referenced by it',
              check=_check_i13),
    Invariant('I14', 'No TOMBSTONE has GridFS ids left, and no fs.files row points '
                     'at one', check=_check_i14,
              partial='the document half only; "no fs.files row points at one" '
                      'is reported by I13, which reads the backlink'),
    Invariant('I15', 'A chain is EMPTY iff every member is a TOMBSTONE',
              check=_check_i15,
              partial='the "never stored" half, now covering the chain view as '
                      'well as the documents. The derived half has nothing to '
                      'compare against by design -- the view deliberately does '
                      'not store emptiness, so there is no second opinion to '
                      'disagree with; that the view matches its members at all '
                      'is I9'),
    Invariant('I16', 'Every chain has exactly one is_latest member',
              check=_check_i16),
    Invariant('I17', 'Every chain-level field survives the emptying of a chain',
              needs='code that declares the split. The decision was taken on '
                    '2026-08-27: project_downloads, sample_downloads and the '
                    'alias pair are chain-level, sample_name_remap_enabled is '
                    'version-level, and owner and original_project_name belong '
                    'to neither -- they are upload scaffolding that a finished '
                    'project must not carry. Nothing declares that in code yet; '
                    'promotion still copies a hand-written list of 9 fields '
                    '(views.py, delete_project_version), so there is no set for '
                    'this check to read'),
    Invariant('I18', 'Exactly one tombstone-creation routine exists and every '
                     'deletion path calls it', check=_check_i18),
    Invariant('I19', 'Every lineage reference is stored in the encoding the '
                     'application reads', check=_check_i19),
    Invariant('I20', 'No chain is headed by a TOMBSTONE while a surviving '
                     'version sits beside it', check=_check_i20),
    Invariant('I21', 'No GridFS file is named by more than one document',
              check=_check_i21),
)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _payload_summary(snap, doc_id):
    if snap.gridfs_skipped:
        return 'not checked'
    count = len(snap.gridfs_ids.get(doc_id, []))
    return f'{count} file(s)' if count else 'none'


def _detached_variant(doc):
    """The flags this document actually carries, for the report column.

    Deliberately not a predicate.  The detached population splits in two --
    documents with no ``current`` field, and documents with both flags false --
    and writing that split as a rule here would be a third copy of the thing
    ``project_status`` exists to hold.  ``classify()`` has already said
    DETACHED; this only formats what is on the document, so a reader can sort
    the report by eye without anything having decided anything.

    (The grep guard caught the first version of this function doing it the
    other way, which is the second time on this branch that it has been right
    and I have been wrong.)
    """
    return ', '.join(f'{field}={doc[field]!r}' if field in doc else f'{field} absent'
                     for field in ('delete', 'current'))


def _report(snap, out):
    out('')
    out('=' * 78)
    out('REPORT -- read-only. Nothing here is a decision.')
    out('=' * 78)

    detached = sorted(snap.ids_with_status(DETACHED), key=str)
    out('')
    out(f'DETACHED documents: {len(detached)}')
    out('  Documents whose meaning cannot be determined from the schema.')
    out('  Deciding their fate is a human call this tool does not make; the list')
    out('  exists so the population is countable instead of invisible.')
    if detached:
        out('')
        out(f'  {"_id":<26} {"project_name":<30} {"date":<21} {"creator":<26} '
            f'{"payload":<12} {"URL":<5} variant')
        for doc_id in detached:
            doc = snap.by_id[doc_id]
            out(f'  {str(doc_id):<26} {str(doc.get("project_name"))[:28]:<30} '
                f'{str(doc.get("date") or doc.get("date_created"))[:19]:<21} '
                f'{str(doc.get("creator") or doc.get("owner"))[:24]:<26} '
                f'{_payload_summary(snap, doc_id):<12} '
                f'{"yes" if is_reachable_by_url(doc) else "no":<5} '
                f'{_detached_variant(doc)}')

    dangling = _check_i6(snap)
    out('')
    out(f'Dangling lineage references: {len(dangling)}')
    out('  A history entry naming a document that is not in the collection. The')
    out('  history page drops it silently, so the chain reads shorter than it was.')
    for finding in dangling:
        out(f'  {finding.line()}')

    conflicts = _check_i7(snap)
    out('')
    out(f'Live-and-superseded conflicts: {len(conflicts)}')
    out('  A document that is the head of its own chain and a past version of')
    out('  another. Promotion or deletion from either side corrupts the other.')
    for finding in conflicts:
        out(f'  {finding.line()}')

    chains = snap.chains()
    if chains:
        empty = _EMPTY_chains(snap)
        sizes = defaultdict(int)
        for members in chains.values():
            sizes[len(members)] += 1
        out('')
        out(f'Chains: {len(chains)} over {sum(len(m) for m in chains.values())} '
            f'documents')
        for size in sorted(sizes):
            out(f'  {size:>3} version(s): {sizes[size]:>4} chain(s)')
        out(f'  EMPTY (every member a TOMBSTONE): {len(empty)}')
        out('  EMPTY is derived here and stored nowhere. A chain with no live')
        out('  version is still a project, and still has a head to restore into.')

    out('')
    out('Census by status:')
    counts = defaultdict(int)
    for status in snap.status.values():
        counts[status] += 1
    for status in (LIVE, SUPERSEDED, 'SOFT_DELETED', TOMBSTONE, DETACHED):
        out(f'  {status:<14} {counts[status]:>5}')
    out(f'  {"TOTAL":<14} {len(snap.documents):>5}')


# ---------------------------------------------------------------------------
# Target guard and entry point
# ---------------------------------------------------------------------------

def _connect(expect_db):
    import pymongo
    from pymongo import uri_parser

    uri = os.environ.get('DB_URI_SECRET')
    db_name = os.environ.get('DB_NAME')
    if not uri or not db_name:
        raise SystemExit("DB_URI_SECRET and DB_NAME must be set. Run:\n"
                         "    set -a; source caper/config.sh; set +a")

    hosts = uri_parser.parse_uri(uri)['nodelist']
    local = all(host in ('localhost', '127.0.0.1', 'mongodb', '::1')
                for host, _port in hosts)

    # The database name alone does not identify the target: dev's database is
    # also called 'caper-dev', so a laptop satisfies --expect-db caper-dev while
    # measuring twenty-four documents that prove nothing. Both halves go in the
    # label, and the label heads the output.
    if expect_db is not None and db_name != expect_db:
        raise SystemExit(f"connected to database {db_name!r}, but --expect-db says "
                         f"{expect_db!r}. Check which config.sh is sourced.")
    if expect_db is None and not local:
        raise SystemExit(
            f"refusing to measure a remote database ({db_name!r}) without being "
            f"told which one was intended: pass --expect-db {db_name}.")

    database = pymongo.MongoClient(uri)[db_name]
    return database, f'{db_name} ({"local" if local else "remote"})'


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Validate project lineage invariants. Read-only.')
    parser.add_argument('--expect-db', metavar='NAME',
                        help='the database name this run intends to read; '
                             'required for any database not on this machine')
    parser.add_argument('--report', action='store_true',
                        help='also print the report: every DETACHED document, '
                             'dangling reference and live/superseded conflict')
    parser.add_argument('--skip-gridfs', action='store_true',
                        help='skip the checks that read GridFS (I8 partly, I12, '
                             'I14). Faster, and strictly less coverage.')
    parser.add_argument('--verbose', action='store_true',
                        help='print every finding rather than the first 20 per '
                             'invariant')
    args = parser.parse_args(argv)

    database, label = _connect(args.expect_db)
    lines = []

    def out(text=''):
        lines.append(text)
        print(text)

    out(f'target: {label}')
    snap = Snapshot(database['projects'], database['fs.files'],
                    skip_gridfs=args.skip_gridfs,
                    chain_view=database['project_version_chains'])
    out(f'{len(snap.documents)} documents, '
        f'{sum(len(v) for v in snap.gridfs_ids.values())} GridFS references')
    if args.skip_gridfs:
        out('GridFS checks skipped (--skip-gridfs): I12 and I14 did not run, '
            'and I8 checked only its status half.')
    out('')

    failed = []
    checked = skipped = 0
    for invariant in INVARIANTS:
        if invariant.check is None:
            skipped += 1
            out(f'  SKIP  {invariant.ident:<4} {invariant.text}')
            out(f'        needs: {invariant.needs}')
            continue
        if args.skip_gridfs and invariant.ident in ('I12', 'I14'):
            skipped += 1
            out(f'  SKIP  {invariant.ident:<4} {invariant.text}')
            out('        needs: a run without --skip-gridfs')
            continue

        try:
            findings = invariant.check(snap)
        except Unavailable as unavailable:
            skipped += 1
            out(f'  SKIP  {invariant.ident:<4} {invariant.text}')
            out(f'        needs: {unavailable}')
            continue
        checked += 1
        if findings:
            failed.append(invariant.ident)
            out(f'  FAIL  {invariant.ident:<4} {invariant.text}  '
                f'-- {len(findings)} finding(s)')
            shown = findings if args.verbose else findings[:20]
            for finding in shown:
                out(f'        {finding.line()}')
            if len(shown) < len(findings):
                out(f'        ... and {len(findings) - len(shown)} more '
                    f'(pass --verbose)')
        else:
            out(f'  ok    {invariant.ident:<4} {invariant.text}')
        if invariant.partial:
            out(f'        partial: {invariant.partial}')

    if args.report:
        _report(snap, out)

    out('')
    out(f'{label}: {checked} invariant(s) checked, {skipped} not yet checkable, '
        f'{len(failed)} failing{": " + ", ".join(failed) if failed else ""}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
