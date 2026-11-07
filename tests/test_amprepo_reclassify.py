"""Tests for amprepo_reclassify, with a synthetic archive and a stubbed AC and aggregator.

CI has no Gurobi licence, so nothing here runs the real tools. Nothing here uses the database
either; the `integration` mark is because that's the one mark CI selects.
"""

import ast
import http.server
import io
import json
import os
import sys
import tarfile
import textwrap
import threading

import pytest

from amprepo_reclassify import archive, environment, pins, pipeline, plan, report
from amprepo_reclassify.archive import ProjectError
from amprepo_reclassify.machine import Machine
from amprepo_reclassify.site_api import Site, SiteError, USER_AGENT

pytestmark = pytest.mark.integration

PACKAGE_DIR = os.path.dirname(os.path.abspath(archive.__file__))
ID_A = 'a' * 24
ID_B = 'b' * 24
ID_OLD = 'c' * 24

STUB_AC = textwrap.dedent('''
    import argparse, os, sys
    p = argparse.ArgumentParser()
    p.add_argument('--ref'); p.add_argument('--AA_results'); p.add_argument('-o')
    args, _ = p.parse_known_args()
    with open(os.environ['STUB_CALLS'], 'a') as f:
        f.write(' '.join(sys.argv[1:]) + '\\n')
    print(os.environ.get('STUB_AC_SAYS', 'classifying'))
    if os.environ.get('STUB_AC_EXIT'):
        sys.exit(int(os.environ['STUB_AC_EXIT']))
    out = os.path.dirname(args.o)
    open(os.path.join(out, 'AC.input'), 'w').write(os.environ.get('STUB_AC_INPUT', 'S1 graph cycles\\n'))
    with open(args.o + '_result_table.tsv', 'w') as f:
        f.write('Sample name\\tAA amplicon number\\tFeature ID\\tClassification\\tAC version\\tOncogenes\\n')
        f.write('S1\\t1\\tS1_amplicon1_ecDNA_1\\tecDNA\\t2.0.1\\t[]\\n')
    open(args.o + '_amplicon_classification_profiles.tsv', 'w').write('header\\n')
''')

STUB_AGGREGATOR = textwrap.dedent('''
    import argparse, json, os, tarfile
    p = argparse.ArgumentParser()
    p.add_argument('--files', nargs='+'); p.add_argument('-o')
    args = p.parse_args()
    drop = (args.o.endswith('_reclass') and os.environ.get('STUB_ASA_DROP')) or '\\0'
    samples = set()
    with tarfile.open(args.o + '.tar.gz', 'w:gz') as tar:
        for root in args.files:
            for dirpath, _, names in os.walk(root):
                for name in names:
                    if name == 'run.json' or name.startswith(drop):
                        continue
                    if name.endswith('_CNV_CALLS.bed'):
                        samples.add(name.split('_CNV_CALLS')[0])
                    tar.add(os.path.join(dirpath, name), arcname='results/files/' + name)
        data = json.dumps({'runs': {'r': [{'Sample name': s} for s in sorted(samples)]}}).encode()
        info = tarfile.TarInfo('results/run.json'); info.size = len(data)
        import io
        tar.addfile(info, io.BytesIO(data))
''')


def _add(tar, name, text=''):
    data = text.encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def make_archive(path, ac_log='', manifest=None):
    """A modern archive: S1 has an amplicon, and S2 has only a CNV."""
    manifest = manifest or {'runs': {'run': [
        {'Sample name': 'S1', 'Reconstruction directory': 'samples/S1/S1_AA_results',
         'CNV BED file': 'samples/S1/S1_CNV_CALLS.bed'},
        {'Sample name': 'S2', 'Reconstruction directory': 'Not Provided',
         'CNV BED file': 'samples/S2/S2_CNV_CALLS.bed'}]}}
    with tarfile.open(path, 'w:gz') as tar:
        _add(tar, 'results/run.json', json.dumps(manifest))
        aa = 'results/samples/S1/S1_AA_results/'
        for name in ('S1_amplicon1_graph.txt', 'S1_amplicon1_cycles.txt', 'S1_amplicon1.png', 'S1_summary.txt'):
            _add(tar, aa + name, 'aa data for ' + name)
        _add(tar, 'results/samples/S1/S1_CNV_CALLS.bed', 'chr1 1 2 3\n')
        _add(tar, 'results/samples/S2/S2_CNV_CALLS.bed', 'chr1 4 5 6\n')
        ac = 'results/consolidated_classification/project'
        _add(tar, ac + '_amplicon_classification_profiles.tsv', 'header\n')
        _add(tar, ac + '_result_table.tsv',
             'Sample name\tAA amplicon number\tFeature ID\tClassification\tAC version\n'
             'S1\t1\tS1_amplicon1_linear_1\tLinear amplification\tNA\n'
             'S2\tNA\tS2_NA\tNA\tNA\n')
        if ac_log:
            _add(tar, ac + '.log', ac_log)
    return path


class FakeSite:
    """What pipeline and plan need of a Site."""
    name = 'dev'

    def __init__(self, source, heads=(ID_A, ID_B), sizes=None):
        self.source = source
        self.heads = heads
        self.sizes = sizes or {}
        self.downloads = 0

    def project_url(self, project_id):
        return 'https://example.test/project/' + project_id

    def list_projects(self):
        return [{'id': head, 'is_latest_version': True} for head in self.heads]

    def archive_size(self, project_id):
        return self.sizes.get(project_id, 1000)

    def download(self, project_id, dest):
        self.downloads += 1
        with open(self.source, 'rb') as source, open(dest, 'wb') as out:
            out.write(source.read())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A machine, stub tools and a one-project plan."""
    stub_ac, stub_asa = tmp_path / 'stub_ac.py', tmp_path / 'stub_asa.py'
    stub_ac.write_text(STUB_AC)
    stub_asa.write_text(STUB_AGGREGATOR)
    calls = tmp_path / 'ac_calls.txt'
    calls.write_text('')
    monkeypatch.setenv('STUB_CALLS', str(calls))

    class Workspace:
        machine = Machine(workdir=str(tmp_path / 'work'), jobs=2, bfb_threads=1)
        tools = environment.Tools(ac_cmd=[sys.executable, str(stub_ac)],
                                  asa_cmd=[sys.executable, str(stub_asa)],
                                  description='stub tools 1')
        site = FakeSite(make_archive(str(tmp_path / 'source.tar.gz')))
        ac_calls = calls

        @staticmethod
        def entry(project_id=ID_A, name='Project A', **extra):
            return dict({'id': project_id, 'name': name, 'status': plan.UPDATE, 'samples': 2,
                         'reference': 'GRCh38', 'ac_ref': 'GRCh38', 'current_ac': '2.0.0',
                         'archive_bytes': 1000, 'extra_ac_flags': ''}, **extra)

        def run(self, *entries, tools=None):
            target = {'site': 'dev', 'target_ac': '2.0.1', 'created': 'now',
                      'projects': list(entries) or [self.entry()]}
            return target, pipeline.run_plan(target, self.machine, tools or self.tools, self.site)

        def state(self, project_id=ID_A):
            path = os.path.join(self.machine.projects_dir, project_id, pipeline.STATE_FILE)
            with open(path) as handle:
                return json.load(handle)

    return Workspace()


# ---- a whole project ----

def test_a_project_runs_end_to_end(workspace):
    _, failures = workspace.run()
    assert failures == 0
    result = workspace.state()['result']
    assert result['calls']['ecdna_old'] == 0 and result['calls']['ecdna_new'] == 1
    assert result['calls']['amplicon_changes'] == [['S1', '1', 'Linear', 'ecDNA']]
    assert result['expected_samples'] == result['archive_samples'] == 2
    assert result['final_dropped'] == [] and result['control_dropped'] == []
    assert os.path.exists(result['archive']) and os.path.exists(result['report'])
    with open(result['report']) as handle:
        text = handle.read()
    assert '`2.0.0` on the site, `2.0.1` here' in text


def test_a_sample_without_amplicons_stays_in_the_archive(workspace):
    workspace.run()
    project_dir = os.path.join(workspace.machine.projects_dir, ID_A)
    table = os.path.join(project_dir, 'ac_new', 'Project_A_result_table.tsv')
    with open(table) as handle:
        rows = [line.split('\t') for line in handle]
    assert ['S2', 'NA', 'S2_NA'] == rows[-1][:3]


def test_machine_threading_reaches_ac(workspace):
    workspace.run()
    call = workspace.ac_calls.read_text()
    assert '--jobs 2' in call and '--bfb_threads 1' in call and '--ref GRCh38' in call


def test_finished_project_is_not_repeated(workspace):
    workspace.run()
    workspace.run()
    assert workspace.ac_calls.read_text().count('--ref') == 1
    assert workspace.site.downloads == 1


def test_a_crash_repeats_only_the_step_it_was_in(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_EXIT', '3')
    _, failures = workspace.run()
    assert failures == 1
    state = workspace.state()
    assert 'AC exited with 3' in state['failed']
    assert state['done'] == ['download', 'unpack', 'control', 'stage']

    monkeypatch.delenv('STUB_AC_EXIT')
    _, failures = workspace.run()
    assert failures == 0
    assert workspace.state()['failed'] is None
    assert workspace.site.downloads == 1
    assert workspace.ac_calls.read_text().count('--ref') == 2


def test_new_tools_restart_a_project_but_keep_its_download(workspace):
    workspace.run()
    changed = environment.Tools(workspace.tools.ac_cmd, workspace.tools.asa_cmd, 'stub tools 2')
    workspace.run(tools=changed)
    assert workspace.ac_calls.read_text().count('--ref') == 2
    assert workspace.site.downloads == 1
    assert workspace.state()['tools'] == 'stub tools 2'


def test_solver_fallback_stops_the_project_not_the_batch(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_SAYS', 'Gurobi unavailable; retrying with cbc')
    _, failures = workspace.run(workspace.entry(), workspace.entry(ID_B, 'Project B'))
    assert failures == 2
    assert 'did not all use Gurobi' in workspace.state()['failed']
    assert workspace.state()['result'] is None
    assert workspace.ac_calls.read_text().count('--ref') == 2


def test_empty_ac_input_is_a_failure_not_all_no_amp(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_INPUT', '')
    _, failures = workspace.run()
    assert failures == 1
    assert 'AC.input is empty' in workspace.state()['failed']


def test_dropped_sample_is_flagged_for_review(workspace, monkeypatch):
    monkeypatch.setenv('STUB_ASA_DROP', 'S2')
    workspace.run()
    result = workspace.state()['result']
    found = report.concerns(result)
    assert any('site has 2 samples and the rebuilt archive has 1' in item for item in found)
    assert any('rebuilt archive dropped files' in item for item in found)


def test_project_with_a_newer_version_than_the_plan_is_refused(workspace):
    workspace.site.heads = (ID_B,)
    _, failures = workspace.run()
    assert failures == 1
    assert workspace.site.downloads == 0


def test_only_update_rows_run(workspace):
    entries = [workspace.entry(ID_B, 'CoRAL one', status=plan.UNTESTED_TOOL),
               workspace.entry(ID_A, 'Current one', status=plan.CURRENT)]
    _, failures = workspace.run(*entries)
    assert failures == 0
    assert workspace.site.downloads == 0


def test_summary_shows_built_failed_and_concerns(workspace, monkeypatch):
    target, _ = workspace.run(workspace.entry())
    monkeypatch.setenv('STUB_AC_EXIT', '1')
    target, _ = workspace.run(workspace.entry(), workspace.entry(ID_B, 'Project B'),
                              workspace.entry(ID_OLD, 'Never run', status=plan.CURRENT))
    text = report.combined_summary(target, pipeline.load_states(workspace.machine, target['projects']))
    assert '| Project A | built | 2 | 0 to 1 |' in text
    assert '| Project B | failed |' in text
    assert '| Never run | current |' in text
    assert 'Project B: failed: AC exited with 1' in text


# ---- discovery and settings ----

def _unpack(tmp_path, **kwargs):
    root = str(tmp_path / 'unpacked')
    archive.extract(make_archive(str(tmp_path / 'project.tar.gz'), **kwargs), root)
    return root


def test_both_names_for_the_aa_field_are_read(tmp_path):
    for field in ('Reconstruction directory', 'AA directory'):
        target = tmp_path / field.split()[0]
        target.mkdir()
        manifest = {'runs': {'r': [{'Sample name': 'S1', field: 'samples/S1/S1_AA_results'}]}}
        samples = archive.discover_samples(_unpack(target, manifest=manifest))
        assert samples['S1']['aa_dir'].endswith('S1_AA_results')


def test_run_json_path_cannot_leave_the_archive(tmp_path):
    (tmp_path / 'secret').mkdir()
    manifest = {'runs': {'r': [{'Sample name': 'S1', 'Reconstruction directory': '../../secret'}]}}
    samples = archive.discover_from_runjson(_unpack(tmp_path, manifest=manifest))
    assert 'S1' not in samples


def test_a_sample_name_cannot_leave_the_stage_directory(tmp_path):
    with pytest.raises(ProjectError, match='unusable sample name'):
        archive.stage_samples({'../outside': {}}, str(tmp_path))
    assert not (tmp_path.parent / 'outside').exists()


def test_extract_refuses_a_path_outside_the_destination(tmp_path):
    path = str(tmp_path / 'bad.tar.gz')
    with tarfile.open(path, 'w:gz') as tar:
        _add(tar, '../escaped.txt', 'x')
    with pytest.raises(tarfile.TarError):
        archive.extract(path, str(tmp_path / 'dest'))
    assert not (tmp_path / 'escaped.txt').exists()


LOG = ('python amplicon_classifier.py --ref GRCh38 --min_flow 3 --filter_similar --jobs 8\n'
       'AmpliconClassifier 2.0.0\n')


def test_settings_come_from_the_log_and_keep_only_semantic_flags(tmp_path):
    settings = archive.recover_settings(_unpack(tmp_path, ac_log=LOG), 'hg19')
    assert settings['ref'] == 'GRCh38'
    assert settings['flags'] == ['--min_flow', '3', '--filter_similar']
    assert settings['source'] == 'AC log' and settings['settings_ac_version'] == '2.0.0'


def test_default_config_line_is_normal(tmp_path):
    log = LOG + 'Configuration loaded from: default config\n'
    assert archive.recover_settings(_unpack(tmp_path, ac_log=log), 'hg19')['notes'] == []


def test_a_named_config_file_is_flagged(tmp_path):
    log = LOG + 'Configuration loaded from: /some/path/custom.json\n'
    notes = archive.recover_settings(_unpack(tmp_path, ac_log=log), 'hg19')['notes']
    assert any('/some/path/custom.json' in note for note in notes)


def test_a_rerun_of_ac_is_recorded_without_asking_for_attention(tmp_path):
    root = _unpack(tmp_path, ac_log=LOG)
    with open(os.path.join(root, 'results', 'S1_run_metadata.json'), 'w') as handle:
        json.dump({'AC_version': '1.1.3'}, handle)
    settings = archive.recover_settings(root, 'hg19')
    assert settings['notes'] == []
    assert 'log says 2.0.0 and run metadata says 1.1.3' in settings['info'][0]


def test_metadata_without_a_version_is_not_a_rerun(tmp_path):
    root = _unpack(tmp_path, ac_log=LOG)
    with open(os.path.join(root, 'results', 'S1_run_metadata.json'), 'w') as handle:
        json.dump({'AC_version': 'NA'}, handle)
    assert archive.recover_settings(root, 'hg19')['info'] == []


def test_no_log_falls_back_to_the_site_reference_and_says_so(tmp_path):
    settings = archive.recover_settings(_unpack(tmp_path), 'hg38')
    assert settings['ref'] == 'GRCh38' and settings['flags'] == []
    assert any('Reference taken from the site' in note for note in settings['notes'])


def test_a_different_reference_from_the_log_is_flagged(workspace, tmp_path):
    source = make_archive(str(tmp_path / 'hg19.tar.gz'),
                          ac_log='amplicon_classifier.py --ref hg19\nAmpliconClassifier 2.0.0\n')
    workspace.site.source = source
    workspace.run()
    result = workspace.state()['result']
    assert result['ref'] == 'hg19'
    assert any('reference hg19, and the site says GRCh38' in item for item in report.concerns(result))


@pytest.mark.parametrize('old, new', [('Linear amplification', 'Linear'), ('ecDNA', 'ECDNA'),
                                      ('Complex-non-cyclic', 'heavily rearranged'), ('NA', None),
                                      ('No amp/Invalid', 'No amp/Invalid')])
def test_renamed_labels_are_not_changes(old, new):
    assert report.canon_class(old) == report.canon_class(new)


def test_new_content_counts_only_files_the_old_archive_lacks(tmp_path):
    old, new = str(tmp_path / 'old.tar.gz'), str(tmp_path / 'new.tar.gz')
    with tarfile.open(old, 'w:gz') as tar:
        _add(tar, 'a/same.txt', 'unchanged')
    with tarfile.open(new, 'w:gz') as tar:
        _add(tar, 'b/moved_name.txt', 'unchanged')
        _add(tar, 'b/new.txt', '12345')
    assert archive.new_content_bytes(old, new) == 5


# ---- plan ----

HEAD_A = {'id': ID_A, 'project_name': 'Alpha', 'reference_genome': 'hg38', 'sample_count': 3,
          'AC_version': '2.0.0', 'reconstruction_tools': 'AmpliconArchitect', 'CoRAL_version': 'NA',
          'is_latest_version': True, 'previous_versions': [{'linkid': ID_OLD}]}


def _plan(projects, wanted, sizes=None):
    site = FakeSite(None, sizes=sizes)
    site.list_projects = lambda: projects
    return plan.build_plan([dict({'name': '', 'id': '', 'ref': '', 'extra_ac_flags': ''}, **w)
                            for w in wanted], site)['projects']


def test_old_id_and_name_both_resolve_to_the_latest_version():
    for wanted in ({'id': ID_OLD}, {'id': ID_A}, {'name': 'Alpha'}):
        entry = _plan([HEAD_A], [wanted])[0]
        assert entry['id'] == ID_A and entry['status'] == plan.UPDATE and entry['ac_ref'] == 'GRCh38'


def test_a_name_on_two_projects_is_refused():
    other = dict(HEAD_A, id=ID_B, previous_versions=[])
    assert _plan([HEAD_A, other], [{'name': 'Alpha'}])[0]['status'] == plan.AMBIGUOUS


@pytest.mark.parametrize('changes, expected', [
    ({'AC_version': pins.AC_VERSION}, plan.CURRENT),
    ({'reconstruction_tools': 'CoRAL', 'CoRAL_version': '2.2.0'}, plan.UNTESTED_TOOL),
    ({'reference_genome': 'unknown'}, plan.UNKNOWN_REF),
])
def test_projects_that_should_not_run_are_marked(changes, expected):
    assert _plan([dict(HEAD_A, **changes)], [{'name': 'Alpha'}])[0]['status'] == expected


def test_missing_project_and_missing_archive_are_marked():
    assert _plan([HEAD_A], [{'name': 'Nope'}])[0]['status'] == plan.NOT_FOUND
    assert _plan([HEAD_A], [{'name': 'Alpha'}], sizes={ID_A: None})[0]['status'] == plan.NO_ARCHIVE


def test_project_file_skips_comments_and_reads_overrides(tmp_path):
    path = tmp_path / 'list.tsv'
    path.write_text('# a comment\nproject_name\tlatest_id\tref\textra_ac_flags\n'
                    'Alpha\t%s\thg19\t--filter_similar\n' % ID_OLD)
    assert plan.read_project_file(str(path)) == [{'name': 'Alpha', 'id': ID_OLD, 'ref': 'hg19',
                                                  'extra_ac_flags': '--filter_similar'}]


def test_argument_is_read_as_an_id_or_a_name():
    assert plan.wanted_from_argument(ID_A)['id'] == ID_A
    assert plan.wanted_from_argument('Project Name')['name'] == 'Project Name'


def test_smallest_project_comes_first():
    other = dict(HEAD_A, id=ID_B, project_name='Beta', previous_versions=[])
    entries = _plan([HEAD_A, other], [{'name': 'Alpha'}, {'name': 'Beta'}], sizes={ID_A: 900, ID_B: 5})
    assert [e['name'] for e in entries] == ['Beta', 'Alpha']


# ---- talking to a site ----

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get('Authorization'), self.headers.get('User-Agent')))
        behaviour = self.server.behaviours.get(self.path)
        if behaviour:
            return behaviour(self)
        if self.path.endswith('/download/'):
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:%d/storage/x?signature=s' % self.server.server_port)
            self.end_headers()
        elif self.path.startswith('/storage/'):
            body = b'0123456789'
            if self.headers.get('Range'):
                self.send_response(206)
                self.send_header('Content-Range', 'bytes 0-0/10')
                self.send_header('Content-Length', '1')
                self.end_headers()
                self.wfile.write(body[:1])
            else:
                self.send_response(200)
                self.send_header('Content-Length', '10')
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'[]')


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    httpd.seen, httpd.behaviours = [], {}
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def _site(server, token='secret'):
    return Site('dev', token=token, base_url='http://127.0.0.1:%d' % server.server_port)


def test_token_goes_to_the_site_and_never_to_storage(server, tmp_path):
    dest = str(tmp_path / 'project.tar.gz')
    _site(server).download(ID_A, dest)
    with open(dest, 'rb') as handle:
        assert handle.read() == b'0123456789'
    api, storage = server.seen
    assert api[1] == 'Token secret' and api[2] == USER_AGENT
    assert storage[0].startswith('/storage/') and storage[1] is None


def test_size_reads_one_byte_from_storage(server):
    assert _site(server).archive_size(ID_A) == 10
    assert server.seen[-1][1] is None


def test_missing_stored_archive_gives_no_size(server):
    def missing(handler):
        handler.send_response(404)
        handler.end_headers()
    server.behaviours['/storage/x?signature=s'] = missing
    assert _site(server).archive_size(ID_A) is None


def test_truncated_download_never_takes_the_final_name(server, tmp_path):
    def truncated(handler):
        handler.send_response(200)
        handler.send_header('Content-Length', '100')
        handler.end_headers()
        handler.wfile.write(b'short')
    server.behaviours['/storage/x?signature=s'] = truncated
    dest = str(tmp_path / 'project.tar.gz')
    with pytest.raises(Exception):
        _site(server).download(ID_A, dest)
    assert not os.path.exists(dest)


def test_rate_limit_is_waited_out(server):
    attempts = []

    def limited(handler):
        attempts.append(1)
        if len(attempts) == 1:
            handler.send_response(429)
            handler.send_header('Retry-After', '0')
            handler.end_headers()
        else:
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(b'[{"id": "x"}]')
    server.behaviours['/api/v1/projects/'] = limited
    assert _site(server).list_projects() == [{'id': 'x'}]


def test_bad_token_is_a_clear_error(server):
    def refused(handler):
        handler.send_response(401)
        handler.end_headers()
    server.behaviours['/api/v1/projects/'] = refused
    with pytest.raises(SiteError, match='refused the token'):
        _site(server).list_projects()


# ---- tools and machine ----

GOOD = {'ac': pins.AC_VERSION, 'ac_commit': pins.AC_COMMIT, 'bfbarchitect': pins.BFBARCHITECT,
        'solver': pins.REQUIRED_SOLVER, 'gurobi': pins.GUROBI, 'aggregator': '8.3.1'}


def test_pinned_tools_have_no_problems():
    assert environment.version_problems(GOOD, '8.3.1') == []


@pytest.mark.parametrize('change, words', [
    ({'ac': '2.0.0'}, 'AC is 2.0.0'),
    ({'ac_commit': 'moved'}, 'from commit moved'),
    ({'bfbarchitect': '1.0.1'}, 'BFBArchitect is 1.0.1'),
    ({'gurobi': '12.0.0'}, 'Gurobi is 12.0.0'),
    ({'aggregator': '8.3.0'}, 'aggregator is 8.3.0'),
    ({'solver': 'cbc'}, 'would use cbc'),
])
def test_each_difference_from_the_pins_is_a_problem(change, words):
    problems = environment.version_problems(dict(GOOD, **change), '8.3.1')
    assert len(problems) == 1 and words in problems[0]


def test_aggregator_pin_comes_from_the_sites_requirements(tmp_path):
    path = tmp_path / 'requirements.txt'
    path.write_text('django==4\nAmpliconSuiteAggregator==9.9.9\n')
    assert pins.aggregator_pin(str(path)) == '9.9.9'
    assert pins.aggregator_pin()


def test_reference_check(tmp_path):
    ref = tmp_path / 'GRCh38'
    ref.mkdir()
    assert 'file_list' in environment.check_reference(str(tmp_path), 'GRCh38')
    (ref / 'file_list.txt').write_text('fa_file x.fa\nexon_file #unused\ngene_filename genes.gff\n')
    (ref / 'x.fa').write_text('')
    assert 'missing 1 named file' in environment.check_reference(str(tmp_path), 'GRCh38')
    (ref / 'genes.gff').write_text('')
    assert environment.check_reference(str(tmp_path), 'GRCh38') is None


def test_machine_settings_round_trip(tmp_path):
    path = str(tmp_path / 'machine.json')
    Machine(workdir='/w', jobs=16, bfb_threads=2, token_files={'prod': '/t'}).save(path)
    loaded = Machine.load(path)
    assert (loaded.jobs, loaded.bfb_threads, loaded.token_files) == (16, 2, {'prod': '/t'})
    assert loaded.python == '/w/env/bin/python'


def test_the_tool_imports_nothing_from_the_site():
    for name in os.listdir(PACKAGE_DIR):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(PACKAGE_DIR, name)) as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module]
            for module in modules:
                assert not module.startswith(('caper', 'django', 'pymongo')), (name, module)
