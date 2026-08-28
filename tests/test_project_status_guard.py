"""
The grep guard: no 'delete' / 'current' literal outside project_status.py.

Zero query literals containing 'delete' or 'current' outside
project_status.py.  It is blunt on purpose.  A guard that reasoned about
which literals are filters and which are payloads would have to read the code
the way a person does, and every case it got wrong would be an argument for
switching it off.  This one asks a question with no judgement in it -- does the
token appear at all -- and every answer of "yes, and that is fine" is written
down below with its reason.

It earns its keep: writing it turned up two soft-delete writes in views.py
spelled ``{'delete' : True}``, with a space before the colon, which the audit's
own grep for ``'delete':`` had walked straight past.  That exact spacing
divergence is one of the 21 distinct query shapes the audit counted.

Scope: application code (``caper/caper/``) and the operational scripts at the
repo root.  ``tests/`` is exempt -- fixture documents have to be free to spell
out the awkward states the fixture catalogue demands, and a guard with two
hundred exemptions is a guard nobody keeps.  The protection for tests is
different in kind: ``FakeCollection`` and the truth-table fixtures evaluate
through ``project_status.matches()``, so a test can no longer encode a
*different* belief about reachability, which is what
``tests/test_purge_local_db.py`` once did.
"""

import importlib
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The one module allowed to know.
RESOLVER = os.path.join('caper', 'caper', 'project_status.py')

APPLICATION_PACKAGE = os.path.join('caper', 'caper')

# Four spellings, because the codebase uses all four:
#   {'delete': False}          -- a dict key, in a filter, a $set or a document
#   project['delete'] = False  -- item assignment while building a document
#   doc.get('delete', ...)     -- reading the flag back out
#   doc['delete'] == False     -- comparing it
#
# The first two were what the original guard caught, and finding them was
# already worth the file.  The last two were the hole: a predicate spelled
# `delete_val == False and current_val == False` contains no quoted flag
# adjacent to a colon or an assignment, so the earlier pattern walked past
# check_project_flags.py's orphan finder, its Django twin, views_apis.py's
# "is this the live one" test, and schema_validate.py's skip-deleted test --
# four more copies of the predicate, one of them in a live API path.
#
# That is the whole lesson of this codebase in miniature: the guard was written
# to catch the spelling the last bug happened to use.  Reading the flag is as
# much a re-derivation as querying it, so the pattern now covers reads too, and
# the display-only reads are listed as allowances below.
FLAG_LITERAL = re.compile(
    r"""['"](?:delete|current)['"]\s*(?::|\]\s*(?:=(?!=)|==|!=|\sis\s)|[,)])""")


# Widening the pattern to cover reads turned up 36 lines, none of them
# predicates: index field-name tuples, a form's `fields`, argparse choices,
# display reads defaulting to 'NOT SET'.  Listing all 36 individually would
# have made the allowance table four times the size of the guard, and a table
# that big stops being read -- which is how the previous hole survived.
#
# So benign *shapes* are named once here, with the property that makes each
# safe, rather than line by line.  The test below asserts every one of these
# still matches something, so a shape that stops occurring gets deleted rather
# than sitting here widening the guard for nothing.
BENIGN_READS = [
    (re.compile(r'^\s*#'),
     "a comment; prose about the flags is not a use of them"),
    (re.compile(r"""\.get\(['"](?:delete|current)['"],\s*['"]NOT SET['"]\)"""),
     "read with a string sentinel default -- formats a flag for display, and a "
     "sentinel that is neither True nor False cannot be mistaken for a decision"),
    (re.compile(r"""check_flag_value\([^)]*['"](?:delete|current)['"]\)"""),
     "the display helper in the flag-report scripts; it returns a label, not a "
     "verdict"),
    (re.compile(r"""['"](?:delete|current)['"]\s*[,)]"""
                r"""(?![^#]*(?:==|!=|\bis\b|\bnot\b))"""),
     "the flag named as a field, not tested: an index spec, a form's field "
     "list, a projection, a key tuple, an argparse choice.  The lookahead is "
     "what keeps this narrow -- a line that names the flag and then compares "
     "anything on the same line is not covered here and needs its own reason"),
]


def _benign_reason(text):
    """The first benign shape matching this line, or None."""
    for pattern, reason in BENIGN_READS:
        if pattern.search(text):
            return reason
    return None


# Every remaining occurrence, keyed by file and by the text of the line, with
# why it is not a predicate.  Keyed on text rather than line number because
# line numbers move for unrelated reasons and a guard that cries wolf gets
# deleted; the text changing is exactly when a human should look again.
#
# Adding an entry is a deliberate act.  If the reason you would write is "it is
# a query, but a special one", the answer is to name that predicate in
# project_status.py instead.
ALLOWED = {
    # -- prose, not code -------------------------------------------------
    (os.path.join('caper', 'caper', 'apps.py'),
     "# Query pattern: {'delete': False, 'current': True, 'private': 'public'}"):
        "comment recording the query shape an index serves",
    (os.path.join('caper', 'caper', 'apps.py'),
     "# Query pattern: {'delete': False, 'current': True, 'private': "
     "{'$in': ['private', 'hidden_public']}, 'project_members': <user>}"):
        "comment recording the query shape an index serves",
    ('cleanup_orphaned_projects.py',
     "because {'current': False} does not match a missing field.  One routine"):
        "module docstring explaining the field-absence trap",
    ('cleanup_orphaned_projects.py',
     "`{'current': False}` does not match a document with no 'current' field, so"):
        "docstring explaining the field-absence trap",
    ('cleanup_orphaned_projects.py',
     "\"{'current': False}\")"):
        "log line quoting the query it is explaining to the operator",

    # -- output formatting, not queries ----------------------------------
    ('check_project_flags.py',
     "print(f\"{'Project Name':<50} {'ID':<26} {'current':<10} {'delete':<10}\")"):
        "f-string column header",
    ('check_project_flags.py',
     "print(\"      {'$set': {'current': True}}\")"):
        "printed remediation advice",
    ('check_project_flags_django.py',
     "print(f\"{'Project Name':<50} {'ID':<26} {'current':<10} {'delete':<10}\")"):
        "f-string column header",
    ('check_project_flags_django.py',
     "print(\"      {'$set': {'current': True}}\")"):
        "printed remediation advice",

    # -- projections and control flow, not filters -----------------------
    ('purge-local-db.py',
     "if scope == 'current':"):
        "string comparison on a --reference-scope argument named 'current'",
    ('purge-local-db.py',
     "choices=['reachable', 'active', 'current', 'not-deleted', 'all'],"):
        "argparse choices for --reference-scope.  Named here rather than left "
        "to the field-name shape because the neighbouring choice 'not-deleted' "
        "contains the word 'not', which trips that shape's comparison lookahead",
    ('purge-local-db.py',
     "for project in projects.find({}, {'_id': 1, 'project_name': 1, "
     "'current': 1, 'delete': 1, 'tarfile': 1}):"):
        "projection listing fields to fetch, not a filter",
    (os.path.join('caper', 'caper', 'views_admin.py'),
     "if action == 'delete':"):
        "string comparison on a form action named 'delete'",
    ('backfill_project_status.py',
     "{'project_name': 1, 'delete': 1, 'current': 1,"):
        "projection listing fields to fetch, not a filter.  The filter itself "
        "is MISSING_CURRENT_QUERY, named in project_status.py",

    # -- writing the flag that was never there ---------------------------
    # The backfill exists to give the documents with no 'current' field the
    # one they should have had, so it necessarily names that key.  It does not
    # decide the value: that comes from status_flags(target), and which target
    # applies is decided by classify() and the lineage reader.
    ('backfill_project_status.py',
     "{'$set': {'current': value}})"):
        "the backfill's write; the value comes from status_flags(target)",
    ('backfill_project_status.py',
     "record(rollback, doc['_id'], '$unset', {'current': ''})"):
        "the undo record for that write -- removes the field again, restoring "
        "the absence, so it encodes no belief about what the flag should be",

    # -- deliberate half-writes of the flag pair -------------------------
    # These set one flag and leave the other alone, which is what moves a
    # document between statuses one step at a time.  The value still comes from
    # status_flags(), so there is no second copy of it; only the key is spelled
    # out, because writing half a pair is the point and no status names that.
    (os.path.join('caper', 'caper', 'views_admin.py'),
     "new_val = {\"$set\": {'delete': status_flags(LIVE)['delete'],"):
        "admin un-delete clears 'delete' only; the resulting status comes from "
        "status_after(), which reads the 'current' already stored",
    (os.path.join('caper', 'caper', 'views_admin.py'),
     "{'$set': {'current': status_flags(LIVE)['current'],"):
        "admin repair sets 'current' only; status from status_after()",
    (os.path.join('caper', 'caper', 'views.py'),
     "new_val = { \"$set\": {'delete': status_flags(SOFT_DELETED)['delete'],"):
        "project_delete() sets 'delete' only: LIVE -> SOFT_DELETED",
    (os.path.join('caper', 'caper', 'views.py'),
     "new_val = { \"$set\": {'current': status_flags(SUPERSEDED)['current'],"):
        "project_update() clears 'current' only: SOFT_DELETED -> SUPERSEDED",

    # -- the one write with no status to name -----------------------------
    (os.path.join('caper', 'caper', 'views.py'),
     "'current': False,"):
        "_do_rollback() marks a failed placeholder current=False while leaving "
        "delete=False, producing a DETACHED document that is still reachable "
        "by URL. That is the defect itself, not a routing miss -- see the "
        "comment above the line, where the population is 39 "
        "documents. status_flags() refuses to write DETACHED on purpose.",
}


def _scanned_files():
    """Application package (recursively) plus the root-level scripts."""
    package = os.path.join(REPO_ROOT, APPLICATION_PACKAGE)
    for dirpath, dirnames, filenames in os.walk(package):
        dirnames[:] = [d for d in dirnames if d not in {'__pycache__', 'migrations'}]
        for name in sorted(filenames):
            if name.endswith('.py'):
                yield os.path.relpath(os.path.join(dirpath, name), REPO_ROOT)

    for name in sorted(os.listdir(REPO_ROOT)):
        if name.endswith('.py'):
            yield name


def _occurrences():
    """(relative path, line number, stripped text) for every flag literal."""
    for relpath in _scanned_files():
        if relpath == RESOLVER:
            continue
        with open(os.path.join(REPO_ROOT, relpath), encoding='utf-8') as handle:
            for number, line in enumerate(handle, start=1):
                if FLAG_LITERAL.search(line):
                    yield relpath, number, line.strip()


def test_no_unlisted_delete_or_current_literal():
    """Zero query literals containing 'delete' or 'current' outside
    project_status.py.

    A failure here is not a style complaint.  It means a predicate this
    codebase has already got wrong twice has been written down a second time,
    somewhere that can drift from the resolver.  Name it in project_status.py.
    """
    unlisted = [(path, number, text)
                for path, number, text in _occurrences()
                if (path, text) not in ALLOWED and _benign_reason(text) is None]

    assert not unlisted, "unlisted 'delete'/'current' literals:\n" + "\n".join(
        f"  {path}:{number}: {text}" for path, number, text in unlisted)


def test_every_allowance_matches_exactly_one_line():
    """A stale or ambiguous exemption is a hole.

    Matching zero lines means the exemption is dead and something has moved
    without being re-read.  Matching two means a second occurrence has slipped
    in under cover of the first -- the guard would pass while a genuinely new
    literal went unreviewed.
    """
    counts = {}
    for path, _number, text in _occurrences():
        counts[(path, text)] = counts.get((path, text), 0) + 1

    wrong = {key: counts.get(key, 0) for key in ALLOWED if counts.get(key, 0) != 1}

    assert not wrong, (
        "these allowances do not match exactly one line; re-check them: "
        f"{ {f'{p}: {t}': n for (p, t), n in wrong.items()} }")


def test_every_benign_shape_still_matches_something():
    """A benign shape that matches nothing is a hole with no purpose.

    Each entry in BENIGN_READS widens the guard.  That is only worth it while
    the shape actually occurs; once it stops, the entry should go, not sit here
    quietly excusing whatever grows into it later.
    """
    lines = [text for _path, _number, text in _occurrences()]
    dead = [reason for pattern, reason in BENIGN_READS
            if not any(pattern.search(text) for text in lines)]

    assert not dead, (
        "these benign shapes no longer match any line and should be deleted "
        f"rather than left widening the guard: {dead}")


def test_a_predicate_spelled_as_a_comparison_is_caught():
    """The regression that motivated widening the pattern.

    ``delete_val == False and current_val == False`` -- the DETACHED predicate
    as check_project_flags.py spelled it -- contains no quoted flag next to a
    colon, so the original pattern walked past it.  What it does contain is the
    read that feeds it.  This pins the read forms as caught and, just as
    importantly, pins them as *not* benign: shape 4's lookahead must decline a
    line that goes on to compare something.
    """
    predicate_reads = [
        "        delete_val = project.get('delete', None)",
        "        current_val = project.get('current', None)",
        "        if project.get('delete') == False:",
        "        if doc['current'] is False:",
    ]
    for line in predicate_reads:
        assert FLAG_LITERAL.search(line), f"pattern no longer catches: {line}"

    assert _benign_reason("        if project.get('delete') == False:") is None
    assert _benign_reason("        if doc['current'] is False:") is None


def test_the_resolver_is_where_the_predicate_lives():
    """Sanity: the one exempt file is actually the one holding the definitions."""
    with open(os.path.join(REPO_ROOT, RESOLVER), encoding='utf-8') as handle:
        source = handle.read()

    assert FLAG_LITERAL.search(source)
    assert 'STATUS_QUERIES' in source
    assert 'def classify(' in source
    assert 'def is_reachable_by_url(' in source


@pytest.mark.parametrize('module', [
    'caper.utils',
    'caper.views',
    'caper.views_admin',
    'caper.views_apis',
    'caper.search',
    'caper.site_stats',
    'caper.account_deletion',
    'caper.account_signals',
    'caper.project_version_cleanup',
    'caper.schema_validate',
])
def test_status_consumers_import_the_resolver(module):
    """Every module that used to spell the predicate out now imports it.

    Without this, a rewrite that removed the literals by removing the behaviour
    would pass the guard above while collapsing nothing.
    """
    path = importlib.import_module(module).__file__
    with open(path, encoding='utf-8') as handle:
        source = handle.read()

    assert 'project_status import' in source, (
        f"{module} no longer imports from project_status")


@pytest.mark.parametrize('script', [
    'cleanup_orphaned_projects.py',
    'purge-local-db.py',
    'restore_sample_csv_metadata.py',
    'migrate_project_visibility.py',
    'check_project_flags.py',
    'check_project_flags_django.py',
])
def test_operational_scripts_import_the_resolver(script):
    """The scripts are where both incidents happened.  They read the predicate
    from the application rather than re-deriving it."""
    with open(os.path.join(REPO_ROOT, script), encoding='utf-8') as handle:
        source = handle.read()

    assert 'project_status import' in source, (
        f"{script} no longer imports from project_status")
