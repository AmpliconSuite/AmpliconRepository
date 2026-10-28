"""
The home page's Projects scope answers with projects, not feature rows.

It posts ``scope=project_name`` along with ``project_name``; ``search_results``
then lists the matching projects -- name, description, sample count,
visibility and a download link -- and never collects a sample. The advanced
form posts no scope, and still gets the sample search it always did.
"""

import uuid

import pytest


TAG = 'PSearch' + uuid.uuid4().hex[:8]


def _project(name, private, members, sample_names):
    return {
        'project_name': name,
        'description': f'{name} description',
        'date': '2026-10-28T12:00:00',
        'creator': members[0] if members else 'nobody',
        'project_members': members,
        'private': private,
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'sample_count': len(sample_names),
        'runs': {s: [{'Sample_name': s, 'Feature_ID': f'{s}_amplicon1',
                      'Classification': 'ecDNA', 'All_genes': ['MYC'],
                      'Oncogenes': ['MYC']}] for s in sample_names},
        'sample_data': [{'Sample_name': s, 'Features': 1} for s in sample_names],
    }


@pytest.fixture
def projects(mongo_collection, test_user):
    docs = {
        'public': _project(f'{TAG}_Public', 'public', ['someone_else'], ['A1', 'A2', 'A3']),
        'private': _project(f'{TAG}_Private', 'private', [test_user.username], ['B1']),
        'unlisted': _project(f'{TAG}_Unlisted', 'hidden_public', [test_user.email], ['C1', 'C2']),
        'other_private': _project(f'{TAG}_NotYours', 'private', ['someone_else'], ['D1']),
    }
    ids = {key: mongo_collection.insert_one(doc).inserted_id for key, doc in docs.items()}
    yield ids
    mongo_collection.delete_many({'_id': {'$in': list(ids.values())}})


def _search(request_factory, user, data):
    from caper.views import search_results
    req = request_factory.post('/search_results/', data)
    req.user = user
    resp = search_results(req)
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.mark.integration
def test_projects_scope_lists_projects_not_samples(request_factory, test_user, projects):
    html = _search(request_factory, test_user, {'scope': 'project_name', 'project_name': TAG})

    assert 'id="projectSearchTable"' in html
    assert 'public-samples-table' not in html
    assert 'A1_amplicon1' not in html

    for key in ('public', 'private', 'unlisted'):
        assert f'/project/{projects[key]}"' in html
        assert f'/project/{projects[key]}/download"' in html
    assert f'{TAG}_NotYours' not in html

    assert 'Private</span>' in html
    assert 'Unlisted</span>' in html


@pytest.mark.integration
def test_anonymous_sees_only_public(request_factory, projects):
    from django.contrib.auth.models import AnonymousUser
    html = _search(request_factory, AnonymousUser(), {'scope': 'project_name', 'project_name': TAG})

    assert f'{TAG}_Public' in html
    for key in ('private', 'unlisted', 'other_private'):
        assert str(projects[key]) not in html


@pytest.mark.integration
def test_no_match_says_so(request_factory, test_user, projects):
    html = _search(request_factory, test_user,
                   {'scope': 'project_name', 'project_name': TAG + '_nothing_has_this'})
    assert 'id="projectSearchTable"' not in html
    assert 'No projects you can see match' in html


@pytest.mark.integration
def test_without_scope_still_searches_samples(request_factory, test_user, projects):
    """The advanced form, and the "Search the samples" button, post no scope."""
    html = _search(request_factory, test_user, {'project_name': TAG})
    assert 'id="projectSearchTable"' not in html
    assert 'public-samples-table' in html


@pytest.mark.integration
def test_sample_rows_carry_no_whitespace_between_tags(request_factory, test_user, projects):
    """The indentation between row tags was most of a big result page.

    Measured on dev 2026-10-28, a search returning 19,634 feature rows carried
    7.8 MB of it inside the samples table, and building the table took about
    98 s in Chromium with it against about 16 s without.
    """
    import re
    html = _search(request_factory, test_user, {'project_name': TAG})
    start = html.index('<tbody', html.index('id="public-samples-table"'))
    body = html[start:html.index('</tbody>', start)]
    assert body.count('<tr') == 3   # the public project's samples; the rest are on the private tab
    assert not re.search(r'>\s+<', body.split('>', 1)[1])


@pytest.mark.integration
def test_find_projects_never_reads_the_payload(test_user, projects):
    from caper.search import find_projects_by_name

    found = {str(p['_id']): p for p in find_projects_by_name(TAG, test_user)}
    assert set(found) == {str(projects[k]) for k in ('public', 'private', 'unlisted')}

    for proj in found.values():
        assert 'runs' not in proj
        assert 'sample_data' not in proj

    counts = {k: found[str(projects[k])]['sample_count_display']
              for k in ('public', 'private', 'unlisted')}
    assert counts == {'public': 3, 'private': 1, 'unlisted': 2}
    assert [p['project_name'] for p in found.values()] == sorted(
        p['project_name'] for p in found.values())


@pytest.mark.integration
def test_count_falls_back_to_stored_count(mongo_collection, test_user):
    """A document without sample_data still reports a count."""
    from caper.search import find_projects_by_name

    doc = _project(f'{TAG}_NoSampleData', 'public', ['someone_else'], ['E1', 'E2'])
    del doc['sample_data']
    inserted = mongo_collection.insert_one(doc).inserted_id
    try:
        found = find_projects_by_name(f'{TAG}_NoSampleData', test_user)
        assert [p['sample_count_display'] for p in found] == [2]
    finally:
        mongo_collection.delete_one({'_id': inserted})
