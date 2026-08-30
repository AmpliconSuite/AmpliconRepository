"""One counter per version, starting at zero, summed across the chain to read.

The design question this settles: a download belongs to the version that served
it, and the project's number is a sum. Nothing is carried forward, so nothing
has to be reconciled -- promotion copies no counter, deletion changes no total.

What went wrong before was not the absence of an owner. It was a *second*
counter for the same clicks, seeded from its predecessor on every new version,
and therefore neither summable nor safe to reset. The tests here hold the
per-version rule and the two ways the old code broke it.
"""
import pytest
from bson.objectid import ObjectId

from caper import download_totals


def _version(**over):
    doc = {'_id': 'v1', 'project_name': 'Study A', 'version_chain_id': 'chain-9',
           'version_ordinal': 1, 'is_latest': True}
    doc.update(over)
    return doc


class FakeProjects:
    """Enough of a collection to watch what the counter code actually writes."""

    def __init__(self, docs=None):
        self.docs = {doc['_id']: doc for doc in (docs or [])}
        self.updates = []

    def find(self, query, _projection=None):
        chain = (query.get('version_chain_id') or {})
        wanted = chain.get('$in', [chain]) if isinstance(chain, dict) else [chain]
        return [d for d in self.docs.values()
                if d.get('version_chain_id') in wanted]

    def find_one(self, query, projection=None):
        return self.docs.get(query['_id'])

    def find_one_and_update(self, query, update, projection=None,
                            return_document=None):
        self.update_one(query, update)
        return self.docs.get(query['_id'])

    def update_one(self, query, update):
        self.updates.append(update)
        doc = self.docs.setdefault(query['_id'], {'_id': query['_id']})
        for field, by in (update.get('$inc') or {}).items():
            if '.' in field:
                outer, key = field.split('.', 1)
                doc.setdefault(outer, {})
                doc[outer][key] = doc[outer].get(key, 0) + by
            else:
                doc[field] = doc.get(field, 0) + by
        for field, value in (update.get('$set') or {}).items():
            doc[field] = value


# --- the sum is the project's number --------------------------------------

def test_the_projects_number_is_the_sum_over_its_versions():
    """Each version counts its own; the project is the sum. That is the design."""
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False,
                 project_downloads={'2026-01-01': 100}),
        _version(_id='v2', version_ordinal=2,
                 project_downloads={'2026-06-01': 30}),
    ])

    totals = download_totals.chain_totals(collection, collection.docs['v2'])

    assert download_totals.total(totals['project_downloads']) == 130


def test_a_deleted_version_still_counts_toward_the_total():
    """Its downloads happened. A tombstone keeps its share for exactly this."""
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False, status='TOMBSTONE',
                 project_downloads={'2026-01-01': 100}),
        _version(_id='v2', version_ordinal=2,
                 project_downloads={'2026-06-01': 30}),
    ])

    totals = download_totals.chain_totals(collection, collection.docs['v2'])

    assert download_totals.total(totals['project_downloads']) == 130


def test_a_project_of_one_version_is_a_chain_of_one():
    collection = FakeProjects([_version(project_downloads={'2026-01-01': 7})])

    totals = download_totals.chain_totals(collection, collection.docs['v1'])

    assert download_totals.total(totals['project_downloads']) == 7


# --- nothing is carried forward, so nothing has to be reconciled -----------

def test_promotion_carries_no_download_counter():
    """There is nothing to carry: the older version already has its own.

    ``views`` is deliberately still carried, and that is a statement about
    today rather than about where it belongs -- no per-date record of views was
    ever kept, so its values cannot be summed and it is still read off the head.
    """
    from caper.project_fields import CARRIED_ON_PROMOTION, KEPT_WHERE_EARNED

    assert KEPT_WHERE_EARNED == {'project_downloads', 'sample_downloads',
                                 'downloads'}
    assert CARRIED_ON_PROMOTION.isdisjoint(KEPT_WHERE_EARNED)
    assert 'views' in CARRIED_ON_PROMOTION


# --- the two ways the old code broke the per-version rule -----------------

def test_counting_a_view_cannot_reset_the_download_count(monkeypatch):
    """The bug: an initialiser that set two fields to initialise one.

        if ('views' not in project) or ('downloads' not in project):
            update_one(query, {'$set': {'views': 1, 'downloads': 0}})

    A project missing only 'views' had its downloads zeroed by the next page
    view. On prod 2026-08-30 that had happened to seven public projects --
    CCLE, PCAWG and TCGA among them -- reading downloads=0 against 12 to 27
    downloads still recorded in their per-date dicts.
    """
    from caper import view_download_stats

    collection = FakeProjects([_version(downloads=27)])  # no 'views' field
    # monkeypatch, not a bare assignment with a `del` to undo it: the name
    # arrives here through `from .utils import *`, so deleting it removes the
    # module attribute outright rather than restoring what was there. Written
    # that way first, it left every later caller in the session raising
    # NameError -- the whole page-render matrix among them.
    monkeypatch.setattr(view_download_stats, 'collection_handle', collection)
    views = view_download_stats.get_increment_view_and_download_statistics(
        collection.docs['v1'])

    assert views == 1
    assert collection.docs['v1']['downloads'] == 27
    for update in collection.updates:
        assert '$set' not in update, f'a counter write used $set: {update}'


def test_a_download_is_counted_with_inc_not_read_modify_write(monkeypatch):
    """Two downloads between a read and a write used to lose one.

    The old code read project_downloads, added one in Python and wrote the dict
    back. Downloads arrive in bursts, which is exactly when that loses them.
    """
    from caper import views

    oid = ObjectId()
    collection = FakeProjects([_version(_id=oid,
                                        project_downloads={'2026-08-30': 5})])
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'get_date_short', lambda: '2026-08-30')
    monkeypatch.setattr(views, 'increment_download', lambda _project: None)

    views.update_project_download_count(collection.docs[oid], str(oid))

    assert collection.updates[0] == {'$inc': {'project_downloads.2026-08-30': 1}}
    assert collection.docs[oid]['project_downloads']['2026-08-30'] == 6


def test_a_legacy_bare_int_is_moved_under_a_date_rather_than_lost(monkeypatch):
    """Some documents predate the per-date form and hold a plain int."""
    from caper import views

    oid = ObjectId()
    collection = FakeProjects([_version(_id=oid, project_downloads=12)])
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'get_date_short', lambda: '2026-08-30')
    monkeypatch.setattr(views, 'increment_download', lambda _project: None)

    views.update_project_download_count(collection.docs[oid], str(oid))

    assert collection.docs[oid]['project_downloads'] == {'2026-08-30': 13}


def test_a_project_with_no_counter_yet_starts_at_one(monkeypatch):
    """$inc creates the field, which is why no initialiser is needed."""
    from caper import views

    oid = ObjectId()
    collection = FakeProjects([_version(_id=oid)])
    monkeypatch.setattr(views, 'collection_handle', collection)
    monkeypatch.setattr(views, 'get_date_short', lambda: '2026-08-30')
    monkeypatch.setattr(views, 'increment_download', lambda _project: None)

    views.update_project_download_count(collection.docs[oid], str(oid))

    assert collection.docs[oid]['project_downloads'] == {'2026-08-30': 1}


# --- the index that makes summing at read affordable ----------------------

def test_the_chain_lookup_is_indexed():
    """Without it the sum is a full scan of the projects collection.

    Measured on prod 2026-08-30, before the index: a flat 280 ms per lookup,
    the same for a one-version chain as for an eight-version one -- which is
    the signature of a scan rather than of the work being asked for.
    """
    from pathlib import Path

    apps = (Path(__file__).parents[1] / 'caper' / 'caper' / 'apps.py').read_text()

    assert "'version_chain_id'" in apps
    assert 'idx_project_version_chain' in apps


def test_the_page_reads_the_chain_and_not_the_head():
    from pathlib import Path

    source = (Path(__file__).parents[1] / 'caper' / 'caper' / 'views.py').read_text()

    assert 'download_totals.chain_totals(collection_handle, project)' in source
