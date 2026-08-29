"""Tests for validate_project_lineage.py -- the lineage invariant checkers.

Against a hand-built population rather than a database, because what is under
test is the rule each checker applies, and every one of them reads documents
that are already in memory.  ``backfill_project_status`` has the tests that need
real Mongo query semantics.

The load-bearing test here is ``test_no_invariant_claims_a_field_is_unwritten``.
The whole file exists because this validator spent a day reporting eight
invariants as "not checkable -- nothing writes that field yet" while all 311
production documents carried the field.  A validator's coverage is a claim about
the data, and a claim about the data has to be measured against the data.
"""

import pytest
from bson import ObjectId

from caper.project_status import TOMBSTONE
from validate_project_lineage import (
    INVARIANTS, Snapshot, Unavailable, _check_i1, _check_i2, _check_i3,
    _check_i4, _check_i5, _check_i11, _check_i15, _check_i16, _check_i20,
    _check_i21,
)


class FakeCollection:
    """Enough of a collection for Snapshot: find() ignoring the projection.

    The projection is an exclusion of three large fields the checkers never
    read, so ignoring it changes nothing they can observe.
    """

    def __init__(self, documents):
        self.documents = documents

    def find(self, filter=None, projection=None):
        return iter(self.documents)

    def count_documents(self, filter=None):
        return len(self.documents)


def snapshot(documents):
    return Snapshot(FakeCollection(documents), FakeCollection([]),
                    skip_gridfs=True)


def chain(*versions, chain_id=None):
    """A well-formed chain: pointers, ordinals, is_latest and the array agree.

    Each *version* is a dict of overrides, oldest first.  Built here rather than
    written out per test so a test that means to break one thing breaks exactly
    that thing.
    """
    ids = [spec.get('_id') or ObjectId() for spec in versions]
    chain_id = chain_id or ids[0]
    documents = []
    for index, (doc_id, spec) in enumerate(zip(ids, versions)):
        doc = {
            '_id': doc_id,
            'project_name': spec.get('project_name', f'proj-{index + 1}'),
            'version_chain_id': chain_id,
            'previous_version_id': ids[index - 1] if index else None,
            'next_version_id': ids[index + 1] if index + 1 < len(ids) else None,
            'version_ordinal': index + 1,
            'is_latest': index + 1 == len(ids),
            'previous_versions': [{'linkid': str(i)} for i in ids[:index]],
            # Head is live, everything before it superseded.  classify() reads
            # this pair and nothing else.
            'delete': index + 1 != len(ids),
            'current': index + 1 == len(ids),
        }
        doc['status'] = 'LIVE' if doc['current'] and not doc['delete'] else 'SUPERSEDED'
        doc.update({k: v for k, v in spec.items() if k != 'project_name'})
        documents.append(doc)
    return documents


HEALTHY = chain({}, {}, {}) + chain({})


# ---------------------------------------------------------------------------
# The healthy population passes every checker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('check', [_check_i1, _check_i2, _check_i3, _check_i4,
                                   _check_i5, _check_i11, _check_i15, _check_i16,
                                   _check_i20])
def test_healthy_population_has_no_findings(check):
    assert check(snapshot(HEALTHY)) == []


# ---------------------------------------------------------------------------
# Coverage is measured, not declared
# ---------------------------------------------------------------------------

def test_checkers_are_unavailable_when_the_backfill_has_not_run():
    """A pre-backfill document must make them skip, not pass.

    Reporting "ok" over a database with no lineage fields would be the failure
    this file guards against, in the other direction: a check that examined
    nothing saying everything is fine.
    """
    legacy = [{'_id': ObjectId(), 'project_name': 'old', 'delete': False,
               'current': True, 'previous_versions': []}]
    snap = snapshot(legacy)
    for check in (_check_i1, _check_i2, _check_i3, _check_i4, _check_i5,
                  _check_i11, _check_i15, _check_i16, _check_i20):
        with pytest.raises(Unavailable):
            check(snap)


def test_no_invariant_claims_a_field_is_unwritten():
    """No SKIP reason may name a field the population in front of it carries.

    This is the regression test for the defect the file was built to prevent
    and then committed itself.  Every invariant is run against a population
    carrying the full Phase 1 schema; any that skips must give a reason that
    does not name one of these fields, because these fields are demonstrably
    there.
    """
    snap = snapshot(HEALTHY)
    written = ('status', 'version_chain_id', 'version_ordinal', 'is_latest',
               'previous_version_id', 'next_version_id')
    for invariant in INVARIANTS:
        if invariant.check is None:
            reason = invariant.needs or ''
        else:
            try:
                invariant.check(snap)
                continue
            except Unavailable as unavailable:
                reason = str(unavailable)
        named = [field for field in written if field in reason]
        assert not named, (
            f'{invariant.ident} skips because of {named}, but every document '
            f'in this population carries {named}. A skip reason has to be '
            f'measured against the data, not written down once.')


def test_a_half_finished_backfill_is_a_finding_not_a_skip():
    """Some documents with the fields and some without must not skip.

    An unstarted backfill is a gap; a half-finished one is a fault, and the
    difference is exactly what require() is for.
    """
    population = HEALTHY + [{'_id': ObjectId(), 'project_name': 'missed',
                             'delete': False, 'current': True}]
    findings = _check_i1(snapshot(population))
    assert len(findings) == 1
    assert 'version_chain_id' in findings[0].detail


# ---------------------------------------------------------------------------
# Each checker catches the thing it is for
# ---------------------------------------------------------------------------

def test_i1_reports_an_absent_field():
    documents = chain({}, {})
    del documents[0]['current']
    findings = _check_i1(snapshot(documents))
    assert [f.detail for f in findings] == ['absent: current']


def test_i2_reports_a_stored_status_that_disagrees_with_classify():
    documents = chain({}, {})
    documents[0]['status'] = 'LIVE'          # it is delete=True, current=False
    findings = _check_i2(snapshot(documents))
    assert len(findings) == 1
    assert "stored status 'LIVE'" in findings[0].detail
    assert 'SUPERSEDED' in findings[0].detail


def test_i2_counts_documents_with_no_stored_status_in_one_line():
    documents = chain({}, {})
    del documents[0]['status']
    findings = _check_i2(snapshot(documents))
    assert len(findings) == 1
    assert '1 document(s) store no status' in findings[0].detail


def test_i3_reports_two_heads_in_one_chain():
    documents = chain({}, {})
    documents[0]['is_latest'] = True
    findings = _check_i3(snapshot(documents))
    assert len(findings) == 1
    assert 'has 2 is_latest members' in findings[0].detail


def test_i3_reports_a_chain_with_no_head():
    documents = chain({}, {})
    documents[-1]['is_latest'] = False
    findings = _check_i3(snapshot(documents))
    assert len(findings) == 1
    assert 'has 0 is_latest members' in findings[0].detail


def test_i4_reports_an_ordinal_gap():
    documents = chain({}, {}, {})
    documents[2]['version_ordinal'] = 4
    findings = _check_i4(snapshot(documents))
    assert len(findings) == 1
    assert '[1, 2, 4]' in findings[0].detail


def test_i4_reports_a_tie():
    documents = chain({}, {})
    documents[1]['version_ordinal'] = 1
    findings = _check_i4(snapshot(documents))
    assert len(findings) == 1


def test_i5_reports_a_broken_inverse():
    documents = chain({}, {})
    documents[1]['previous_version_id'] = ObjectId()   # points elsewhere
    details = ' '.join(f.detail for f in _check_i5(snapshot(documents)))
    assert 'is not in the collection' in details
    assert 'next_version_id' in details


def test_i5_does_not_require_is_latest_to_agree_with_the_pointer():
    """A promoted head keeps a next_version_id, and that is correct.

    The shape after the head version of a two-version chain is deleted: the
    tombstone stays in the chain holding its ordinal, its predecessor is
    promoted, and the predecessor therefore has is_latest=True *and* a
    next_version_id pointing at the version it outlived.

    I5 used to call that a violation, on the reasoning that the flag is derived
    from the pointer. It is not: pointers are structure, is_latest is position.
    Exactly one head per chain is I3's and I16's job, and they still say so.
    """
    documents = chain({}, {})
    documents[0]['is_latest'] = True         # promoted, still points at [1]
    documents[1]['is_latest'] = False        # the deleted head
    assert _check_i5(snapshot(documents)) == []


def test_i11_does_not_call_a_deleted_version_a_divergence():
    """The array cannot say "this version was deleted".

    Deleting the middle version of a three-version chain removes it from the
    head's previous_versions[] while it stays in the chain holding its ordinal.
    From the first deletion onwards the pointer lineage is a strict superset of
    the array -- by design, which is the entire reason the pointers exist. An
    I11 that compared across tombstones would report every correct deletion as
    drift, and the first person to see that would switch the check off.
    """
    documents = chain({}, {}, {})
    documents[1].update({'status': 'TOMBSTONE', 'delete': True, 'current': False,
                         'version_deleted_from_history': True,
                         'payload_purged': True})
    documents[1].pop('previous_versions')          # replace_one drops it
    documents[2]['previous_versions'] = [{'linkid': str(documents[0]['_id'])}]

    assert _check_i11(snapshot(documents)) == []


def test_i11_still_reports_a_surviving_version_the_array_does_not_name():
    """The exclusion is for tombstones, not for anything missing.

    A live ancestor absent from the array is a history table rendering short,
    and that is exactly what I11 is for.
    """
    documents = chain({}, {}, {})
    documents[2]['previous_versions'] = [{'linkid': str(documents[1]['_id'])}]
    findings = _check_i11(snapshot(documents))
    assert len(findings) == 1
    assert str(documents[0]['_id']) in findings[0].detail


def test_i11_reports_an_array_entry_the_pointers_do_not_place_before_it():
    documents = chain({}, {})
    stranger = ObjectId()
    documents.append({'_id': stranger, 'project_name': 'other', 'delete': False,
                      'current': True, 'status': 'LIVE',
                      'version_chain_id': stranger, 'previous_version_id': None,
                      'next_version_id': None, 'version_ordinal': 1,
                      'is_latest': True, 'previous_versions': []})
    documents[1]['previous_versions'].append({'linkid': str(stranger)})
    findings = _check_i11(snapshot(documents))
    assert len(findings) == 1
    assert 'the pointers do not place before it' in findings[0].detail


def test_i11_reports_a_pointer_ancestor_the_array_does_not_name():
    documents = chain({}, {})
    documents[1]['previous_versions'] = []
    findings = _check_i11(snapshot(documents))
    assert len(findings) == 1
    assert 'previous_versions[] names 0' in findings[0].detail


def test_i11_says_so_when_the_array_is_absent_entirely():
    documents = chain({}, {})
    del documents[1]['previous_versions']
    findings = _check_i11(snapshot(documents))
    assert len(findings) == 1
    assert 'absent entirely' in findings[0].detail


def test_i11_ignores_a_dangling_reference_because_i6_owns_it():
    documents = chain({}, {})
    documents[1]['previous_versions'].append({'linkid': str(ObjectId())})
    assert _check_i11(snapshot(documents)) == []


def test_i15_reports_stored_chain_emptiness():
    documents = chain({}, {})
    documents[0]['chain_empty'] = False
    findings = _check_i15(snapshot(documents))
    assert len(findings) == 1
    assert 'chain_empty' in findings[0].detail


def test_i16_reports_a_headless_chain_with_no_live_member():
    documents = chain({}, {})
    for doc in documents:
        doc.update({'version_deleted_from_history': True, 'payload_purged': True,
                    'status': TOMBSTONE, 'is_latest': False})
    findings = _check_i16(snapshot(documents))
    assert len(findings) == 1
    assert 'no LIVE member and 0 is_latest' in findings[0].detail


def test_i16_accepts_a_tombstone_head():
    """An all-tombstone chain with a head is correct, not a violation.

    is_latest is position, status is state.  A project whose versions have all
    been deleted still has a current version -- the one a restore lands in.
    """
    documents = chain({}, {})
    for doc in documents:
        doc.update({'version_deleted_from_history': True, 'payload_purged': True,
                    'status': TOMBSTONE})
    assert _check_i16(snapshot(documents)) == []


def test_i20_reports_a_tombstone_head_beside_a_survivor():
    """The shape the promotion bug produced, which nothing else caught.

    Deleting the head promoted a tombstone instead of the surviving version.
    I3, I4, I5, I8 and I16 all pass over the result -- one head, contiguous
    ordinals, mutual pointers, purged payloads. Only the relationship between
    the head's status and its siblings' says anything is wrong.
    """
    documents = chain({}, {}, {})
    marks = {'version_deleted_from_history': True, 'payload_purged': True,
             'status': TOMBSTONE, 'delete': True, 'current': False}
    documents[1].update(marks, is_latest=True)      # the promoted tombstone
    documents[2].update(marks, is_latest=False)     # the deleted head
    findings = _check_i20(snapshot(documents))
    assert len(findings) == 1
    assert str(documents[0]['_id']) in findings[0].detail
    assert '1 version(s) survive' in findings[0].detail


def test_i20_accepts_an_emptied_chain():
    """Every member a tombstone is an emptied project, which T6 requires.

    I16 says the same thing from the other side; this must not contradict it.
    """
    documents = chain({}, {})
    for doc in documents:
        doc.update({'version_deleted_from_history': True, 'payload_purged': True,
                    'status': TOMBSTONE})
    assert _check_i20(snapshot(documents)) == []


def test_i20_accepts_a_live_head_over_tombstoned_ancestors():
    """The ordinary result of deleting an old version, which must stay silent."""
    documents = chain({}, {}, {})
    documents[1].update({'version_deleted_from_history': True,
                         'payload_purged': True, 'status': TOMBSTONE,
                         'delete': True, 'current': False})
    assert _check_i20(snapshot(documents)) == []


def test_i11_does_not_call_an_array_named_tombstone_a_divergence():
    """T9: the new version's array names the tombstone it was built on.

    The array is not only the compatibility encoding of the history table --
    it is also what tells the write path which chain to extend. Re-populating
    an emptied project therefore names a tombstone there, necessarily, and an
    I11 that filtered only the pointer side reported that correct result as a
    divergence on dev.

    Filtering one side and not the other compares one encoding against a subset
    of the other, which is not what this invariant is for.
    """
    documents = chain({}, {})
    documents[0].update({'status': 'TOMBSTONE', 'delete': True, 'current': False,
                         'version_deleted_from_history': True,
                         'payload_purged': True})
    documents[0].pop('previous_versions')
    # documents[1] still names its tombstoned predecessor, as T9 leaves it
    assert _check_i11(snapshot(documents)) == []


# ---------------------------------------------------------------------------
# I21 -- no GridFS file is named by more than one document
#
# The rule, not the loading: Snapshot builds gridfs_ids by streaming the heavy
# field out of the collection, and a fake that reproduced that would be testing
# pymongo. What matters here is what the checker concludes from the map.
# ---------------------------------------------------------------------------

def _with_gridfs(documents, gridfs_ids):
    snap = snapshot(documents)
    snap.gridfs_skipped = False
    snap.gridfs_ids = gridfs_ids
    return snap


def test_i21_passes_when_every_file_has_one_owner():
    first, second = ObjectId(), ObjectId()
    documents = chain({'_id': first}) + chain({'_id': second})

    findings = _check_i21(_with_gridfs(documents, {
        first: [ObjectId(), ObjectId()],
        second: [ObjectId()],
    }))

    assert findings == []


def test_i21_reports_a_file_two_documents_name():
    """The case every deletion path would get wrong."""
    first, second = ObjectId(), ObjectId()
    shared = ObjectId()
    documents = chain({'_id': first}) + chain({'_id': second})

    findings = _check_i21(_with_gridfs(documents, {
        first: [shared, ObjectId()],
        second: [shared],
    }))

    assert len(findings) == 1
    assert str(shared) in findings[0].detail
    assert 'named by 2 documents' in findings[0].detail


def test_i21_does_not_fire_when_one_document_names_a_file_twice():
    """Two slots of one document pointing at one file is not sharing.

    A document can name the same file from more than one key -- the same plot
    reached as a feature file and through a directory slot. Counting that as
    two owners would make this fire on the ordinary case and train everyone to
    ignore it.
    """
    only = ObjectId()
    twice = ObjectId()

    findings = _check_i21(_with_gridfs(chain({'_id': only}),
                                       {only: [twice, twice, ObjectId()]}))

    assert findings == []


def test_i21_says_nothing_when_gridfs_was_not_read():
    """--skip-gridfs means unmeasured, which is not the same as clean."""
    only = ObjectId()
    snap = snapshot(chain({'_id': only}))

    assert snap.gridfs_skipped is True
    assert _check_i21(snap) == []
