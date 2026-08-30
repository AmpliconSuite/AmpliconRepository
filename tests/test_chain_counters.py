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
        """Enough query support for the two shapes the callers use.

        ``{'version_chain_id': <id>}`` for one chain, and
        ``{'version_chain_id': {'$ne': None}}`` for every document that names
        one. Written out rather than assumed: a fake that silently matches
        nothing makes a script that does nothing look correct.
        """
        chain = query.get('version_chain_id')
        if isinstance(chain, dict):
            if '$ne' in chain:
                return [d for d in self.docs.values()
                        if d.get('version_chain_id') != chain['$ne']]
            if '$in' in chain:
                return [d for d in self.docs.values()
                        if d.get('version_chain_id') in chain['$in']]
            raise AssertionError(f'unsupported query: {query}')
        return [d for d in self.docs.values()
                if d.get('version_chain_id') == chain]

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


# --- views joined the same rule on 2026-08-30 -----------------------------

def test_a_new_version_starts_both_counters_at_zero():
    """The seeding is what made the numbers unsummable, so it had to stop.

    A version seeded from its predecessor overlaps it by an amount nobody
    records. Reading the two values afterwards cannot recover the split -- the
    old version goes on earning through old links -- so the fix is to stop
    creating the overlap, not to correct for it.
    """
    from pathlib import Path

    source = (Path(__file__).parents[1] / 'caper' / 'caper' / 'views.py').read_text()

    assert "project['views'] = 0" in source
    assert "project['downloads'] = 0" in source
    assert "project['views'] = previous_views[0]" not in source


def test_views_is_summed_over_the_chain_like_downloads():
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False, views=40),
        _version(_id='v2', version_ordinal=2, views=9),
    ])

    assert download_totals.chain_sum(collection, collection.docs['v2'],
                                     'views') == 49


def test_a_boolean_view_count_is_not_a_view():
    """isinstance(True, int) is True in Python, so this needs saying."""
    collection = FakeProjects([_version(views=True)])

    assert download_totals.chain_sum(collection, collection.docs['v1'],
                                     'views') == 0


def test_the_reconciliation_leaves_the_head_alone():
    """Zeroing the head would delete the only record of the carried history.

    The older versions' counts are already inside the head's number. Zeroing
    them makes the sum equal the head; zeroing the head as well would make it
    zero.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / 'zero_carried_forward_views.py'
    spec = importlib.util.spec_from_file_location('zcfv', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False, views=40),
        _version(_id='v2', version_ordinal=2, is_latest=True, views=100),
    ])
    to_zero, head_total, would_be_sum, touched = module.plan(collection)

    assert [d['_id'] for d in to_zero] == ['v1']
    assert head_total == 100
    assert would_be_sum == 140      # what summing without this would report
    assert touched == 1


def test_a_chain_with_no_clear_head_is_left_alone():
    """Guessing which member is the head is how the wrong counter gets zeroed."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / 'zero_carried_forward_views.py'
    spec = importlib.util.spec_from_file_location('zcfv', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=True, views=40),
        _version(_id='v2', version_ordinal=2, is_latest=True, views=100),
    ])

    assert module.plan(collection)[0] == []


def test_the_reconciliation_refuses_to_run_twice():
    """After it has run, an old version's count is a real view, not residue.

    Found on the first dev run: 73 documents zeroed, and the immediate re-plan
    found one more -- a live page view that had landed while it worked. A
    second run would have deleted it. The guard is a marker, and the refusal is
    the default.
    """
    from pathlib import Path

    source = (Path(__file__).parents[1] /
              'zero_carried_forward_views.py').read_text()

    assert 'views_carry_forward_reconciled' in source
    assert "'--i-know'" in source
    # The marker is only consulted on the writing path, and refusing is what
    # happens without the override.
    assert 'refusing: this database was already reconciled' in source
    assert 'if marker and not args.i_know' in source


def test_the_page_says_total_because_that_is_what_it_now_shows():
    """The number changed meaning, so the label had to change with it.

    It used to be this version's count and now it is the project's, summed
    across every version including the tombstones of deleted ones. A reader
    comparing it against a per-version figure elsewhere needs the label to say
    which one they are looking at.
    """
    from pathlib import Path

    template = (Path(__file__).parents[1] / 'caper' / 'templates' / 'pages' /
                'project.html').read_text()

    assert 'Total downloads:' in template
    assert 'Total views:' in template


# --- repairing what promotion dropped ------------------------------------

def _repair_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / 'repair_head_chain_fields.py'
    spec = importlib.util.spec_from_file_location('rhcf', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_field_missing_from_the_head_is_restored_from_the_newest_holder():
    """Several versions can hold it with different values; the newest wins.

    That is what the project last meant. Taking the oldest, or the first the
    cursor happens to return, restores a value the project moved on from.
    """
    module = _repair_module()
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False,
                 publication_link='old'),
        _version(_id='v2', version_ordinal=2, is_latest=False,
                 publication_link='new'),
        _version(_id='v3', version_ordinal=3, is_latest=True),
    ])

    repairs = module.plan(collection)

    assert [(r[0], r[2], r[3], r[4]) for r in repairs] == [
        ('v3', 'publication_link', 'new', 2)]


def test_a_head_that_already_has_the_field_is_left_alone():
    """A different value is a disagreement, not a gap, and not this script's."""
    module = _repair_module()
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False, featured=True),
        _version(_id='v2', version_ordinal=2, is_latest=True, featured=False),
    ])

    assert module.plan(collection) == []


def test_an_unfeatured_project_is_not_re_featured():
    """The admin control writes featured=False, so the field stays present.

    That is what makes absence unambiguous: it can only be inheritance, never
    a deliberate choice. If unfeaturing had unset the field instead, this
    script could not tell the two apart and should not exist.
    """
    from pathlib import Path

    admin = (Path(__file__).parents[1] / 'caper' / 'caper' /
             'views_admin.py').read_text()

    assert '{"$set": {\'featured\': featured}}' in admin


def test_a_chain_with_no_clear_head_is_skipped():
    module = _repair_module()
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=True, featured=True),
        _version(_id='v2', version_ordinal=2, is_latest=True),
    ])

    assert module.plan(collection) == []


def test_a_single_version_chain_has_nothing_to_restore_from():
    module = _repair_module()
    collection = FakeProjects([_version(_id='v1', is_latest=True)])

    assert module.plan(collection) == []


def test_only_declared_chain_level_fields_are_restored():
    """Copying a version-level field onto the head would be a lie about it."""
    module = _repair_module()
    collection = FakeProjects([
        _version(_id='v1', version_ordinal=1, is_latest=False,
                 sample_count=99, AA_version='1.2.3', featured=True),
        _version(_id='v2', version_ordinal=2, is_latest=True),
    ])

    restored = {r[2] for r in module.plan(collection)}

    assert restored == {'featured'}
