"""The live audit writer stamps each event with its project's version-chain id.

An audit event without a chain id cannot be joined to a project's history, so
``views._audit_chain_id`` resolves it at write time. A backfill once filled in the
events written before this existed (it has since been retired); these tests hold
the property that matters going forward: new events arrive with the id, and a
lookup that fails never breaks the upload that triggered the write.
"""
from bson import ObjectId


class FakeCollection:
    """Enough of a collection for the audit-writer lookups and no more."""

    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    def _match(self, doc, query):
        for key, want in query.items():
            if key == '$or':
                if not any(self._match(doc, clause) for clause in want):
                    return False
                continue
            got = doc.get(key)
            if isinstance(want, dict):
                if '$exists' in want and (key in doc) != want['$exists']:
                    return False
                if '$in' in want and got not in want['$in']:
                    return False
                if '$ne' in want and got == want['$ne']:
                    return False
                unsupported = set(want) - {'$exists', '$in', '$ne'}
                if unsupported:
                    raise NotImplementedError(sorted(unsupported))
            elif got != want:
                return False
        return True

    def find(self, query=None, projection=None):
        return [dict(d) for d in self.docs if self._match(d, query or {})]

    def find_one(self, query=None, projection=None):
        found = self.find(query, projection)
        return found[0] if found else None


def project(ordinal=1, chain=None, **extra):
    doc = {'_id': ObjectId(), 'project_name': 'P', 'version_ordinal': ordinal,
           'version_chain_id': chain if chain is not None else ObjectId(),
           'date': '2025-01-02T03:04:05.000000', 'creator': 'someone@example.org'}
    doc.update(extra)
    return doc


# --------------------------------------------------------------------------
# The live writer resolves the chain id at write time
# --------------------------------------------------------------------------

def test_the_live_writer_resolves_the_chain_id_from_the_project(monkeypatch):
    from caper import views

    proj = project()
    monkeypatch.setattr(views, 'collection_handle', FakeCollection([proj]))
    assert views._audit_chain_id(str(proj['_id'])) == str(proj['version_chain_id'])


def test_the_live_writer_accepts_a_linkid_too(monkeypatch):
    from caper import views

    proj = project(linkid='link-9')
    monkeypatch.setattr(views, 'collection_handle', FakeCollection([proj]))
    assert views._audit_chain_id('link-9') == str(proj['version_chain_id'])


def test_a_placeholder_with_no_chain_yet_gets_none_rather_than_a_guess(monkeypatch):
    """The upload placeholder is written before link_new_version() runs."""
    from caper import views

    proj = project()
    del proj['version_chain_id']
    monkeypatch.setattr(views, 'collection_handle', FakeCollection([proj]))
    assert views._audit_chain_id(str(proj['_id'])) is None


def test_a_lookup_failure_does_not_break_the_audit_write(monkeypatch):
    """The log is evidence, never authority: it must not raise into an upload."""
    from caper import views

    class Exploding:
        def find_one(self, *_a, **_k):
            raise RuntimeError('connection lost')

    monkeypatch.setattr(views, 'collection_handle', Exploding())
    assert views._audit_chain_id(str(ObjectId())) is None
