"""The manifest of URLs that resolve, which is what makes a rebuild recoverable.

The property under test is that the identifiers come from the resolver's own
query definitions rather than a list written beside them. A URL list maintained
separately from the resolver fails in the one direction that matters: silently,
and only once somebody needs it.
"""

from bson import ObjectId

from caper import url_manifest
from caper.project_status import resolver_queries


def test_the_identifier_fields_come_from_the_resolvers_own_queries():
    fields = url_manifest.resolver_identifier_fields()
    assert fields == {'_id', 'alias_name', 'project_name'}

    # If get_one_project() grows a lookup on another field, this is what says
    # so -- the manifest would otherwise drop those URLs without a word.
    queried = set()
    for _line, query in resolver_queries(project_id='0' * 24, project_name='x'):
        queried.update(key for key in query if not key.startswith('$'))
    assert fields <= queried


class _Collection:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query=None, projection=None):
        for doc in self.docs:
            yield dict(doc)


def test_every_identifier_a_project_answers_to_gets_a_row():
    pid = ObjectId()
    chain = ObjectId()
    collection = _Collection([{
        '_id': pid, 'linkid': 'abc123', 'project_name': 'PCAWG',
        'alias_name': 'pcawg-2026', 'delete': False, 'current': True,
        'private': 'public', 'version_chain_id': chain, 'version_ordinal': 2,
        'is_latest': True,
    }])

    rows = url_manifest.rows(collection)

    assert {row['identifier'] for row in rows} == {
        'abc123', str(pid), 'PCAWG', 'pcawg-2026'}
    assert {row['url'] for row in rows} == {
        '/project/abc123', '/project/%s' % pid,
        '/project/PCAWG', '/project/pcawg-2026'}
    assert all(row['status'] == 'LIVE' for row in rows)


def test_a_tombstone_records_where_it_redirects():
    """After a rebuild that target is a new id, so the redirect has to be
    re-pointed rather than simply recreated."""
    tomb, target = ObjectId(), ObjectId()
    collection = _Collection([{
        '_id': tomb, 'project_name': 'gone', 'delete': True, 'current': False,
        # Both markers: classify() reads them as a pair.
        'version_deleted_from_history': True, 'payload_purged': True,
        'redirect_to_project': str(target),
        'previous_versions': [{'linkid': str(ObjectId())}],
    }])

    rows = url_manifest.rows(collection)

    assert rows
    assert all(row['redirect_to_project'] == str(target) for row in rows)
    assert any(row['identifier_kind'] == 'tombstone_history' for row in rows)


def test_the_csv_has_a_header_and_one_line_per_url():
    collection = _Collection([
        {'_id': ObjectId(), 'project_name': 'a', 'delete': False,
         'current': True, 'private': 'public'},
        {'_id': ObjectId(), 'project_name': 'b', 'delete': False,
         'current': True, 'private': 'public'},
    ])

    text = url_manifest.as_csv(collection)
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0] == ','.join(url_manifest.COLUMNS)
    assert len(lines) - 1 == len(url_manifest.rows(collection))
