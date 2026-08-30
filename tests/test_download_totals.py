"""Download counts belong to the project, not to the version that served them.

The bug these guard against has two halves, and they fail differently.

The read half: a project's dated download history is spread across its
versions, because reaggregation starts the new version's tally empty. Showing
only the current version's share hid 90% of it on prod (measured 2026-08-29:
54,888 of 529,432 sample downloads).

The write half: a tombstone is written with replace_one, so a counter left off
the tombstone is destroyed, not hidden. That one is not recoverable afterwards,
which is why it is tested at the level of the routine that builds tombstones
rather than through a view.
"""
import pytest

from caper.download_totals import (
    DATED_COUNTERS, as_dated, chain_totals, chain_totals_for, merge_dated,
    total,
)


class FakeCollection:
    """Just enough of a collection to serve ``chain_members``."""

    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        chain = query.get('version_chain_id')
        if isinstance(chain, dict) and '$in' in chain:
            wanted = set(chain['$in'])
            return [d for d in self.docs if d.get('version_chain_id') in wanted]
        return [d for d in self.docs if d.get('version_chain_id') == chain]


def _version(oid, chain, ordinal, project=None, sample=None):
    doc = {'_id': oid, 'version_chain_id': chain, 'version_ordinal': ordinal}
    if project is not None:
        doc['project_downloads'] = project
    if sample is not None:
        doc['sample_downloads'] = sample
    return doc


# --- encodings ------------------------------------------------------------

def test_the_three_encodings_production_actually_holds():
    assert as_dated({'2026-08-01': 3, '2026-08-02': 1}) == {'2026-08-01': 3,
                                                            '2026-08-02': 1}
    assert as_dated(None) == {}
    assert as_dated({}) == {}
    # A bare int predates the per-date breakdown and has no date to claim.
    assert as_dated(7) == {None: 7}
    assert total(7) == 7
    assert as_dated(0) == {}


def test_a_boolean_is_not_a_count_of_one():
    """``bool`` is an ``int`` subclass; a True here is corruption."""
    assert as_dated(True) == {}
    assert as_dated(False) == {}


def test_non_numeric_values_inside_a_dict_are_dropped_not_summed():
    assert as_dated({'2026-08-01': 3, '2026-08-02': 'x'}) == {'2026-08-01': 3}


def test_merging_adds_the_same_date_from_two_versions():
    """A version superseded at noon and its successor both serve that day."""
    merged = merge_dated([{'2026-08-01': 2}, {'2026-08-01': 3, '2026-08-02': 1}])

    assert merged == {'2026-08-01': 5, '2026-08-02': 1}


# --- the read half --------------------------------------------------------

def test_a_chain_totals_every_version_not_just_the_head():
    chain = 'chain-1'
    docs = [
        _version('v1', chain, 1, project={'2026-01-01': 100}, sample={'2026-01-01': 900}),
        _version('v2', chain, 2, project={'2026-02-01': 20}, sample={'2026-02-01': 80}),
        _version('v3', chain, 3, project={'2026-03-01': 7}, sample={'2026-03-01': 11}),
    ]
    totals = chain_totals(FakeCollection(docs), docs[-1])

    assert sum(totals['project_downloads'].values()) == 127
    assert sum(totals['sample_downloads'].values()) == 991


def test_a_deleted_version_still_counts_towards_the_project():
    """Tidying an old version away must not shrink the project's history."""
    chain = 'chain-2'
    tombstone = _version('v1', chain, 1, project={'2026-01-01': 40})
    tombstone['status'] = 'TOMBSTONE'
    docs = [tombstone, _version('v2', chain, 2, project={'2026-02-01': 2})]

    totals = chain_totals(FakeCollection(docs), docs[-1])

    assert sum(totals['project_downloads'].values()) == 42


def test_a_project_with_no_chain_keeps_its_own_counts():
    doc = {'_id': 'solo', 'project_downloads': {'2026-01-01': 5}}

    totals = chain_totals(FakeCollection([]), doc)

    assert totals['project_downloads'] == {'2026-01-01': 5}
    assert totals['sample_downloads'] == {}


def test_a_chain_id_the_collection_does_not_have_falls_back_to_the_document():
    doc = _version('lonely', 'missing-chain', 1, project={'2026-01-01': 3})

    totals = chain_totals(FakeCollection([]), doc)

    assert totals['project_downloads'] == {'2026-01-01': 3}


def test_the_batch_form_agrees_with_the_per_project_form():
    """The admin page uses the batch form; it must not answer differently."""
    docs = [
        _version('a1', 'A', 1, project={'2026-01-01': 5}),
        _version('a2', 'A', 2, project={'2026-02-01': 6}),
        _version('b1', 'B', 1, sample={'2026-01-01': 9}),
        {'_id': 'solo', 'project_downloads': {'2026-01-01': 1}},
    ]
    collection = FakeCollection(docs)
    heads = [docs[1], docs[2], docs[3]]

    batch = chain_totals_for(collection, heads)

    for head in heads:
        assert batch[str(head['_id'])] == chain_totals(collection, head), (
            f'batch and per-project disagree for {head["_id"]}')


def test_the_batch_form_does_not_query_once_per_project():
    """One query for every chain, not one for every row on the page."""
    docs = [_version(f'v{i}', 'A', i, project={f'2026-01-{i:02d}': 1})
            for i in range(1, 6)]

    class CountingCollection(FakeCollection):
        queries = 0

        def find(self, query, projection=None):
            CountingCollection.queries += 1
            return super().find(query, projection)

    chain_totals_for(CountingCollection(docs), docs)

    assert CountingCollection.queries == 1


# --- the write half -------------------------------------------------------

def test_a_tombstone_keeps_the_downloads_its_version_served():
    from caper.project_version_cleanup import build_deleted_version_tombstone

    old = {
        '_id': 'old', 'project_name': 'P', 'private': 'private',
        'project_downloads': {'2026-01-01': 40},
        'sample_downloads': {'2026-01-01': 900},
        'views': 12, 'downloads': 5,
    }
    latest = {'_id': 'new', 'project_name': 'P', 'private': 'private'}

    tombstone = build_deleted_version_tombstone(old, latest, 'someone', 'today')

    assert tombstone['project_downloads'] == {'2026-01-01': 40}
    assert tombstone['sample_downloads'] == {'2026-01-01': 900}


def test_a_tombstone_does_not_keep_the_cumulative_counters():
    """``views`` and ``downloads`` are copied onto the promoted version.

    Keeping them here as well would count the same download twice for any
    reader that sums the chain, which is exactly what this module does for the
    dated pair. The asymmetry is deliberate and is the reason the two pairs are
    not handled by one rule.
    """
    from caper.project_version_cleanup import build_deleted_version_tombstone

    old = {'_id': 'old', 'project_name': 'P', 'private': 'private',
           'views': 12, 'downloads': 5}
    latest = {'_id': 'new', 'project_name': 'P', 'private': 'private'}

    tombstone = build_deleted_version_tombstone(old, latest, 'someone', 'today')

    assert 'views' not in tombstone
    assert 'downloads' not in tombstone


def test_a_tombstone_for_a_version_that_earned_nothing_stays_lean():
    from caper.project_version_cleanup import build_deleted_version_tombstone

    old = {'_id': 'old', 'project_name': 'P', 'private': 'private'}
    latest = {'_id': 'new', 'project_name': 'P', 'private': 'private'}

    tombstone = build_deleted_version_tombstone(old, latest, 'someone', 'today')

    for field in DATED_COUNTERS:
        assert field not in tombstone


def test_the_terminal_deletion_keeps_them_too():
    """T7/T8: no surviving version to inherit from, and still a real history."""
    from caper.project_version_cleanup import build_deleted_version_tombstone

    old = {'_id': 'only', 'project_name': 'P', 'private': 'public',
           'sample_downloads': {'2026-01-01': 3}}

    tombstone = build_deleted_version_tombstone(old, None, 'someone', 'today')

    assert tombstone['sample_downloads'] == {'2026-01-01': 3}
    assert 'redirect_to_project' not in tombstone


# --- the classification this all has to agree with ------------------------

def test_the_dated_counters_are_declared_chain_level():
    """They belong to the project; storing them per version is a mechanism.

    ``project_fields`` calls them chain-level because they must survive the
    deletion of every version -- which they now do, by being kept on each
    tombstone and summed, rather than by being copied onto a survivor.
    """
    from caper.project_fields import level_of

    for field in DATED_COUNTERS:
        assert level_of(field) == 'chain'
