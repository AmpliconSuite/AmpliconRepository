"""Lifecycle events reach both consumers, and stay reachable from one place.

``site_statistics`` and the feature index are both derived from the project
documents, and both were once wired in separately -- statistics at thirteen
call sites across three view modules. The defect this guards is not that one of
those calls is wrong today. It is that the fourteenth gets added next to twelve
of them, and the search index quietly stops matching the site for one kind of
event, with nothing failing.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW_MODULES = [
    os.path.join('caper', 'caper', 'views.py'),
    os.path.join('caper', 'caper', 'views_admin.py'),
    os.path.join('caper', 'caper', 'views_apis.py'),
]

# The functions that know how a derived copy is kept in step. Calling one of
# these from a view is the thing that has to go through project_events instead.
SITE_STATS_MUTATORS = (
    'add_project_to_site_statistics',
    'delete_project_from_site_statistics',
    'edit_proj_privacy',
)


def _source(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path)) as handle:
        return handle.read()


def _calls_to(name, text):
    """Lines that call ``name``, ignoring comments and its own import."""
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('from ') or stripped.startswith('import '):
            continue
        if re.search(rf'\b{re.escape(name)}\s*\(', line):
            hits.append((number, stripped))
    return hits


@pytest.mark.parametrize('module', VIEW_MODULES)
@pytest.mark.parametrize('mutator', SITE_STATS_MUTATORS)
def test_views_do_not_update_one_derived_copy_without_the_other(module, mutator):
    """A view calls project_events, never site_stats directly.

    project_events is where "a project changed" is defined, so a view that
    reaches past it updates the counters and leaves the search index behind.
    """
    hits = _calls_to(mutator, _source(module))
    assert hits == [], (
        f"{module} calls {mutator}() directly at {[n for n, _ in hits]}. "
        f"Use caper.project_events instead, so the feature index hears about "
        f"the same event.")


def test_project_events_is_the_only_caller_of_the_site_stats_mutators():
    """...and the exemption is one file, named here rather than assumed."""
    allowed = {
        os.path.join('caper', 'caper', 'project_events.py'),
        os.path.join('caper', 'caper', 'site_stats.py'),
        os.path.join('caper', 'caper', 'views_admin.py'),  # regenerate button only
    }
    offenders = {}
    package = os.path.join(REPO_ROOT, 'caper', 'caper')
    for entry in sorted(os.listdir(package)):
        if not entry.endswith('.py'):
            continue
        relative = os.path.join('caper', 'caper', entry)
        if relative in allowed:
            continue
        for mutator in SITE_STATS_MUTATORS:
            hits = _calls_to(mutator, _source(relative))
            if hits:
                offenders.setdefault(relative, []).extend(n for n, _ in hits)
    assert offenders == {}, (
        f"these modules bypass project_events: {offenders}")


def test_every_lifecycle_helper_reaches_the_index():
    """Each exported event does something to the index.

    Written against the source rather than by calling them, because calling
    them means a database and a real project; what this needs to know is only
    that no helper was added that updates the statistics and stops there.
    """
    text = _source(os.path.join('caper', 'caper', 'project_events.py'))
    helpers = re.findall(r'^def (project_\w+)\(', text, re.M)
    assert set(helpers) == {
        'project_changed', 'project_removed',
        'project_visibility_changed', 'project_content_changed',
    }
    for helper in helpers:
        body = text.split(f'def {helper}(')[1].split('\ndef ')[0]
        assert 'reindex_project' in body, (
            f"{helper}() updates the statistics but never the feature index")


def test_reindex_failures_are_swallowed_not_raised():
    """An index write must never fail a user's upload.

    The drift check is what turns a failure here into something a person sees.
    Raising instead would trade a search that is briefly behind for an upload
    that does not complete.
    """
    from caper import project_events

    assert project_events.reindex_project(None) is None
    assert project_events.reindex_project('not-an-object-id') is None


def test_reindex_asks_the_database_rather_than_the_caller():
    """The primitive takes an id, not a document.

    That is what makes one function right for creation, promotion, demotion,
    soft delete and restore: after any of them the correct action is 'index it
    if it is indexable, drop it if it is not', and only the database knows
    which. Handing it a document would let a caller index a version that was
    just deleted, or miss the predecessor that was just promoted.
    """
    import inspect

    from caper import project_events

    signature = inspect.signature(project_events.reindex_project)
    assert list(signature.parameters) == ['project_id']
