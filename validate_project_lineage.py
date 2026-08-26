#!/usr/bin/env python
"""
What has to be true about project version history, checked against a database.

Eighteen invariants.  Six of them can be evaluated against the schema as it
stands; the other twelve are about fields that do not exist yet -- a stored
``status``, ``version_chain_id``, ``version_ordinal``, ``is_latest``, and the
``previous_version_id`` / ``next_version_id`` pointers that would replace the
denormalised ``previous_versions[]`` array.  This script declares all eighteen
and reports each as ok, FAIL or SKIP with the reason.

Declaring the ones that cannot run is the point.  A validator that quietly
implements the six checkable invariants and prints "all checks passed" is the
same mistake as the cleanup script that protected three of the four states it
needed to: a confident statement about a list, made by something that only knew
part of the list.  Here every gap names the field it is waiting for, so
coverage grows visibly as those fields land instead of being something a reader
has to reconstruct.

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

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if os.path.join(_REPO_ROOT, 'caper') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'caper'))

from bson import ObjectId                                          # noqa: E402

from caper.project_status import (                                 # noqa: E402
    DETACHED, LIVE, NOT_DELETED_QUERY, PRIOR_VERSION_QUERY, SUPERSEDED,
    TOMBSTONE, classify, is_reachable_by_url,
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

    def __init__(self, collection, fs_files, skip_gridfs=False):
        self.collection = collection
        self.fs_files = fs_files

        self.documents = list(collection.find({}, {field: 0 for field in _HEAVY_FIELDS}))
        self.by_id = {doc['_id']: doc for doc in self.documents}
        self.status = {doc['_id']: classify(doc) for doc in self.documents}

        # linkid string -> ids of the documents whose previous_versions[] names
        # it.  Built once; I6 and I7 both walk it from opposite ends.
        self.referenced_by = defaultdict(list)
        for doc in self.documents:
            for entry in doc.get('previous_versions') or []:
                linkid = entry.get('linkid') if isinstance(entry, dict) else entry
                if linkid:
                    self.referenced_by[str(linkid)].append(doc['_id'])

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

    def name(self, doc_id):
        doc = self.by_id.get(doc_id) or {}
        return doc.get('project_name')

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
# The checks that can run against today's schema
# ---------------------------------------------------------------------------

def _check_i6(snap):
    """Every previous_versions[].linkid resolves to a document that exists."""
    findings = []
    for linkid, referrers in sorted(snap.referenced_by.items()):
        try:
            target = ObjectId(linkid)
        except Exception:
            for referrer in referrers:
                findings.append(Finding(
                    'I6', referrer, snap.name(referrer),
                    f'previous_versions[] names {linkid!r}, which is not an ObjectId'))
            continue
        if target not in snap.by_id:
            for referrer in referrers:
                findings.append(Finding(
                    'I6', referrer, snap.name(referrer),
                    f'previous_versions[] names {linkid}, which is not in the collection'))
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
    tombstone -- needs a ``metadata.project_id`` on each ``fs.files`` row, which
    Until then a stranded file cannot be traced back to the document it came
    nothing writes today.  Until then a stranded file cannot be traced back to
    the document it came from at all.
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


_PHASE_1_FIELDS = 'status, version_chain_id, version_ordinal, is_latest'
_POINTERS = 'previous_version_id / next_version_id'

INVARIANTS = (
    Invariant('I1', 'Every project document has status, current, delete, '
                    'version_chain_id, version_ordinal, is_latest -- all present',
              needs=_PHASE_1_FIELDS),
    Invariant('I2', 'Stored status equals classify(doc) recomputed from the document',
              needs='a stored status field, which nothing writes yet. '
                    'classify() and STATUS_QUERIES agreeing is its precursor, '
                    'not this'),
    Invariant('I3', 'Exactly one is_latest=True document per version_chain_id',
              needs='version_chain_id, is_latest'),
    Invariant('I4', 'version_ordinal is unique and contiguous from 1 within a chain',
              needs='version_chain_id, version_ordinal'),
    Invariant('I5', f'{_POINTERS} are mutual inverses', needs=_POINTERS),
    Invariant('I6', 'Every lineage reference resolves to an existing document',
              check=_check_i6),
    Invariant('I7', 'No document is both LIVE and referenced as another chain\'s '
                    'member', check=_check_i7),
    Invariant('I8', 'payload purged implies TOMBSTONE and no GridFS ids remain',
              check=_check_i8),
    Invariant('I9', 'Chain document source_digest matches its members',
              needs='a derived project_version_chains collection, which does '
                    'not exist'),
    Invariant('I10', 'get_one_project() resolves every LIVE and every SUPERSEDED '
                     'document by _id', check=_check_i10),
    Invariant('I11', 'previous_versions[] matches the lineage derived from pointers',
              needs=_POINTERS),
    Invariant('I12', 'Every GridFS id named by a retained document exists in fs.files',
              check=_check_i12),
    Invariant('I13', 'Every fs.files row naming an existing document is still '
                     'referenced by it',
              needs='metadata on each fs.files row, which nothing writes'),
    Invariant('I14', 'No TOMBSTONE has GridFS ids left, and no fs.files row points '
                     'at one', check=_check_i14,
              partial='the document half only; "no fs.files row points at one" '
                      'needs metadata on each fs.files row, which nothing writes'),
    Invariant('I15', 'A chain is EMPTY iff every member is a TOMBSTONE',
              needs='version_chain_id'),
    Invariant('I16', 'Every chain has exactly one is_latest member',
              needs='version_chain_id, is_latest'),
    Invariant('I17', 'Every chain-level field survives the emptying of a chain',
              needs='a decision about which project-level fields belong to a '
                    'chain and which to a version; six are still unclassified'),
    Invariant('I18', 'Exactly one tombstone-creation routine exists and every '
                     'deletion path calls it', check=_check_i18),
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
                    skip_gridfs=args.skip_gridfs)
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
