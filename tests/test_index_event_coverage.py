"""Every write that can stale the feature index must fire a lifecycle event.

The feature index is a second copy of what ``runs`` holds, and a second copy is
this repository's recurring defect: the copies diverge silently and the
divergence is only found by measuring.  ``rebuild_feature_index --check`` is the
measurement, but it is run by a person, and between two runs a search can return
an answer that disagrees with the project page.

So this reads the source instead.  It finds every write to the projects
collection, keeps the ones that touch a field the index is built from, and
requires that the function doing the write also calls ``project_events``.  The
list it checks against is ``INDEX_SOURCE_PROJECTION`` in ``feature_index.py``
plus the fields ``indexable_projects_query()`` selects on -- read from the
module, not restated here, so widening what the index reads widens this test on
the same commit.

## What this does not prove

It checks that *an* event fires, not that the *right* one does.  Two of the six
writes below were first hooked with ``project_changed()``, which updates
site_statistics as well as the index; on an upload placeholder that counts the
project twice, and on the API upload path it adds counting that path never had.
Neither is visible to this test -- ``test_reaggregation_does_not_double_count_stats``
is what caught the first, and reading every statistics call site caught the
second.  Treat a pass here as "nothing was forgotten", not "everything is right".

Found six unhooked writes when it was written, 2026-09-08.  Only one of them
could return a wrong answer: a metadata sheet rewrites Cancer_type inside
``runs`` without changing whether the project is indexable, so
``index_coverage()`` -- the per-request guard -- cannot see it.  The other five
changed indexability, which the guard does catch: it would have switched the
whole site to the slow path rather than served anything wrong.  That difference
is why this test exists and the guard is not enough on its own.
"""

import ast
import os


CAPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'caper', 'caper')

WRITE_METHODS = {
    'update_one', 'update_many', 'insert_one', 'replace_one',
    'delete_one', 'delete_many', 'find_one_and_update', 'bulk_write',
}

EVENT_NAMES = {
    'project_changed', 'project_removed', 'project_visibility_changed',
    'project_content_changed', 'reindex_project',
}

# The modules that define the index and the events cannot be required to call
# the events; everything else that writes projects can.
EXEMPT_MODULES = {'feature_index.py', 'project_events.py'}


def _indexed_fields():
    """The document fields the index is built from, read from feature_index.py."""
    from caper.feature_index import INDEX_SOURCE_PROJECTION

    # Whether a project is indexable at all is decided by its status, which
    # status_query() expresses over these fields.
    status_fields = {'delete', 'project_status', 'current_version',
                     'previous_versions'}
    return set(INDEX_SOURCE_PROJECTION) | status_fields


def _enclosing_function(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def _project_writes():
    """(module, line, function, op, touched fields, events fired) per write."""
    fields = _indexed_fields()
    found = []
    for filename in sorted(os.listdir(CAPER)):
        if not filename.endswith('.py') or filename in EXEMPT_MODULES:
            continue
        path = os.path.join(CAPER, filename)
        with open(path) as handle:
            source = handle.read()
        if 'collection_handle' not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'collection_handle'
                    and func.attr in WRITE_METHODS):
                continue
            call_text = ast.unparse(node)
            touched = sorted(f for f in fields
                             if f"'{f}" in call_text or f'"{f}' in call_text)
            # An insert or a replace writes the whole document, so it touches
            # every indexed field whether or not it names one.
            whole_document = func.attr in ('insert_one', 'replace_one')
            if not touched and not whole_document:
                continue
            enclosing = _enclosing_function(tree, node.lineno)
            body = ast.unparse(enclosing) if enclosing else ''
            fired = sorted(e for e in EVENT_NAMES if e + '(' in body)
            found.append((filename, node.lineno,
                          enclosing.name if enclosing else '<module>',
                          func.attr, touched or ['<whole document>'], fired))
    return found


def test_the_audit_still_finds_the_writes():
    """Guard the guard: if this stops finding writes, it stops proving anything."""
    writes = _project_writes()
    assert len(writes) >= 10, (
        'the AST walk found almost no writes to the projects collection, which '
        'more likely means the walk broke than that the writes went away'
    )


def test_every_indexed_write_fires_a_lifecycle_event():
    unhooked = [w for w in _project_writes() if not w[-1]]
    assert not unhooked, (
        'These writes change a field the feature index is built from, but the '
        'function making the write never calls project_events, so the index '
        'keeps the old value and a search disagrees with the project page:\n'
        + '\n'.join(
            f'  {mod}:{line} in {func}() -- {op} touching {", ".join(touched)}'
            for mod, line, func, op, touched, _ in unhooked
        )
    )
