"""The audit page must resolve a project's chain from the document, not its name.

The fault these lock down, reported from dev on 2026-09-02: three live projects
named ``test``/``Test``, one with 118 samples and the others with 7 and 1. All
three rows resolved to the same chain -- whichever document ``find_one`` happened
to return for the name -- so two of them were compared against the 118-sample
project's log entry and reported ``Sample Count 118 vs 7`` as a mismatch. The
118-sample project itself came back "not recorded".

Measured the same day, before the fix: **7 of dev's 53 live projects and 12 of
production's 103** resolved to a chain not containing themselves. Three separate
mechanisms produced that, and only the first is what "duplicate names" suggests:

1. two live projects with the same name (``empty project``),
2. the name match being case-insensitive (``Test`` vs ``test``),
3. ``alias_name`` being tried *before* the name, and colliding across documents
   -- resolving the project named ``Contino`` returned ``Contino_1-3_agguploader``,
   which carries ``alias_name: 'Contino'``.

The third is why making project names unique would not have fixed this, and why
the invariant tested here is "never look at a name", not "names are unique".
"""

import inspect
import re

import pytest
from bson import ObjectId

from caper import lineage, utils


class FakeCollection:
    """Answers the two query shapes the resolver sends, and records them all."""

    def __init__(self, documents):
        self.documents = documents
        self.queries = []

    def find(self, filter=None, projection=None):
        self.queries.append(filter or {})
        wanted = (filter or {}).get('version_chain_id')
        if wanted is not None:
            return [d for d in self.documents if d.get('version_chain_id') == wanted]
        linkid = (filter or {}).get('previous_versions.linkid')
        if linkid is not None:
            return [d for d in self.documents
                    if any(str(e.get('linkid')) == linkid
                           for e in d.get('previous_versions', []) or [])]
        return []

    def find_one(self, filter=None, projection=None):
        self.queries.append(filter or {})
        results = self.find(filter, projection)
        return results[0] if results else None


def version(chain, ordinal, name, is_latest=False, **extra):
    doc = {'_id': ObjectId(), 'version_chain_id': chain, 'version_ordinal': ordinal,
           'is_latest': is_latest, 'project_name': name}
    doc.update(extra)
    return doc


@pytest.fixture
def resolve(monkeypatch):
    """Run the real resolver against a collection we control."""
    def _install(documents):
        fake = FakeCollection(documents)
        monkeypatch.setattr(utils, 'collection_handle_primary', fake)
        return fake
    return _install


# --------------------------------------------------------------------------
# The reported fault
# --------------------------------------------------------------------------

def test_projects_sharing_a_name_resolve_to_their_own_chains(resolve):
    """The dev report: three live projects named test/Test/test.

    Each must come back with only its own chain. Before the fix all three
    returned the same one, which is what compared a 7-sample document against a
    118-sample log entry.
    """
    big = version('chain-big', 1, 'test', is_latest=True, sample_count=118)
    small = version('chain-small', 1, 'test', is_latest=True, sample_count=7)
    capital = version('chain-capital', 1, 'Test', is_latest=True, sample_count=1)
    resolve([big, small, capital])

    for document in (big, small, capital):
        uuids, _ = utils.get_project_version_chain_for_document(document)
        assert uuids == [str(document['_id'])], (
            f"{document['project_name']!r} with {document['sample_count']} samples "
            f"resolved to someone else's chain")


def test_a_name_that_differs_only_in_case_is_a_different_project(resolve):
    """The old lookup matched with `$options: 'i'`, so Test and test were one key."""
    lower = version('chain-lower', 1, 'test', is_latest=True)
    upper = version('chain-upper', 1, 'Test', is_latest=True)
    resolve([lower, upper])

    assert utils.get_project_version_chain_for_document(lower)[0] == [str(lower['_id'])]
    assert utils.get_project_version_chain_for_document(upper)[0] == [str(upper['_id'])]


def test_an_alias_matching_another_projects_name_does_not_capture_it(resolve):
    """The `Contino` case, and the one unique project names would not have fixed.

    `alias_name` was tried before `project_name`, so a document whose alias
    happens to equal another project's name won the lookup outright.
    """
    contino = version('chain-contino', 1, 'Contino', is_latest=True,
                      alias_name='ContinoTest')
    impostor = version('chain-impostor', 1, 'Contino_1-3_agguploader',
                       is_latest=True, alias_name='Contino')
    resolve([contino, impostor])

    uuids, _ = utils.get_project_version_chain_for_document(contino)
    assert uuids == [str(contino['_id'])]
    assert str(impostor['_id']) not in uuids


def test_the_resolver_never_queries_a_name(resolve):
    """The invariant. Not "names are unique" -- names are not, and need not be."""
    doc = version('chain-a', 2, 'shared name', is_latest=True,
                  previous_versions=[{'linkid': str(ObjectId())}])
    fake = resolve([doc, version('chain-b', 1, 'shared name', is_latest=True)])

    utils.get_project_version_chain_for_document(doc)

    for query in fake.queries:
        assert 'project_name' not in query and 'alias_name' not in query, query


# --------------------------------------------------------------------------
# It still has to return the whole chain
# --------------------------------------------------------------------------

def test_a_pointered_chain_comes_back_whole_and_in_order(resolve):
    first = version('chain', 1, 'Proj')
    second = version('chain', 2, 'Proj renamed', is_latest=True)
    resolve([first, second])

    uuids, display = utils.get_project_version_chain_for_document(first)
    assert set(uuids) == {str(first['_id']), str(second['_id'])}
    assert display == 'Proj renamed', "the display name is the head's, not the caller's"


def test_an_unpointered_document_still_finds_its_versions(resolve):
    """The 26 dev documents the backfill refused to order must not regress."""
    old_id = ObjectId()
    doc = {'_id': ObjectId(), 'project_name': 'Legacy',
           'previous_versions': [{'linkid': str(old_id)}]}
    newer = {'_id': ObjectId(), 'project_name': 'Legacy v3',
             'previous_versions': [{'linkid': str(doc['_id'])}]}
    resolve([doc, newer])

    uuids, display = utils.get_project_version_chain_for_document(doc)
    assert set(uuids) == {str(doc['_id']), str(old_id), str(newer['_id'])}
    assert display == 'Legacy v3'


def test_previous_versions_ids_are_added_even_when_pointers_exist(resolve):
    """The two encodings are kept in step by the validator, not by construction.

    So the audit unions them: an id named by the array but missing from the
    chain is still a uuid this project's log entries may be filed under.
    """
    stray = ObjectId()
    doc = version('chain', 1, 'Proj', is_latest=True,
                  previous_versions=[{'linkid': str(stray)}])
    resolve([doc])

    uuids, _ = utils.get_project_version_chain_for_document(doc)
    assert set(uuids) == {str(doc['_id']), str(stray)}


def test_a_pointered_document_does_not_scan_previous_versions_of_others(resolve):
    """The forward scan is the read amplification the pointers exist to remove.

    It stays for unpointered documents, which have nothing else to go on, and
    must not run for the rest -- this page was measured at 157 s on prod.
    """
    doc = version('chain', 1, 'Proj', is_latest=True)
    fake = resolve([doc])

    utils.get_project_version_chain_for_document(doc)

    assert not any('previous_versions.linkid' in q for q in fake.queries)


def test_an_unpointered_document_does_scan(resolve):
    doc = {'_id': ObjectId(), 'project_name': 'Legacy'}
    fake = resolve([doc])

    utils.get_project_version_chain_for_document(doc)

    assert any('previous_versions.linkid' in q for q in fake.queries)


# --------------------------------------------------------------------------
# The call sites
# --------------------------------------------------------------------------

def test_no_audit_call_site_resolves_a_chain_by_name():
    """All three call sites already held the document; the name was never needed.

    A source check, because the fault was not in the resolver -- it did what it
    was asked -- but in three callers choosing the wrong key for it.
    """
    from caper import views_admin

    source = inspect.getsource(views_admin)
    assert 'get_project_version_chain(' not in source, (
        "a caller is resolving a version chain from a name again")
    assert source.count('get_project_version_chain_for_document(') >= 3


def test_the_audit_projection_carries_what_the_resolver_reads():
    """Resolving from the document only works if the document has the fields."""
    from caper import views_admin

    fields = views_admin._AUDIT_PROJECT_FIELDS
    assert 'previous_versions' in fields
    for pointer_field in lineage.POINTER_PROJECTION:
        assert pointer_field in fields, pointer_field
