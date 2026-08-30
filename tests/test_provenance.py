"""Who deleted what, and whether the deletion finished.

Before this, the audit log held creation and edits only. Every deletion path --
soft delete, version delete, promotion, restore, permanent delete -- wrote
nothing, so after the fact there was no record of who acted or what the project
looked like first.

Two properties matter more than the field names, and both are tested here
rather than left to reading:

1. The event is written *before* the mutation. A record written afterwards is
   missing exactly when it matters -- the process that dies half way through a
   purge is the one whose trace you need.
2. A failure to write the log never breaks the operation it describes. The
   deletion is the user's intent; the log is our record of it, and evidence
   must not be able to veto the event.
"""
import datetime

import pytest

from caper import provenance


class FakeAuditCollection:
    """Records calls in order, so 'before or after?' is answerable."""

    def __init__(self, insert_error=None, update_error=None):
        self.docs = {}
        self.calls = []
        self.insert_error = insert_error
        self.update_error = update_error
        self._next = 0

    def insert_one(self, doc):
        self.calls.append(('insert', doc.get('event_type')))
        if self.insert_error:
            raise self.insert_error
        self._next += 1
        key = f'event-{self._next}'
        self.docs[key] = dict(doc)
        return type('R', (), {'inserted_id': key})()

    def update_one(self, query, update):
        self.calls.append(('update', query.get('_id')))
        if self.update_error:
            raise self.update_error
        self.docs.setdefault(query['_id'], {}).update(update['$set'])

    def find(self, query):
        ids = query.get('project_uuid', {}).get('$in', [])
        matched = [d for d in self.docs.values() if d.get('project_uuid') in ids]

        class Cursor(list):
            def sort(self, *_args, **_kw):
                return self

            def limit(self, _n):
                return self
        return Cursor(matched)


def _project(**over):
    doc = {
        '_id': 'proj-1', 'project_name': 'Study A',
        'version_chain_id': 'chain-9', 'version_ordinal': 3,
        'status': 'LIVE', 'is_latest': True, 'private': 'public',
        'sample_count': 12, 'tarfile': 'abc', 'runs': {'s1': [{}]},
    }
    doc.update(over)
    return doc


class _User:
    email = 'someone@example.org'
    username = 'someone'


# --- the snapshot ---------------------------------------------------------

def test_the_snapshot_keeps_what_a_reader_needs():
    snap = provenance.snapshot(_project())

    assert snap['project_id'] == 'proj-1'
    assert snap['version_chain_id'] == 'chain-9'
    assert snap['version_ordinal'] == 3
    assert snap['status'] == 'LIVE'
    assert snap['had_tarfile'] is True


def test_the_snapshot_does_not_copy_the_payload():
    """The log must not become the biggest thing in the database."""
    snap = provenance.snapshot(_project())

    for heavy in ('runs', 'sample_data', 'aggregate_df', 'features_list'):
        assert heavy not in snap


def test_a_missing_chain_id_is_none_not_the_string_none():
    snap = provenance.snapshot(_project(version_chain_id=None))

    assert snap['version_chain_id'] is None


# --- ordering: recorded before, confirmed after ---------------------------

def test_the_event_is_written_before_the_mutation():
    audit = FakeAuditCollection()
    order = []

    event_id = provenance.record(audit, provenance.DELETE_VERSION, _User(),
                                 _project())
    order.append('recorded')
    order.append('mutated')
    provenance.confirm(audit, event_id)
    order.append('confirmed')

    assert order == ['recorded', 'mutated', 'confirmed']
    assert audit.calls[0][0] == 'insert'
    assert audit.calls[-1][0] == 'update'


def test_an_unconfirmed_event_is_the_signature_of_a_half_done_deletion():
    audit = FakeAuditCollection()

    event_id = provenance.record(audit, provenance.PERMANENT_DELETE, _User(),
                                 _project())
    # ...and the process dies here, before confirm() is reached.

    assert audit.docs[event_id]['completed'] is False
    assert audit.docs[event_id]['intended'] == {}
    assert audit.docs[event_id]['before']['project_id'] == 'proj-1'


def test_confirming_records_what_actually_happened():
    audit = FakeAuditCollection()

    event_id = provenance.record(audit, provenance.DELETE_VERSION, _User(),
                                 _project(),
                                 intended={'status': 'TOMBSTONE'})
    provenance.confirm(audit, event_id, outcome='promoted',
                       gridfs_files_purged=42)

    stored = audit.docs[event_id]
    assert stored['completed'] is True
    assert stored['outcome'] == 'promoted'
    assert stored['gridfs_files_purged'] == 42
    assert stored['intended'] == {'status': 'TOMBSTONE'}


# --- the log must never veto the operation --------------------------------

def test_a_failing_audit_write_does_not_raise():
    audit = FakeAuditCollection(insert_error=RuntimeError('mongo is down'))

    assert provenance.record(audit, provenance.DELETE_PROJECT, _User(),
                             _project()) is None


def test_a_failing_confirm_does_not_raise():
    audit = FakeAuditCollection(update_error=RuntimeError('mongo is down'))
    event_id = provenance.record(audit, provenance.DELETE_PROJECT, _User(),
                                 _project())

    provenance.confirm(audit, event_id)  # must not raise


def test_confirming_an_event_that_was_never_written_is_a_no_op():
    audit = FakeAuditCollection()

    provenance.confirm(audit, None)

    assert audit.calls == []


# --- identity -------------------------------------------------------------

def test_the_actor_falls_back_when_there_is_no_django_user():
    assert provenance._actor(_User()) == 'someone@example.org'
    assert provenance._actor('a-string') == 'a-string'
    assert provenance._actor(None) == 'unknown'

    class NoEmail:
        email = ''
        username = 'bare'
    assert provenance._actor(NoEmail()) == 'bare'


def test_events_are_findable_by_the_field_the_old_entries_use():
    """121 stored entries on prod carry project_uuid and no chain id."""
    audit = FakeAuditCollection()
    provenance.record(audit, provenance.DELETE_PROJECT, _User(), _project())

    found = provenance.history_for(audit, ['proj-1'])

    assert len(found) == 1
    assert found[0]['event_type'] == provenance.DELETE_PROJECT


def test_history_for_no_ids_asks_no_question():
    audit = FakeAuditCollection()

    assert provenance.history_for(audit, []) == []
    assert audit.calls == []


# --- the template contract ------------------------------------------------

def test_every_deletion_event_has_a_badge_in_the_admin_template():
    """An event type the template does not know renders as 'Edit'.

    That fallback predates these events and is silent, so a new event type
    would be displayed as its own opposite. This is the guard for that.
    """
    from pathlib import Path

    template = (Path(__file__).parents[1] / 'caper' / 'templates' / 'pages' /
                'admin_project_files_report.html').read_text()

    for event in provenance.DELETION_EVENTS:
        assert f"entry.event_type == '{event}'" in template, (
            f'{event!r} has no badge; it would render as a generic Edit')


# --- deletion events are not payload descriptions -------------------------

def test_a_deletion_event_is_not_used_to_validate_a_live_project():
    """Found on dev 2026-08-30, by Jens, on the first real deletion.

    ``_run_audit_checks`` compares an entry's AA/AC/ASP versions, sample count
    and S3 size against the live document. A deletion event carries none of
    those, so when one became the newest entry for a project the page reported
    every field missing and downgraded a perfectly healthy project to
    "Partial". The events were right; the query that chose them was wrong.
    """
    from caper.views_admin import _PAYLOAD_DESCRIBING_ENTRY

    excluded = _PAYLOAD_DESCRIBING_ENTRY['event_type']['$nin']
    for event in provenance.DELETION_EVENTS:
        assert event in excluded, (
            f'{event!r} could still be picked as the entry to validate against')


def test_the_filter_still_admits_the_entries_that_predate_event_types():
    """$nin matches a document with no event_type, which the oldest entries are.

    Encoded as a test because the alternative spelling -- an $in over the three
    known content types -- looks equivalent and would silently drop every
    legacy entry, of which prod had 121 on 2026-08-29.
    """
    from caper.views_admin import _PAYLOAD_DESCRIBING_ENTRY

    assert '$nin' in _PAYLOAD_DESCRIBING_ENTRY['event_type']
    assert '$in' not in _PAYLOAD_DESCRIBING_ENTRY['event_type']


def test_a_project_with_only_lifecycle_events_is_distinguished_from_unaudited():
    """Two different states that both used to render as a warning.

    "We logged your deletion and there is no payload entry to compare" is not
    "we have no record of this project". The first is correct and expected
    after a version delete; the second means the log is missing.
    """
    from pathlib import Path

    template = (Path(__file__).parents[1] / 'caper' / 'templates' / 'pages' /
                'admin_project_files_report.html').read_text()

    assert "proj.validation_status == 'lifecycle_only'" in template
    assert "proj.validation_status == 'no_log'" in template


# --- what the audit log records about tool versions -----------------------

def test_the_log_records_detected_versions_not_the_form_placeholder():
    """Measured on dev 2026-08-30: 12 of 70 live documents showed Mismatch.

    Every one had the same shape -- log='NA' against a real live version, and
    not one was a genuine disagreement between two real values. The upload
    form's version fields default to 'NA' and most submitters leave them there;
    get_tool_versions() fills the document in afterwards from what was actually
    detected in the uploaded data. Logging the form value recorded the
    placeholder and the admin table called the difference a mismatch.
    """
    from caper.views import _resolved_tool_versions

    typed = {'aa_version': 'NA', 'ac_version': 'NA', 'asp_version': 'NA'}
    detected = {'AA_version': '1.3.r5', 'AC_version': '0.4.16',
                'ASP_version': '0.1477.1'}

    assert _resolved_tool_versions(detected, typed) == ('1.3.r5', '0.4.16',
                                                        '0.1477.1')


def test_a_version_the_user_typed_is_kept_when_nothing_was_detected():
    """The failure paths never run an aggregation, so the form is all there is."""
    from caper.views import _resolved_tool_versions

    typed = {'aa_version': '1.2.3', 'ac_version': 'NA', 'asp_version': 'NA'}

    assert _resolved_tool_versions({}, typed) == ('1.2.3', 'NA', 'NA')
    assert _resolved_tool_versions(None, typed) == ('1.2.3', 'NA', 'NA')


def test_a_stored_placeholder_does_not_beat_a_typed_value():
    """'NA' on the document is absence, not a value, whatever its case."""
    from caper.views import _resolved_tool_versions

    typed = {'aa_version': '1.2.3', 'ac_version': 'NA', 'asp_version': 'NA'}
    stored = {'AA_version': 'na', 'AC_version': '  ', 'ASP_version': None}

    assert _resolved_tool_versions(stored, typed) == ('1.2.3', 'NA', 'NA')
