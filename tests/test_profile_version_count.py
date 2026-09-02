"""The projects table says how many versions each project has.

Counted from the lineage chain in one query for the whole page, not one query
per row -- the read amplification this codebase has had to go and fix twice.
"""

import os

from bson import ObjectId
from django.conf import settings

from caper import lineage


class _FakeCollection:
    def __init__(self, members):
        self.members = members
        self.queries = 0

    def find(self, query, projection=None):
        self.queries += 1
        wanted = query['version_chain_id']['$in']
        return [m for m in self.members if m['version_chain_id'] in wanted]


def test_one_query_covers_every_project_on_the_page():
    chain_a, chain_b = ObjectId(), ObjectId()
    members = (
        [{'_id': ObjectId(), 'version_chain_id': chain_a, 'version_ordinal': i}
         for i in range(3)] +
        [{'_id': ObjectId(), 'version_chain_id': chain_b, 'version_ordinal': 0}]
    )
    collection = _FakeCollection(members)
    projects = [{'version_chain_id': chain_a}, {'version_chain_id': chain_b},
                {'version_chain_id': chain_a}]

    chains = lineage.chains_for(collection, projects, lineage.POINTER_PROJECTION)

    assert collection.queries == 1
    assert len(chains[chain_a]) == 3
    assert len(chains[chain_b]) == 1


def test_a_single_version_project_counts_itself():
    """One version reads 1, not 0. The current version is a version."""
    chain = ObjectId()
    collection = _FakeCollection(
        [{'_id': ObjectId(), 'version_chain_id': chain, 'version_ordinal': 0}])

    chains = lineage.chains_for(collection, [{'version_chain_id': chain}],
                                lineage.POINTER_PROJECTION)

    assert len(chains[chain]) == 1


def test_an_unpointered_project_has_no_count_rather_than_zero():
    """Zero versions would be a lie; the history simply cannot be read."""
    collection = _FakeCollection([])
    chains = lineage.chains_for(collection, [{'project_name': 'no pointers'}],
                                lineage.POINTER_PROJECTION)
    assert chains == {}
    assert chains.get(None) is None


def _profile_template():
    path = os.path.join(settings.BASE_DIR, 'templates', 'pages', 'profile.html')
    with open(path) as handle:
        return handle.read()


def test_the_table_renders_the_count():
    markup = _profile_template()
    assert '{{ project.version_count }}' in markup
    assert '>Versions</th>' in markup


def test_the_header_and_the_body_have_the_same_number_of_columns():
    """The guard the first version of this file did not have.

    Inserting the Versions column dropped the sample-count cell: ten headers
    against nine cells, so every column after Samples rendered one to the left
    of its heading and the table read as nonsense.  Checking that a string is
    present says nothing about whether the row still lines up.
    """
    import re

    markup = _profile_template()
    head = markup.split('<thead>')[1].split('</thead>')[0]
    body = markup.split('<tbody>')[1].split('</tbody>')[0]

    headers = len(re.findall(r'<th\b', head))
    cells = len(re.findall(r'<td\b', body))

    assert headers == cells, (
        f'{headers} column headings against {cells} cells: the body row does '
        f'not line up with the header')


def test_the_sample_count_still_has_a_cell():
    """It is the column that went missing, so it gets its own assertion."""
    markup = _profile_template()
    assert '<td>{{ project.sample_count }}</td>' in markup


def test_the_date_column_index_followed_the_new_column():
    """Adding a column at index 2 moved Date from 3 to 4.

    The custom-date sorter is bound by index, so leaving it on 3 would have
    silently applied a date parser to the version numbers and sorted the table
    on the wrong column.
    """
    markup = _profile_template()
    assert 'targets: 4,' in markup and 'custom-date' in markup
    assert "order: [[4, 'desc']]" in markup
    assert "order: [[3, 'desc']]" not in markup
