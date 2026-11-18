"""The CNVkit coverage audit on the admin menu.

``summarize`` is tested on hand-built documents; the pipeline is tested against
a scratch collection in the real MongoDB, because what it has to get right is
what the database hands back from $objectToArray and a $map over each sample's
rows -- a pure-Python test of summarize() would say nothing about that.
"""

import pytest
from bson import ObjectId

from caper import cnvkit_audit, views
from caper.project_status import LIVE, SUPERSEDED, status_flags


def _row(cnvkit_dir='Not Provided', cnv_bed='Not Provided', *, spaced=False):
    if spaced:
        return {'cnvkit directory': cnvkit_dir, 'CNV BED file': cnv_bed}
    return {'cnvkit_directory': cnvkit_dir, 'CNV_BED_file': cnv_bed}


def _project(name, private, runs, **status):
    doc = {'_id': ObjectId(), 'project_name': name, 'private': private, 'runs': runs}
    doc.update(status or status_flags(LIVE))
    return doc


def _documents():
    oid = ObjectId
    return [
        # public: one sample complete, one with a user-supplied CNV BED, one with neither
        _project('pub-mixed', 'public', {
            'sample_1': [_row(oid(), oid()), _row(oid(), oid())],
            'sample_2': [_row('Not Provided', oid())],
            'sample_3': [_row()],
        }),
        # legacy boolean False is public; complete, in the space-separated spelling
        _project('pub-legacy', False, {
            'sample_1': [_row('samples/x/x_cnvkit_output.tar.gz', oid(), spaced=True)],
        }),
        # unlisted: every sample missing everything
        _project('unlisted-bare', 'hidden_public', {
            'sample_1': [_row()],
            'sample_2': [_row('', None)],
        }),
        # private: one sample whose rows disagree -- any row with the file counts
        _project('priv-disagree', 'private', {
            'sample_1': [_row(), _row(oid(), oid())],
            'sample_2': [{}],
        }),
        # a sample with no rows is not judged
        _project('priv-empty-sample', 'private', {'sample_1': []}),
        # not live: must not be counted at all
        _project('superseded', 'public', {'sample_1': [_row()]},
                 **status_flags(SUPERSEDED)),
    ]


@pytest.fixture
def scratch_collection():
    from caper.utils import db_handle_primary
    name = 'test_cnvkit_audit_scratch'
    coll = db_handle_primary[name]
    coll.drop()
    coll.insert_many(_documents())
    yield coll
    coll.drop()


def _rows_by_key(report):
    return {row['key']: row for row in report['rows']}


def test_the_breakdown_by_visibility(scratch_collection):
    report = cnvkit_audit.build_report(scratch_collection)
    rows = _rows_by_key(report)

    pub = rows['public']
    assert pub['projects'] == 2
    assert pub['samples'] == 4
    assert pub['cnvkit_dir_samples'] == 2      # sample_2, sample_3 of pub-mixed
    assert pub['cnv_bed_samples'] == 1         # sample_3
    assert pub['neither_samples'] == 1
    assert pub['cnvkit_dir_projects'] == 1
    assert pub['cnvkit_dir_all_projects'] == 0

    unlisted = rows['hidden_public']
    assert unlisted['projects'] == 1
    assert unlisted['samples'] == 2
    assert unlisted['cnvkit_dir_samples'] == 2
    assert unlisted['cnv_bed_samples'] == 2
    assert unlisted['cnvkit_dir_all_projects'] == 1
    assert unlisted['cnv_bed_all_projects'] == 1

    priv = rows['private']
    assert priv['projects'] == 2
    assert priv['samples'] == 2                # the row-less sample is not judged
    assert priv['cnvkit_dir_samples'] == 1     # only sample_2 ({}), not sample_1
    assert priv['cnv_bed_samples'] == 1

    total = rows['total']
    assert total['projects'] == 5              # the superseded document is left out
    assert total['samples'] == 8
    assert total['cnvkit_dir_samples'] == 5


def test_the_project_list_names_only_affected_projects(scratch_collection):
    report = cnvkit_audit.build_report(scratch_collection)
    names = [p['project_name'] for p in report['projects']]
    assert sorted(names) == ['priv-disagree', 'pub-mixed', 'unlisted-bare']
    by_name = {p['project_name']: p for p in report['projects']}
    assert by_name['pub-mixed']['missing_cnvkit_dir'] == 2
    assert by_name['pub-mixed']['missing_cnv_bed'] == 1
    assert by_name['pub-mixed']['missing_both'] == 1
    assert by_name['unlisted-bare']['visibility_display'] == 'Public, unlisted'


@pytest.mark.parametrize('value, present', [
    (None, False), ('', False), ('Not Provided', False), ('  not provided ', False),
    ('NA', False), (ObjectId(), True), ('samples/a/a_cnvkit_output.tar.gz', True),
])
def test_what_counts_as_present(value, present):
    assert cnvkit_audit._present(value) is present


def test_the_page_is_staff_only(request_factory, non_member_user):
    request = request_factory.get('/admin-cnvkit-audit/', HTTP_HOST='localhost')
    request.user = non_member_user
    response = views.admin_cnvkit_audit(request)
    assert response.status_code in (301, 302)
    assert '/notfound/' in response['Location']


def test_the_page_renders_for_staff(request_factory, admin_user):
    request = request_factory.get('/admin-cnvkit-audit/', HTTP_HOST='localhost')
    request.user = admin_user
    response = views.admin_cnvkit_audit(request)
    assert response.status_code == 200
    body = response.content.decode()
    assert 'CNVkit Coverage' in body
    assert 'Public, unlisted' in body
    assert 'All live projects' in body


def test_the_admin_menu_links_the_page():
    from pathlib import Path
    header = (Path(__file__).parents[1] / 'caper' / 'templates' / 'includes' /
              'header.html').read_text()
    assert 'href="/admin-cnvkit-audit/"' in header
