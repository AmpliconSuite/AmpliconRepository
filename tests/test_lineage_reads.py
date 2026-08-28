"""Tests for caper.lineage and the read paths that now go through it.

The rule these enforce is the one the differential harness measured against
both servers on 2026-08-27: switching a read path onto the pointers may add
rows and correct stale ones, but it may never *drop* a row the array reader
had. Measured then: prod 308 of 311 history tables byte-identical, dev 180 of
234, and **zero rows lost on either**.

The fallback matters as much as the pointer path. A document the backfill never
reached -- 26 of them on dev, in the six chains it refused to order -- must
render the history it always had, not an empty one.
"""

import pytest
from bson import ObjectId

from caper import lineage


class FakeCollection:
    """A collection that answers the two queries lineage issues.

    Supports ``{'version_chain_id': x}`` and ``{'version_chain_id': {'$in': [...]}}``
    plus a projection it ignores, which is every shape this module sends.
    """

    def __init__(self, documents):
        self.documents = documents
        self.queries = []

    def find(self, filter=None, projection=None):
        self.queries.append(filter)
        wanted = (filter or {}).get('version_chain_id')
        if isinstance(wanted, dict):
            allowed = set(wanted.get('$in', []))
            return [d for d in self.documents
                    if d.get('version_chain_id') in allowed]
        return [d for d in self.documents if d.get('version_chain_id') == wanted]


def version(chain, ordinal, is_latest=False, **extra):
    doc = {'_id': ObjectId(), 'version_chain_id': chain,
           'version_ordinal': ordinal, 'is_latest': is_latest,
           'project_name': f'v{ordinal}'}
    doc.update(extra)
    return doc


@pytest.fixture
def chain():
    cid = ObjectId()
    members = [version(cid, 1), version(cid, 2), version(cid, 3, is_latest=True)]
    return cid, members, FakeCollection(list(reversed(members)))


# ---------------------------------------------------------------------------
# Reading a chain
# ---------------------------------------------------------------------------

def test_members_come_back_in_ordinal_order_whatever_mongo_returns(chain):
    _cid, members, collection = chain
    read = lineage.chain_members(collection, members[0])
    assert [d['_id'] for d in read] == [d['_id'] for d in members]


def test_an_unpointered_document_returns_none_not_an_empty_list():
    """The distinction the whole fallback rests on.

    An empty list would mean "this project has no history" and would render a
    blank table. None means "ask the array", which is what a pre-backfill
    document needs.
    """
    collection = FakeCollection([])
    assert lineage.chain_members(collection, {'_id': ObjectId()}) is None


def test_a_chain_id_naming_nothing_still_returns_the_document_itself():
    """A page must render even when the chain has gone missing."""
    doc = {'_id': ObjectId(), 'version_chain_id': ObjectId()}
    assert lineage.chain_members(FakeCollection([]), doc) == [doc]


def test_head_is_the_is_latest_member(chain):
    _cid, members, collection = chain
    read = lineage.chain_members(collection, members[0])
    assert lineage.head(read)['_id'] == members[2]['_id']


def test_head_falls_back_to_the_highest_ordinal_when_the_flag_is_missing():
    cid = ObjectId()
    members = [version(cid, 1), version(cid, 2)]
    collection = FakeCollection(members)
    read = lineage.chain_members(collection, members[0])
    assert lineage.head(read)['_id'] == members[1]['_id']


def test_head_falls_back_when_two_members_claim_the_flag():
    cid = ObjectId()
    members = [version(cid, 1, is_latest=True), version(cid, 2, is_latest=True)]
    read = lineage.chain_members(FakeCollection(members), members[0])
    assert lineage.head(read)['_id'] == members[1]['_id']


def test_a_chain_that_cannot_be_ordered_still_renders_in_a_stable_order():
    """I4 says ordinals are contiguous from 1. This is what a false I4 looks
    like in front of a user: a stable order, not a traceback on a page load."""
    cid = ObjectId()
    members = [version(cid, None), version(cid, 2), version(cid, 1)]
    read = lineage.chain_members(FakeCollection(members), members[0])
    assert [d.get('version_ordinal') for d in read] == [1, 2, None]
    assert [d['_id'] for d in lineage.chain_members(FakeCollection(members),
                                                    members[0])] == \
           [d['_id'] for d in read]


def test_ancestors_are_the_members_before_this_one(chain):
    _cid, members, collection = chain
    read = lineage.chain_members(collection, members[1])
    assert [d['_id'] for d in lineage.ancestors(read, members[1])] == \
           [members[0]['_id']]
    assert lineage.ancestors(read, members[0]) == []
    assert len(lineage.ancestors(read, members[2])) == 2


def test_is_head(chain):
    _cid, members, collection = chain
    read = lineage.chain_members(collection, members[0])
    assert lineage.is_head(members[2], read)
    assert not lineage.is_head(members[0], read)


# ---------------------------------------------------------------------------
# The batched read the list endpoint uses
# ---------------------------------------------------------------------------

def test_chains_for_reads_every_chain_in_one_query():
    """One query for a page of projects, not one per project.

    The list endpoint serialises every project a user can see; a query per row
    is the read amplification this codebase has had to go and fix twice.
    """
    first, second = ObjectId(), ObjectId()
    members = [version(first, 1, is_latest=True),
               version(second, 1), version(second, 2, is_latest=True)]
    collection = FakeCollection(members)

    grouped = lineage.chains_for(collection, [members[0], members[2]])

    assert len(collection.queries) == 1
    assert set(grouped) == {first, second}
    assert [d['_id'] for d in grouped[second]] == \
           [members[1]['_id'], members[2]['_id']]


def test_chains_for_omits_unpointered_documents_rather_than_guessing():
    collection = FakeCollection([])
    assert lineage.chains_for(collection, [{'_id': ObjectId()}]) == {}
    assert collection.queries == []


def test_resolve_id_returns_none_rather_than_raising_on_a_page_load():
    oid = ObjectId()
    assert lineage.resolve_id(oid) is oid
    assert lineage.resolve_id(str(oid)) == oid
    assert lineage.resolve_id('[{"date": "2024-01-01"}]') is None
    assert lineage.resolve_id(None) is None


# ---------------------------------------------------------------------------
# previous_versions() through the real function, both paths
#
# FakeHistoryCollection is borrowed rather than reimplemented: it evaluates
# queries with project_status.matches(), which is itself checked against a real
# MongoDB. A second fake here would be a second opinion about what a query
# means, in a repository whose defining defect is a predicate maintained twice.
# ---------------------------------------------------------------------------

def _project_doc(doc_id, name, ordinal=None, chain=None, is_latest=False,
                 delete=False, current=True, **extra):
    doc = {'_id': doc_id, 'project_name': name, 'delete': delete,
           'current': current, 'date': f'2026-0{ordinal or 1}-01T00:00:00.000000',
           'AA_version': f'AA-{ordinal or 1}'}
    if chain is not None:
        doc.update({'version_chain_id': chain, 'version_ordinal': ordinal,
                    'is_latest': is_latest})
    doc.update(extra)
    return doc


def test_previous_versions_reads_the_chain_when_the_pointers_are_there(monkeypatch):
    from test_project_version_cleanup import FakeHistoryCollection
    from caper import utils

    chain_id = ObjectId()
    old_id, new_id = ObjectId(), ObjectId()
    old = _project_doc(old_id, 'p', 1, chain_id, delete=True, current=False)
    new = _project_doc(new_id, 'p', 2, chain_id, is_latest=True,
                       previous_versions=[{'linkid': str(old_id)}])
    monkeypatch.setattr(utils, 'collection_handle',
                        FakeHistoryCollection([old, new]))

    entries, msg = utils.previous_versions(new)

    assert [e['linkid'] for e in entries] == [str(new_id), str(old_id)]
    assert msg is None
    # The version column comes off each version's own document, so it is right
    # even when the copy stored in the array went stale.
    assert {e['linkid']: e['AA_version'] for e in entries} == \
        {str(new_id): 'AA-2', str(old_id): 'AA-1'}


def test_viewing_an_older_version_gets_the_banner_from_the_chain(monkeypatch):
    from test_project_version_cleanup import FakeHistoryCollection
    from caper import utils

    chain_id = ObjectId()
    old_id, new_id = ObjectId(), ObjectId()
    old = _project_doc(old_id, 'p', 1, chain_id, delete=True, current=False)
    new = _project_doc(new_id, 'p', 2, chain_id, is_latest=True)
    monkeypatch.setattr(utils, 'collection_handle',
                        FakeHistoryCollection([old, new]))

    _entries, msg = utils.previous_versions(old)
    assert msg is not None and str(new_id) in msg


def test_a_chain_with_no_live_member_still_names_its_latest_version(monkeypatch):
    """The prod case: two superseded versions, no live head.

    The array reader looks for a successor with the head flag set and finds
    nothing, so the newer version was invisible from the older one's page. One
    such chain on production, 2026-08-27, and the pointer reader shows both.
    """
    from test_project_version_cleanup import FakeHistoryCollection
    from caper import utils

    chain_id = ObjectId()
    old_id, new_id = ObjectId(), ObjectId()
    old = _project_doc(old_id, 'p', 1, chain_id, delete=True, current=False)
    new = _project_doc(new_id, 'p', 2, chain_id, is_latest=True,
                       delete=True, current=False,
                       previous_versions=[{'linkid': str(old_id)}])
    monkeypatch.setattr(utils, 'collection_handle',
                        FakeHistoryCollection([old, new]))

    entries, msg = utils.previous_versions(old)

    assert {e['linkid'] for e in entries} == {str(old_id), str(new_id)}
    assert msg is not None and str(new_id) in msg


def test_a_document_without_pointers_falls_back_to_the_array(monkeypatch):
    """26 documents on dev are in this state and must not lose their history."""
    from test_project_version_cleanup import FakeHistoryCollection
    from caper import utils

    old_id, new_id = ObjectId(), ObjectId()
    old = _project_doc(old_id, 'p', 1, delete=True, current=False)
    new = _project_doc(new_id, 'p', 2,
                       previous_versions=[{'linkid': str(old_id),
                                           'date': '2026-01-01T00:00:00.000000'}])
    monkeypatch.setattr(utils, 'collection_handle',
                        FakeHistoryCollection([old, new]))

    entries, _msg = utils.previous_versions(new)

    assert {e['linkid'] for e in entries} == {str(old_id), str(new_id)}


def test_the_fallback_renders_history_rather_than_an_empty_table(monkeypatch):
    """The failure this guards against is silent: an unpointered document
    rendering a blank history instead of the one it has always had."""
    from test_project_version_cleanup import FakeHistoryCollection
    from caper import utils

    old_id, new_id = ObjectId(), ObjectId()
    new = _project_doc(new_id, 'p', 2,
                       previous_versions=[{'linkid': str(old_id)}])
    monkeypatch.setattr(utils, 'collection_handle', FakeHistoryCollection([new]))

    entries, _msg = utils.previous_versions(new)
    assert len(entries) == 2       # the head, plus the ancestor it names
