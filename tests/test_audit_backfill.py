"""The two backfills that fill in an audit log started two years too late.

Both scripts write into the audit collection, and the thing worth testing is
not that they write but *what they are willing to assert*. One is a join and may
assert freely; the other is a reconstruction and must mark everything it says.

The properties held here:

1. Neither script writes anything without ``--execute``.
2. Both are idempotent -- a second run finds nothing, so a partial run can
   always be finished by running again.
3. The chain-id stamp never guesses: an event whose project is gone gets
   nothing.
4. Every reconstructed event is flagged, and carries no field that was not read
   off the project document.
"""
import datetime
import importlib.util
import os
import sys

import pytest
from bson import ObjectId

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, f'{name}.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill_chain_ids = _load('backfill_audit_chain_ids')
backfill_create = _load('backfill_create_events')


class FakeCollection:
    """Enough of a collection for these two scripts and no more."""

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

    def count_documents(self, query):
        return len(self.find(query))

    def update_one(self, query, update):
        for doc in self.docs:
            if self._match(doc, query):
                doc.update(update['$set'])
                return type('R', (), {'modified_count': 1})()
        return type('R', (), {'modified_count': 0})()

    def insert_many(self, entries):
        ids = []
        for entry in entries:
            entry = dict(entry)
            entry.setdefault('_id', ObjectId())
            self.docs.append(entry)
            ids.append(entry['_id'])
        return type('R', (), {'inserted_ids': ids})()


def project(ordinal=1, chain=None, **extra):
    doc = {'_id': ObjectId(), 'project_name': 'P', 'version_ordinal': ordinal,
           'version_chain_id': chain if chain is not None else ObjectId(),
           'date': '2025-01-02T03:04:05.000000', 'creator': 'someone@example.org'}
    doc.update(extra)
    return doc


# --------------------------------------------------------------------------
# backfill_audit_chain_ids: a join, and only a join
# --------------------------------------------------------------------------

def test_stamps_the_chain_id_the_project_actually_has():
    proj = project()
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(proj['_id']),
                             'event_type': 'create'}])
    resolvable, unresolvable = backfill_chain_ids.plan(audit, FakeCollection([proj]))
    assert unresolvable == []
    assert [chain for _event, chain in resolvable] == [proj['version_chain_id']]


def test_an_event_whose_project_is_gone_is_left_alone():
    """The honest answer for a permanently deleted project is no answer."""
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(ObjectId()),
                             'event_type': 'create'}])
    resolvable, unresolvable = backfill_chain_ids.plan(audit, FakeCollection([]))
    assert resolvable == []
    assert len(unresolvable) == 1


def test_the_caller_that_passes_linkid_still_resolves():
    """One of the eight call sites logs `linkid` where the others log `_id`."""
    proj = project(linkid='link-1234')
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': 'link-1234',
                             'event_type': 'edit_no_version'}])
    resolvable, _ = backfill_chain_ids.plan(audit, FakeCollection([proj]))
    assert [chain for _e, chain in resolvable] == [proj['version_chain_id']]


def test_events_that_already_have_a_chain_id_are_not_reconsidered():
    proj = project()
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(proj['_id']),
                             'event_type': 'create', 'version_chain_id': 'x'}])
    resolvable, unresolvable = backfill_chain_ids.plan(audit, FakeCollection([proj]))
    assert (resolvable, unresolvable) == ([], [])


# --------------------------------------------------------------------------
# backfill_create_events: a reconstruction, and it says so
# --------------------------------------------------------------------------

def test_every_reconstructed_event_is_flagged():
    entries, skipped = backfill_create.plan(FakeCollection([]),
                                            FakeCollection([project()]))
    assert skipped == []
    assert len(entries) == 1
    assert entries[0]['backfilled'] is True
    assert entries[0]['backfill_basis'] == backfill_create.BASIS


def test_the_timestamp_and_actor_come_off_the_document():
    proj = project(date='2024-07-08T09:10:11.000000', creator='pat@example.org')
    entries, _ = backfill_create.plan(FakeCollection([]), FakeCollection([proj]))
    assert entries[0]['timestamp'] == datetime.datetime(2024, 7, 8, 9, 10, 11)
    assert entries[0]['user_email'] == 'pat@example.org'


def test_nothing_that_was_never_recorded_is_invented():
    """No s3 uri, no file size, no before/after -- those are gaps, not guesses."""
    entries, _ = backfill_create.plan(FakeCollection([]),
                                      FakeCollection([project()]))
    for absent in ('s3_uri', 's3_file_size_bytes', 'before', 'after',
                   'intended', 'completed'):
        assert absent not in entries[0], absent


def test_tool_versions_are_copied_only_when_the_document_has_them():
    with_versions = project(AA_version='1.3.r5', sample_count=12)
    entries, _ = backfill_create.plan(FakeCollection([]),
                                      FakeCollection([with_versions]))
    assert entries[0]['AA_version'] == '1.3.r5'
    assert entries[0]['sample_count'] == 12

    entries, _ = backfill_create.plan(FakeCollection([]),
                                      FakeCollection([project()]))
    assert 'AA_version' not in entries[0]
    assert 'sample_count' not in entries[0]


def test_ordinal_decides_create_versus_edit():
    entries, _ = backfill_create.plan(
        FakeCollection([]), FakeCollection([project(ordinal=1),
                                            project(ordinal=4)]))
    assert sorted(e['event_type'] for e in entries) == ['create',
                                                        'edit_new_version']


def test_a_document_with_no_ordinal_is_treated_as_a_first_version():
    proj = project()
    del proj['version_ordinal']
    entries, _ = backfill_create.plan(FakeCollection([]), FakeCollection([proj]))
    assert entries[0]['event_type'] == 'create'


def test_an_unusable_date_is_skipped_rather_than_guessed():
    entries, skipped = backfill_create.plan(
        FakeCollection([]), FakeCollection([project(date='last tuesday')]))
    assert entries == []
    assert len(skipped) == 1 and skipped[0][1] == 'no usable date'


def test_a_datetime_date_works_as_well_as_a_string():
    """Prod stores ISO strings; dev holds a mix."""
    when = datetime.datetime(2023, 3, 4, 5, 6, 7)
    entries, _ = backfill_create.plan(FakeCollection([]),
                                      FakeCollection([project(date=when)]))
    assert entries[0]['timestamp'] == when


def test_a_missing_creator_is_unknown_and_not_omitted():
    proj = project()
    del proj['creator']
    entries, _ = backfill_create.plan(FakeCollection([]), FakeCollection([proj]))
    assert entries[0]['user_email'] == 'unknown'


def test_a_document_that_already_has_a_creation_event_is_skipped():
    proj = project()
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(proj['_id']),
                             'event_type': 'create'}])
    entries, _ = backfill_create.plan(audit, FakeCollection([proj]))
    assert entries == []


def test_an_edit_no_version_event_does_not_count_as_a_creation():
    """A project only ever edited in place still never got a create event."""
    proj = project()
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(proj['_id']),
                             'event_type': 'edit_no_version'}])
    entries, _ = backfill_create.plan(audit, FakeCollection([proj]))
    assert len(entries) == 1


# --------------------------------------------------------------------------
# Both: report-only by default, and idempotent
# --------------------------------------------------------------------------

@pytest.fixture
def target(monkeypatch):
    """A database both scripts will accept, wired to fake collections."""
    proj = project()
    audit = FakeCollection([{'_id': ObjectId(), 'project_uuid': str(proj['_id']),
                             'event_type': 'edit_no_version'}])
    projects = FakeCollection([proj])
    database = {'project_audit_log': audit, 'projects': projects}
    monkeypatch.setenv('DB_NAME', 'caper-dev')
    for module in (backfill_chain_ids, backfill_create):
        monkeypatch.setattr(module, 'connect', lambda *_a, **_k: database)
    return database, audit, projects


ARGS = ['--expect-db', 'caper-dev', '--expect-host', 'local']


def test_neither_script_writes_without_execute(target, capsys):
    _database, audit, _projects = target
    before = len(audit.docs)
    assert backfill_chain_ids.main(ARGS) == 0
    assert backfill_create.main(ARGS) == 0
    assert len(audit.docs) == before
    assert 'version_chain_id' not in audit.docs[0]


def test_both_scripts_are_idempotent(target, capsys):
    _database, audit, _projects = target
    backfill_chain_ids.main(ARGS + ['--execute'])
    backfill_create.main(ARGS + ['--execute'])
    after_first = [dict(d) for d in audit.docs]

    backfill_chain_ids.main(ARGS + ['--execute'])
    backfill_create.main(ARGS + ['--execute'])
    assert audit.docs == after_first


def test_a_wrong_expect_db_stops_before_connecting(target):
    with pytest.raises(SystemExit):
        backfill_create.main(['--expect-db', 'caper', '--expect-host', 'docdb'])


# --------------------------------------------------------------------------
# The live writer keeps the field the backfill adds
# --------------------------------------------------------------------------
# Without this, the backfill would be undone by the first upload after it:
# every new event would arrive without a chain id and the gap would reopen.

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
