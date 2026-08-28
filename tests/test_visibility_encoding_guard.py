"""
The grep guard: nothing reads the 'private' field raw.

``private`` is not a boolean.  It holds one of three visibility strings, and a
handful of documents written before that change hold a boolean instead.  Both
facts are easy to forget, because the field is *named* like a boolean, and both
failure modes are silent:

  * ``if project['private']`` is true for the string ``'public'``.  This is not
    hypothetical -- it shipped in ``views.py`` as
    ``is_public = not project.get('private', True)``, which evaluated False for
    every project on the site, and in ``views_admin.py`` as
    ``is_private = project.get('private', True)``, which badged every project in
    the admin file report "Private".
  * ``{'private': False}`` matches nothing.  No error, no warning, an empty
    result set that looks like a site with no public projects.

The guard is the same shape as ``test_project_status_guard.py`` and for the same
reason: it asks a question with no judgement in it -- does the field get read
without going through a helper -- and every answer of "yes, and that is fine" is
written down below with its reason.

Scope: application code (``caper/caper/``), the operational scripts at the repo
root, and the templates.  ``tests/`` is exempt; fixture documents have to be
free to spell out the legacy encoding, which is exactly what the fixtures in
``test_admin_delete_user.py`` and ``test_account_deletion_disposition.py`` exist
to do.
"""

import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLICATION_PACKAGE = os.path.join('caper', 'caper')
TEMPLATE_ROOT = os.path.join('caper', 'templates')

# The one module allowed to know how the field is encoded.
ENCODER = os.path.join('caper', 'caper', 'visibility.py')

# The five helpers that know how the field is encoded. A line that reads the
# field and calls one of these has asked the question properly.
HELPERS = (
    'normalize_visibility_field',
    'is_project_private',
    'is_project_public',
    'is_project_hidden_public',
    'format_visibility_for_display',
)

# Two spellings, because both appear in this codebase:
#   project['private']        -- subscript, in a read or an assignment
#   project.get('private')    -- the defaulted read, which is the common one
FIELD_READ = re.compile(r"""\[\s*['"]private['"]\s*\]|\.get\(\s*['"]private['"]""")

# A query literal that pins the field to one encoding. `{'private': 'public'}`
# is wrong in the other direction -- it misses the legacy booleans -- so the
# guard wants the constants, not a hand-written list either way.
QUERY_LITERAL = re.compile(
    r"""['"]private['"]\s*:\s*(?:True|False|['"](?:private|public|hidden_public)['"])""")

# Every read that is allowed to be raw, and why. Keyed by (relative path, the
# logical line's text) so that moving a line does not silently re-approve a
# different one.
ALLOWED = {
    (os.path.join('caper', 'caper', 'forms.py'),
     "self.fields['private'].required = False"):
        "Django form field named 'private', not a document read.",

    (os.path.join('caper', 'caper', 'schema_validate.py'),
     'visibility_schema = schema.get("properties", {}).get("private", {})'):
        "Reads the JSON schema, not a project document.",

    (os.path.join('caper', 'caper', 'schema_validate.py'),
     'current_value = document["private"]'):
        "_normalize_legacy_visibility is the fixer; it has to see the raw value.",

    (os.path.join('caper', 'caper', 'schema_validate.py'),
     'document["private"] = canonical_value'):
        "_normalize_legacy_visibility writing the value it just canonicalised.",

    (os.path.join('caper', 'caper', 'schema_validate.py'),
     '_record_change( changes_log, ["private"], "normalized_legacy_visibility", '
     'canonical_value, )'):
        "A field path in a change record, not a read.",

    ('backfill_project_status.py',
     "stored = doc.get('private')"):
        "plan_visibility is the pass that rewrites the legacy booleans; "
        "seeing the raw value is the whole point of it.",
}

# Whole files exempt, with the reason. These are the scripts whose subject *is*
# the raw encoding: normalising it, or reporting what it currently holds.
EXEMPT_FILES = {
    'migrate_project_visibility.py':
        "The boolean-to-string migration. Reading the raw value is its job.",
    'check_project_flags.py':
        "A flag report. It prints what the field holds, including 'NOT SET'.",
    'check_project_flags_django.py':
        "Django twin of check_project_flags.py; same reason.",
}


def _logical_lines(text):
    """Yield (line_number, joined_text) with bracket continuations joined.

    A read split across two lines by black is still one expression, and the
    helper call that makes it correct may sit on either half.
    """
    lines = []
    buffer = ''
    start = 0
    depth = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not buffer:
            start = number
        buffer += ' ' + line.strip()
        depth += line.count('(') + line.count('[') + line.count('{')
        depth -= line.count(')') + line.count(']') + line.count('}')
        if depth <= 0:
            lines.append((start, buffer.strip()))
            buffer = ''
            depth = 0
    if buffer:
        lines.append((start, buffer.strip()))
    return lines


def _python_sources():
    for directory, _dirs, filenames in os.walk(os.path.join(REPO_ROOT, APPLICATION_PACKAGE)):
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            relative = os.path.relpath(os.path.join(directory, filename), REPO_ROOT)
            if relative != ENCODER:
                yield relative
    for filename in sorted(os.listdir(REPO_ROOT)):
        if filename.endswith('.py') and filename not in EXEMPT_FILES:
            yield filename


def _offending_lines(pattern, require_helper):
    found = []
    for path in _python_sources():
        if os.path.basename(path) in EXEMPT_FILES:
            continue
        with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as handle:
            for number, text in _logical_lines(handle.read()):
                if text.startswith('#'):
                    continue
                if not pattern.search(text):
                    continue
                if require_helper and any(helper in text for helper in HELPERS):
                    continue
                found.append((path, number, text))
    return found


def test_no_raw_read_of_the_visibility_field():
    """Every read of 'private' goes through a helper, or is listed above."""
    offenders = [
        (path, number, text)
        for path, number, text in _offending_lines(FIELD_READ, require_helper=True)
        if (path, text) not in ALLOWED
    ]
    assert not offenders, (
        "The 'private' field holds a visibility string, not a boolean. These "
        "reads bypass the helpers in utils.py:\n"
        + "\n".join(f"  {p}:{n}: {t}" for p, n, t in offenders)
        + "\n\nUse normalize_visibility_field() and one of the is_project_* "
          "predicates, or add the line to ALLOWED with a reason."
    )


def test_no_query_pins_the_field_to_one_encoding():
    """No `{'private': True}` or `{'private': 'public'}` filter literal.

    Both encodings are live, so a *filter* has to match both. The constants in
    utils.py hold the value lists; a query that spells one out by hand is one
    more copy waiting to fall behind.

    A document being written holds exactly one value and is not the hazard, so
    the test looks only at logical lines that also make a query call. That is
    the one piece of structure separating the two, and it is checked rather
    than inferred. The gap it leaves -- a filter assembled into a variable on
    one line and passed to find() on another -- is real; ``status_query`` and
    ``combine`` are in the list below because they are how this codebase builds
    filters, which closes most of it.
    """
    query_call = re.compile(
        r'\b(?:find|find_one|find_one_and_update|count_documents|update_one|'
        r'update_many|delete_one|delete_many|aggregate|status_query|combine)\s*\(')
    offenders = [
        (path, number, text)
        for path, number, text in _offending_lines(QUERY_LITERAL, require_helper=False)
        if query_call.search(text) and '$in' not in text and '$set' not in text
        and not re.search(r"""['"]private['"]\s*:\s*normalize_visibility_field""", text)
    ]
    assert not offenders, (
        "A query literal pinned to one encoding matches only half the "
        "documents, silently:\n"
        + "\n".join(f"  {p}:{n}: {t}" for p, n, t in offenders)
        + "\n\nUse PUBLIC_QUERY_VALUES / RESTRICTED_QUERY_VALUES from utils.py."
    )


def test_no_template_reads_the_field_directly():
    """Django templates evaluate 'public' as truthy, same as Python.

    ``{% if project.private %}`` is true for a public project. Templates get a
    resolved ``visibility_display`` from the view instead.
    """
    pattern = re.compile(r"""\.private(?![_a-zA-Z-])""")
    offenders = []
    for directory, _dirs, filenames in os.walk(os.path.join(REPO_ROOT, TEMPLATE_ROOT)):
        for filename in sorted(filenames):
            if not filename.endswith('.html'):
                continue
            path = os.path.join(directory, filename)
            with open(path, encoding='utf-8') as handle:
                for number, line in enumerate(handle, 1):
                    if pattern.search(line):
                        offenders.append(
                            (os.path.relpath(path, REPO_ROOT), number, line.strip()))
    assert not offenders, (
        "Templates must not read the raw visibility field:\n"
        + "\n".join(f"  {p}:{n}: {t}" for p, n, t in offenders)
        + "\n\nResolve it in the view and pass visibility_display."
    )


def test_every_allowance_matches_exactly_one_line():
    """An allowance that matches nothing is a stale approval."""
    counts = {}
    for path in _python_sources():
        if os.path.basename(path) in EXEMPT_FILES:
            continue
        with open(os.path.join(REPO_ROOT, path), encoding='utf-8') as handle:
            for _number, text in _logical_lines(handle.read()):
                key = (path, text)
                if key in ALLOWED:
                    counts[key] = counts.get(key, 0) + 1
    wrong = {key: counts.get(key, 0) for key in ALLOWED if counts.get(key, 0) != 1}
    assert not wrong, (
        "These allowances no longer match exactly one line -- the code moved on "
        "without them:\n"
        + "\n".join(f"  {path}: {text!r} matched {n} lines"
                    for (path, text), n in wrong.items())
    )


def test_the_encoder_module_has_no_django_import():
    """visibility.py exists to be importable without Django.

    That is the only reason it is a separate module from utils.py: the
    standalone operational scripts have no Django, and before the split their
    only options were to import a module that pulls in allauth or to re-derive
    the mapping. A Django import here quietly removes the first option again.
    """
    with open(os.path.join(REPO_ROOT, ENCODER), encoding='utf-8') as handle:
        source = handle.read()
    offenders = [line.strip() for line in source.splitlines()
                 if re.match(r'\s*(?:from|import)\s+(?:django|allauth)\b', line)]
    assert not offenders, (
        f"{ENCODER} must import no Django: {offenders}")


def test_every_exempt_file_still_exists():
    missing = [name for name in EXEMPT_FILES
               if not os.path.exists(os.path.join(REPO_ROOT, name))]
    assert not missing, f"Exempt files that no longer exist: {missing}"


def test_the_helpers_agree_with_the_schema():
    """utils.VISIBILITY_VALUES and schema.json's enum are the same set.

    Two places name the legal visibilities: the normalizer that decides what to
    accept, and the schema the QC report validates against. When they diverge,
    the normalizer passes a value the validator then flags, or the reverse.
    """
    from caper.utils import VISIBILITY_VALUES

    schema_path = os.path.join(REPO_ROOT, 'caper', 'schema', 'schema.json')
    with open(schema_path, encoding='utf-8') as handle:
        schema = json.load(handle)
    enum = schema['properties']['private']['enum']
    assert set(enum) == set(VISIBILITY_VALUES), (
        f"schema.json enum {sorted(enum)} != utils.VISIBILITY_VALUES "
        f"{sorted(VISIBILITY_VALUES)}"
    )


def test_the_query_value_lists_cover_every_legal_value():
    """The three query lists partition the strings and both booleans.

    A value missing from all three is a document no query finds. A value in two
    of them is a document counted twice by the statistics buckets.
    """
    from caper.utils import VISIBILITY_QUERY_VALUES, VISIBILITY_VALUES

    seen = []
    for values in VISIBILITY_QUERY_VALUES.values():
        seen.extend(values)

    duplicates = [v for v in seen if seen.count(v) > 1]
    assert not duplicates, f"Value listed in more than one bucket: {duplicates}"

    missing = [v for v in VISIBILITY_VALUES if v not in seen]
    assert not missing, f"Visibility with no query bucket: {missing}"

    for legacy in (True, False):
        assert legacy in seen, f"Legacy boolean {legacy} matched by no bucket"


def test_restricted_is_private_plus_hidden_public():
    """The access-control list is the two non-public buckets, derived not typed."""
    from caper.utils import (
        PUBLIC_QUERY_VALUES, RESTRICTED_QUERY_VALUES, VISIBILITY_QUERY_VALUES,
    )

    assert PUBLIC_QUERY_VALUES == VISIBILITY_QUERY_VALUES['public']
    assert set(RESTRICTED_QUERY_VALUES) == (
        set(VISIBILITY_QUERY_VALUES['private'])
        | set(VISIBILITY_QUERY_VALUES['hidden_public'])
    )
    # The two must not overlap, or a project appears in both listings.
    assert not set(PUBLIC_QUERY_VALUES) & set(RESTRICTED_QUERY_VALUES)


@pytest.mark.parametrize('value,expected', [
    (True, 'private'),
    (False, 'public'),
    ('private', 'private'),
    ('public', 'public'),
    ('hidden_public', 'hidden_public'),
])
def test_normalizer_maps_both_encodings(value, expected):
    from caper.utils import normalize_visibility_field
    assert normalize_visibility_field(value) == expected


def test_the_truthiness_trap_is_what_the_guard_is_for():
    """Documents the bug in an assertion, so the reason survives the fix.

    Both encodings of a *public* project are truthy in Python. That is the whole
    hazard, and it is why the guard bans the raw read rather than trusting
    review to notice.
    """
    assert bool('public') is True
    assert bool('hidden_public') is True
    assert bool('private') is True
    # ...so `not project['private']` is False for a public project, and
    # `if project['private']` is True for one.
