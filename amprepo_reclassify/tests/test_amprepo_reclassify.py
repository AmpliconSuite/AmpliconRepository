"""Tests for amprepo_reclassify, with a synthetic archive and a stubbed AC and aggregator.

CI has no Gurobi licence, so nothing here runs the real tools. Nothing here uses the site, Django
or the database, and `amprepo_reclassify/pytest.ini` keeps the site's conftest.py out.
"""

import ast
import dataclasses
import http.server
import io
import json
import os
import re
import sys
import tarfile
import textwrap
import threading

import pytest

from amprepo_reclassify import archive, environment, pins, pipeline, plan, report, slurm, upload
from amprepo_reclassify.archive import ProjectError
from amprepo_reclassify.machine import Machine
from amprepo_reclassify.site_api import Refused, Site, SiteError, USER_AGENT

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
    # Like the real one: AC output goes to consolidated_classification/, a folder with an
    # AUX_DIR file goes whole to other_files/, and everything else is kept.
    with tarfile.open(args.o + '.tar.gz', 'w:gz') as tar:
        for root in args.files:
            for dirpath, dirnames, names in os.walk(root):
                for d in list(dirnames):
                    if os.path.exists(os.path.join(dirpath, d, 'AUX_DIR')):
                        tar.add(os.path.join(dirpath, d), arcname='results/other_files/' + d)
                        dirnames.remove(d)
                for name in names:
                    if name == 'run.json' or name.startswith(drop):
                        continue
                    if name.endswith('_CNV_CALLS.bed'):
                        samples.add(name.split('_CNV_CALLS')[0])
                    path = os.path.join(dirpath, name)
                    if 'consolidated_classification' in dirpath or os.path.basename(root) == 'ac_new':
                        arcname = 'results/consolidated_classification/' + name
                    elif os.path.basename(root) == 'results':
                        arcname = 'results/' + os.path.relpath(path, root)
                    else:  # the stage: one directory per sample
                        arcname = 'results/samples/' + os.path.relpath(path, root)
                    tar.add(path, arcname=arcname)
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


def make_archive(path, ac_log='', manifest=None, extra=()):
    """A modern archive: S1 has an amplicon, and S2 has only a CNV. `extra` is (name, text)
    pairs to add."""
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
        for name, text in extra:
            _add(tar, name, text)
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

    def project(self, project_id):
        """Only what the listing shows, unless a test adds more."""
        raise SiteError('dev: HTTP 404 for /projects/%s/' % project_id)

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


def test_a_change_in_detected_ac_options_rebuilds_the_project(workspace, monkeypatch):
    workspace.run()
    assert workspace.state()['ac_options'] == {'ref': 'GRCh38', 'flags': []}
    recover = archive.recover_settings

    def fixed_detection(root, reference):
        settings = recover(root, reference)
        return dict(settings, flags=settings['flags'] + ['--report_complexity'])

    monkeypatch.setattr(archive, 'recover_settings', fixed_detection)
    _, failures = workspace.run()
    assert failures == 0
    calls = workspace.ac_calls.read_text().splitlines()
    assert len(calls) == 2 and '--report_complexity' in calls[1]
    assert workspace.site.downloads == 1
    assert workspace.state()['ac_options']['flags'] == ['--report_complexity']


def test_a_build_that_did_not_record_its_ac_options_is_rebuilt(workspace):
    workspace.run()
    state = workspace.state()
    del state['ac_options']
    pipeline.save_state(workspace.machine, ID_A, state)
    _, failures = workspace.run()
    assert failures == 0
    assert workspace.ac_calls.read_text().count('--ref') == 2
    assert workspace.site.downloads == 1


def test_solver_fallback_stops_the_project_not_the_batch(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_SAYS', 'Gurobi unavailable; retrying with cbc')
    _, failures = workspace.run(workspace.entry(), workspace.entry(ID_B, 'Project B'))
    assert failures == 2
    assert 'did not all use Gurobi' in workspace.state()['failed']
    assert workspace.state()['result'] is None
    assert workspace.ac_calls.read_text().count('--ref') == 2


def test_lost_bfbarchitect_outputs_stop_the_project(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_SAYS', 'WARNING - Could not write BFBArchitect outputs for '
                                       'x/S1_amplicon1_region1: no display name')
    _, failures = workspace.run()
    assert failures == 1
    assert 'could not write BFBArchitect outputs for 1 region' in workspace.state()['failed']
    assert workspace.state()['result'] is None


def test_a_build_that_lost_bfbarchitect_outputs_is_rebuilt(workspace, monkeypatch):
    workspace.run()
    console = os.path.join(workspace.machine.projects_dir, ID_A, 'ac_new', 'ac_console.out')
    with open(console, 'a') as handle:
        handle.write('WARNING - Could not write BFBArchitect outputs for x: no display name\n')
    _, failures = workspace.run()
    assert failures == 0
    assert workspace.ac_calls.read_text().count('--ref') == 2
    assert workspace.site.downloads == 1


def test_ac_plots_without_a_display():
    assert environment.tool_env()['MPLBACKEND'] == 'Agg'


def test_a_nested_copy_of_the_aa_results_stops_the_project_before_any_work(workspace, tmp_path):
    nested = 'results/samples/S1/S1_AA_results/S1_AA_results/S1_AA_results/'
    workspace.site = FakeSite(make_archive(str(tmp_path / 'nested.tar.gz'), extra=[
        (nested + 'S1_amplicon1_graph.txt', 'aa data for S1_amplicon1_graph.txt')]))
    _, failures = workspace.run()
    assert failures == 1
    state = workspace.state()
    assert 'structural problem' in state['failed']
    assert 'S1: 1 graph file(s) more than once, S1_amplicon1_graph.txt' in state['failed']
    assert state['done'] == ['download', 'unpack']
    assert workspace.ac_calls.read_text() == ''


def test_an_empty_aa_file_stops_the_project_before_any_work(workspace, tmp_path):
    workspace.site = FakeSite(make_archive(str(tmp_path / 'empty.tar.gz'), extra=[
        ('results/samples/S1/S1_AA_results/S1_amplicon2_graph.txt', ''),
        ('results/samples/S1/S1_AA_results/S1_amplicon2_cycles.txt', '')]))
    _, failures = workspace.run()
    assert failures == 1
    assert 'S1: 2 empty AA file(s), first S1_amplicon2_cycles.txt' in workspace.state()['failed']
    assert workspace.ac_calls.read_text() == ''


def test_empty_ac_input_is_a_failure_not_all_no_amp(workspace, monkeypatch):
    monkeypatch.setenv('STUB_AC_INPUT', '')
    _, failures = workspace.run()
    assert failures == 1
    assert 'AC.input is empty' in workspace.state()['failed']


def test_a_rebuild_that_loses_input_files_stops_the_project(workspace, monkeypatch):
    monkeypatch.setenv('STUB_ASA_DROP', 'S2')
    _, failures = workspace.run()
    assert failures == 1
    state = workspace.state()
    assert 'the rebuilt archive lacks 1 file(s) that the input has, first results/samples/S2/S2_CNV_CALLS.bed' in state['failed']
    assert state['result'] is None


def test_other_sample_files_and_aux_folders_survive_the_rebuild(workspace, tmp_path):
    workspace.site = FakeSite(make_archive(str(tmp_path / 'more.tar.gz'), extra=[
        ('results/samples/S1/S1.log', 'pipeline log'),
        ('results/samples/S1/S1_finish_flag.txt', 'All stages completed'),
        ('results/samples/S2/S2_timing_log.txt', 'timing'),
        ('results/other_files/notes/AUX_DIR', ''),
        ('results/other_files/notes/README.md', 'why'),
        ('results/other_files/old_ac/AUX_DIR', ''),
        ('results/other_files/old_ac/old_amplicon_classification_profiles.tsv', 'header\n')]))
    _, failures = workspace.run()
    assert failures == 0, workspace.state()['failed']
    with tarfile.open(workspace.state()['result']['archive']) as tar:
        names = set(tar.getnames())
    for name in ('results/samples/S1/S1.log', 'results/samples/S1/S1_finish_flag.txt',
                 'results/samples/S2/S2_timing_log.txt', 'results/other_files/notes/README.md',
                 'results/other_files/old_ac/old_amplicon_classification_profiles.tsv'):
        assert name in names
    # The AUX copy of an old AC run is the uploader's record, not this version's calls.
    assert workspace.state()['result']['calls']['amplicon_changes'] == [['S1', '1', 'Linear', 'ecDNA']]


def test_a_local_archive_replaces_the_download(workspace, tmp_path):
    local = make_archive(str(tmp_path / 'local.tar.gz'), extra=[('results/samples/S1/S1.log', 'x')])
    entry = workspace.entry(local_archive={'path': local, 'bytes': os.path.getsize(local),
                                           'sha256': plan.sha256(local)})
    _, failures = workspace.run(entry)
    assert failures == 0
    assert workspace.site.downloads == 0
    result = workspace.state()['result']
    assert any('Built from a local archive' in item for item in report.concerns(result))


def test_a_local_archive_that_changed_since_the_plan_is_refused(workspace, tmp_path):
    local = make_archive(str(tmp_path / 'local.tar.gz'))
    entry = workspace.entry(local_archive={'path': local, 'bytes': os.path.getsize(local),
                                           'sha256': '0' * 64})
    _, failures = workspace.run(entry)
    assert failures == 1
    assert 'has changed since `plan` recorded it' in workspace.state()['failed']


def test_switching_to_a_local_archive_starts_the_project_again(workspace, tmp_path):
    workspace.run()
    local = make_archive(str(tmp_path / 'local.tar.gz'))
    entry = workspace.entry(local_archive={'path': local, 'bytes': os.path.getsize(local),
                                           'sha256': plan.sha256(local)})
    _, failures = workspace.run(entry)
    assert failures == 0
    assert workspace.ac_calls.read_text().count('--ref') == 2
    assert workspace.state()['source'].startswith('local ')


def test_project_with_a_newer_version_than_the_plan_is_refused(workspace):
    workspace.site.heads = (ID_B,)
    _, failures = workspace.run()
    assert failures == 1
    assert workspace.site.downloads == 0


def test_only_update_rows_run(workspace):
    entries = [workspace.entry(ID_B, 'No archive', status=plan.NO_ARCHIVE),
               workspace.entry(ID_A, 'Current one', status=plan.CURRENT)]
    _, failures = workspace.run(*entries)
    assert failures == 0
    assert workspace.site.downloads == 0


def test_a_project_reported_by_older_checks_is_reported_again_without_a_rebuild(workspace):
    workspace.run()
    state = workspace.state()
    state['result']['report_version'] = 1
    state['result']['repack_dropped'] = ['stale']
    pipeline.save_state(workspace.machine, ID_A, state)
    _, failures = workspace.run()
    assert failures == 0
    assert workspace.ac_calls.read_text().count('--ref') == 1, 'AC did not run again'
    assert workspace.site.downloads == 1
    result = workspace.state()['result']
    assert result['report_version'] == pipeline.REPORT_VERSION and result['repack_dropped'] == []


def test_an_upload_in_progress_keeps_its_report(workspace):
    workspace.run()
    state = workspace.state()
    state['result']['report_version'] = 1
    state['upload'] = {'state': 'started', 'job_id': 'j1'}
    pipeline.save_state(workspace.machine, ID_A, state)
    workspace.run()
    assert workspace.state()['result']['report_version'] == 1


def test_cnv_beds_the_site_lacked_are_a_note_not_a_concern(workspace):
    workspace.run()
    result = dict(workspace.state()['result'], cnv_beds=0)
    assert report.concerns(result) == []
    assert any('CNV BEDs for 0 of 2' in item for item in report.notes(result))


def test_a_large_run_json_is_flagged_until_a_dry_run_says_it_fits(workspace):
    workspace.run()
    result = dict(workspace.state()['result'], run_json_bytes=report.RECORD_WARNING_BYTES + 1)
    assert any('16 MiB' in item for item in report.concerns(result))
    assert report.concerns(result, {'record': {'fits': True}}) == []
    assert any('16 MiB' in item for item in report.concerns(result, {'record': {'fits': None}}))


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


def test_every_classification_option_is_carried_from_a_real_log_line(tmp_path):
    log = ('2026-07-10 19:17:39,869 - INFO - Command: /x/amplicon_classifier.py --ref GRCh37 '
           '--input HMF.input -o out/HMF --jobs 9 --bfb_threads 2 --verbose_classification '
           '--no_LC_filter --force --no_bfbarchitect --report_complexity --min_size=10000\n'
           'AmpliconClassifier 2.0.0\n')
    settings = archive.recover_settings(_unpack(tmp_path, ac_log=log), 'hg19')
    assert settings['flags'] == ['--no_LC_filter', '--force', '--no_bfbarchitect',
                                 '--min_size', '10000']
    assert settings['notes'] == []


def test_an_option_the_tool_does_not_know_is_flagged_not_dropped_silently(tmp_path):
    log = 'amplicon_classifier.py --ref GRCh38 --plotstyle noplot --filter_similar\nAmpliconClassifier 1.1.3\n'
    settings = archive.recover_settings(_unpack(tmp_path, ac_log=log), 'hg19')
    assert settings['flags'] == ['--filter_similar']
    assert any('--plotstyle' in note and "doesn't carry" in note for note in settings['notes'])


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
    # Where the settings came from is for the review, and stops nothing.
    assert settings['notes'] == []
    assert any('Reference taken from the site' in note for note in settings['info'])
    assert any('AC ran with its defaults' in note for note in settings['info'])


def test_a_flag_that_ac_has_on_already_is_not_passed_or_flagged(workspace, tmp_path):
    log = 'amplicon_classifier.py --ref GRCh38 --report_complexity\nAmpliconClassifier 2.0.0\n'
    workspace.site.source = make_archive(str(tmp_path / 'p.tar.gz'), ac_log=log)
    workspace.run()
    result = workspace.state()['result']
    assert result['flags'] == []
    assert '--report_complexity' not in workspace.ac_calls.read_text()
    assert report.concerns(result) == [] and report.notes(result) == []


def test_logs_that_differ_only_in_a_flag_ac_has_on_already_agree(tmp_path):
    other = ('results/samples/S2/S2_AC.log',
             'amplicon_classifier.py --ref GRCh38\nAmpliconClassifier 2.0.0\n')
    log = 'amplicon_classifier.py --ref GRCh38 --report_complexity\nAmpliconClassifier 2.0.0\n'
    settings = archive.recover_settings(_unpack(tmp_path, ac_log=log, extra=[other]), 'hg19')
    assert settings['notes'] == []


def test_a_real_non_default_flag_is_noted_for_the_review(workspace, tmp_path):
    log = 'amplicon_classifier.py --ref GRCh38 --filter_similar\nAmpliconClassifier 2.0.0\n'
    workspace.site.source = make_archive(str(tmp_path / 'p.tar.gz'), ac_log=log)
    workspace.run()
    result = workspace.state()['result']
    assert report.concerns(result) == []
    assert report.notes(result) == ['Non-default AC flags, carried over: --filter_similar']


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


def _result_table(path, rows):
    with open(path, 'w') as handle:
        handle.write('Sample name\tAA amplicon number\tFeature ID\tClassification\n')
        for sample, amplicon, label in rows:
            handle.write('%s\t%s\t%s_amplicon%s_%s_1\t%s\n' % (sample, amplicon, sample, amplicon, label, label))
    return report.load_result_table(path)[0]


def test_every_feature_of_an_amplicon_is_counted(tmp_path):
    # Measured on CUGA 2026-11-07: keeping one class per amplicon hid 5 of 216 ecDNA+ samples.
    old = _result_table(str(tmp_path / 'old.tsv'), [('S1', '1', 'ecDNA'), ('S1', '1', 'BFB'),
                                                    ('S2', '1', 'BFB'), ('S2', '1', 'ecDNA'),
                                                    ('S3', 'NA', 'NA')])
    new = _result_table(str(tmp_path / 'new.tsv'), [('S1', '1', 'BFB'), ('S1', '1', 'ecDNA'),
                                                    ('S2', '1', 'BFB'),
                                                    ('S3', 'NA', 'NA')])
    calls = report.compare_calls(old, new)
    assert (calls['ecdna_old'], calls['ecdna_new']) == (2, 1)
    assert (calls['bfb_old'], calls['bfb_new']) == (2, 2)
    assert calls['features'] == {'BFB': [2, 2], 'ecDNA': [2, 1]}
    # Row order within an amplicon is not a change.
    assert calls['amplicon_changes'] == [['S2', '1', 'BFB + ecDNA', 'BFB']]
    assert calls['ecdna_lost'] == ['S2']


def test_a_moved_file_is_not_a_dropped_one(tmp_path):
    old, new = str(tmp_path / 'old.tar.gz'), str(tmp_path / 'new.tar.gz')
    with tarfile.open(old, 'w:gz') as tar:
        _add(tar, 'results/samples/S1/S1_AA_results/S1_amplicon1_graph.txt', 'g')
        _add(tar, 'results/samples/S1/S1_AA_results/S1_summary.txt', 's')
        _add(tar, 'results/samples/S1/S1_AA_results/S1.log', 'l')
        _add(tar, 'results/consolidated_classification/old_result_table.tsv', 't')
    with tarfile.open(new, 'w:gz') as tar:
        _add(tar, 'results/samples/S1/S1_reconstruction_results/S1_amplicon1_graph.txt', 'g')
        _add(tar, 'results/samples/S1/S1_reconstruction_results/S1_summary.txt', 's')
    assert archive.missing_names(old, new) == ['results/samples/S1/S1_AA_results/S1.log']


def test_a_file_the_aggregator_gzipped_is_not_a_dropped_one(tmp_path):
    old, new = str(tmp_path / 'old.tar.gz'), str(tmp_path / 'new.tar.gz')
    with tarfile.open(old, 'w:gz') as tar:
        _add(tar, 'results/samples/S1/S1_reconstruction_results/S1_reconstruct.log', 'log')
        _add(tar, 'results/samples/S2/S2_reconstruction_results/S2_reconstruct.log', 'log')
    with tarfile.open(new, 'w:gz') as tar:
        _add(tar, 'results/samples/S1/S1_reconstruction_results/S1_reconstruct.log.gz', 'gz')
    assert archive.missing_names(old, new) == [
        'results/samples/S2/S2_reconstruction_results/S2_reconstruct.log']


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
    ({'reference_genome': 'unknown'}, plan.UNKNOWN_REF),
])
def test_projects_that_should_not_run_are_marked(changes, expected):
    assert _plan([dict(HEAD_A, **changes)], [{'name': 'Alpha'}])[0]['status'] == expected


def test_coral_results_are_planned_like_amplicon_architect():
    coral = dict(HEAD_A, reconstruction_tools='CoRAL', CoRAL_version='2.2.0')
    assert _plan([coral], [{'name': 'Alpha'}])[0]['status'] == plan.UPDATE


def test_missing_project_and_missing_archive_are_marked():
    assert _plan([HEAD_A], [{'name': 'Nope'}])[0]['status'] == plan.NOT_FOUND
    assert _plan([HEAD_A], [{'name': 'Alpha'}], sizes={ID_A: None})[0]['status'] == plan.NO_ARCHIVE


def test_project_file_skips_comments_and_reads_overrides(tmp_path):
    path = tmp_path / 'list.tsv'
    path.write_text('# a comment\nproject_name\tlatest_id\tref\textra_ac_flags\n'
                    'Alpha\t%s\thg19\t--filter_similar\n' % ID_OLD)
    assert plan.read_project_file(str(path)) == [{'name': 'Alpha', 'id': ID_OLD, 'ref': 'hg19',
                                                  'extra_ac_flags': '--filter_similar', 'metadata_sheet': '',
                                                  'archive': ''}]


def test_a_local_archive_in_the_project_file_is_recorded_by_hash(tmp_path):
    (tmp_path / 'clean.tar.gz').write_bytes(b'archive bytes')
    path = tmp_path / 'list.tsv'
    path.write_text('latest_id\tarchive\n%s\tclean.tar.gz\n' % ID_A)
    wanted = plan.read_project_file(str(path))
    assert wanted[0]['archive'] == str(tmp_path / 'clean.tar.gz')
    entry = _plan([HEAD_A], wanted, sizes={ID_A: None})[0]
    assert entry['status'] == plan.UPDATE
    assert entry['local_archive']['bytes'] == 13 and entry['archive_bytes'] == 13
    assert entry['local_archive']['sha256'] == plan.sha256(str(tmp_path / 'clean.tar.gz'))
    (tmp_path / 'clean.tar.gz').unlink()
    assert _plan([HEAD_A], wanted)[0]['status'] == plan.NO_ARCHIVE


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


# ---- writing to a site ----

class _PostHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.server.posts.append({'path': self.path, 'token': self.headers.get('Authorization'),
                                  'type': self.headers.get('Content-Type'),
                                  'body': self.rfile.read(length), 'length': length})
        code, answer, headers = self.server.answers.pop(0)
        payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def post_server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _PostHandler)
    httpd.posts, httpd.answers = [], []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, Site('dev', token='secret', base_url='http://127.0.0.1:%d' % httpd.server_port)
    httpd.shutdown()


def test_an_upload_streams_the_archive_and_says_what_it_is(post_server, tmp_path):
    server, site = post_server
    archive_file = tmp_path / 'x_reclass.tar.gz'
    content = os.urandom(3 * 1024 * 1024)
    archive_file.write_bytes(content)
    sheet = tmp_path / 'sheet.tsv'
    sheet.write_text('sample_name\tcolour\n')
    server.answers.append((202, {'job_id': 'j1'}, {}))

    assert site.replace(ID_A, str(archive_file), ID_B, dry_run=False, metadata_path=str(sheet)) == {'job_id': 'j1'}

    post = server.posts[0]
    assert post['path'] == '/upload_api/replace/%s/' % ID_A
    assert post['token'] == 'Token secret'
    assert post['length'] == len(post['body']), 'Content-Length has to be exact for a streamed body'
    import email.parser
    message = email.parser.BytesParser().parsebytes(
        b'Content-Type: ' + post['type'].encode() + b'\r\n\r\n' + post['body'])
    fields = {part.get_param('name', header='content-disposition'): part for part in message.get_payload()}
    assert fields['dry_run'].get_payload() == 'false', 'false must be sent, not left out'
    assert fields['expected_latest_id'].get_payload() == ID_B
    assert fields['document'].get_filename() == 'x_reclass.tar.gz'
    assert fields['document'].get_payload(decode=True) == content
    assert fields['metadataFile'].get_filename() == 'sheet.tsv'


def test_a_rate_limited_upload_sends_the_whole_body_again(post_server, tmp_path):
    server, site = post_server
    archive_file = tmp_path / 'x.tar.gz'
    archive_file.write_bytes(b'0123456789')
    server.answers += [(429, {}, {'Retry-After': '0'}), (202, {'job_id': 'j1'}, {})]
    site.replace(ID_A, str(archive_file), ID_A, dry_run=True)
    assert b'0123456789' in server.posts[0]['body'] and b'0123456789' in server.posts[1]['body']


@pytest.mark.parametrize('code, answer, kind, expected_code', [
    (409, {'error': 'moved on', 'code': 'stale_version', 'latest_id': ID_B}, Refused, 'stale_version'),
    (403, {'error': 'Only site admins can do this.', 'code': 'admin_required'}, Refused, 'admin_required'),
    (403, b'<html>blocked</html>', Refused, 'blocked'),
    (503, {'error': 'server shutdown is pending'}, SiteError, None),
])
def test_a_refusal_keeps_the_sites_reason(post_server, code, answer, kind, expected_code):
    server, site = post_server
    server.answers.append((code, answer, {}))
    with pytest.raises(kind) as caught:
        site.replace_status('j1')
    assert getattr(caught.value, 'code', None) == expected_code
    assert (type(caught.value) is Refused) == (kind is Refused)
    if isinstance(answer, dict):
        assert answer['error'] in str(caught.value)


def test_a_redirect_on_a_post_is_not_followed(post_server):
    """The token must not go wherever a redirect points."""
    server, site = post_server
    server.answers.append((302, b'', {'Location': 'http://127.0.0.1:1/elsewhere'}))
    with pytest.raises(SiteError):
        site.replace_status('j1')
    assert len(server.posts) == 1


def test_an_admin_token_is_recognised_by_a_404(post_server):
    server, site = post_server
    server.answers.append((404, {'error': 'No such job.', 'code': 'not_found'}, {}))
    site.check_admin()
    server.answers.append((403, {'error': 'Only site admins can do this.', 'code': 'admin_required'}, {}))
    with pytest.raises(Refused):
        site.check_admin()


# ---- `upload` ----

class UploadSite:
    """A site that makes each new version, and can be told to go wrong."""
    name = 'dev'

    def __init__(self, entries=2, features=2, names=1):
        self.sent, self.polls, self.prepared = [], [], []
        self.stored = {'state': 'finished', 'samples': entries, 'features': features, 'page_ready': True,
                       'page_fields': {'sample_data': names, 'reference_genome': 'GRCh38', 'ecDNA_context': 0},
                       'versions': {'AC_version': '2.0.1'},
                       'files': {'shared': 5, 'shared_bytes': 50, 'new': 1, 'new_bytes': 10, 'archive_bytes': 900}}
        self.prediction = {'state': 'finished', 'samples': entries, 'features': features,
                           'record': {'bytes': 1000, 'limit_bytes': 16777216, 'fits': True},
                           'files': {'shared': 5, 'shared_bytes': 50, 'new': 1, 'new_bytes': 10,
                                     'archive_bytes': 900, 'missing': 0}}
        self.refuse = None
        self.page_ready = True
        self.admin = True
        self.latest = True

    def check_admin(self):
        if not self.admin:
            raise Refused('Only site admins can do this.', 'admin_required')

    def project_url(self, project_id):
        return 'https://site/project/' + project_id

    def replace(self, project_id, archive_path, expected_latest_id, dry_run, metadata_path=None):
        self.sent.append({'id': project_id, 'expected': expected_latest_id, 'dry_run': dry_run,
                          'archive': archive_path, 'metadata': metadata_path})
        if self.refuse:
            raise self.refuse
        return {'job_id': ('dry-' if dry_run else 'new-') + project_id}

    def replace_status(self, job_id):
        self.polls.append(job_id)
        return self.prediction if job_id.startswith('dry-') else self.stored

    def prepare_page(self, version_id):
        self.prepared.append(version_id)
        return self.page_ready

    def project(self, project_id):
        return {'id': project_id, 'is_latest_version': self.latest}


def _counts_of(path):
    summary = archive.run_json_summary(path)
    return summary['entries'], summary['features'], len(summary['samples'])


@pytest.fixture
def built(workspace, monkeypatch):
    """Two built projects, A and B, with a change summary. `.send()` runs a real upload."""
    monkeypatch.setattr(upload, 'POLL_SECONDS', 0)
    entries = [workspace.entry(), workspace.entry(project_id=ID_B, name='Project B')]
    target, failures = workspace.run(*entries)
    assert failures == 0
    for project_id in (ID_A, ID_B):
        # The stub archive has no AC log, which is flagged. Nothing here should start flagged.
        state = workspace.state(project_id)
        state['result']['settings']['notes'] = []
        pipeline.save_state(workspace.machine, project_id, state)

    class Built:
        plan = dict(target, target_ac=pins.AC_VERSION)
        machine = workspace.machine
        state = staticmethod(workspace.state)
        site = UploadSite(*_counts_of(workspace.state()['result']['archive']))

        @classmethod
        def summarise(cls):
            os.makedirs(cls.machine.output_dir, exist_ok=True)
            path = os.path.join(cls.machine.output_dir, upload.SUMMARY_FILE)
            with open(path, 'w') as handle:
                handle.write('reviewed')
            newest = max(os.path.getmtime(workspace.state(i)['result']['archive']) for i in (ID_A, ID_B))
            os.utime(path, (newest + 1, newest + 1))

        @classmethod
        def send(cls, **kwargs):
            return upload.run_uploads(cls.plan, cls.machine, cls.site, assume_yes=True, **kwargs)

        @classmethod
        def records(cls):
            path = os.path.join(cls.machine.output_dir, upload.RECORD_FILE)
            if not os.path.exists(path):
                return []
            with open(path) as handle:
                return [json.loads(line) for line in handle]

    Built.summarise()
    return Built


def test_an_upload_sends_each_project_and_records_it(built):
    assert built.send() == 2
    assert [(s['id'], s['expected'], s['dry_run']) for s in built.site.sent] == [(ID_A, ID_A, False), (ID_B, ID_B, False)]
    assert built.site.prepared == ['new-' + ID_A, 'new-' + ID_B]
    first = built.records()[0]
    assert (first['replaced'], first['new_version']) == (ID_A, 'new-' + ID_A)
    assert first['misses'] == [] and first['page_fields']['reference_genome'] == 'GRCh38'
    assert built.state()['upload']['state'] == 'done'


def test_running_it_again_sends_nothing_twice(built):
    built.send()
    assert built.send() == 0
    assert len(built.site.sent) == 2


def test_only_an_admin_token_starts(built):
    built.site.admin = False
    with pytest.raises(Refused):
        built.send()
    assert built.site.sent == []


def test_no_summary_means_no_upload(built):
    os.remove(os.path.join(built.machine.output_dir, upload.SUMMARY_FILE))
    with pytest.raises(upload.Stop, match='summary'):
        built.send()
    assert built.site.sent == []


def test_an_archive_newer_than_the_summary_has_not_been_reviewed(built):
    path = built.state()['result']['archive']
    future = os.path.getmtime(path) + 1000
    os.utime(path, (future, future))
    with pytest.raises(upload.Stop, match='built after the change summary'):
        built.send()
    assert built.site.sent == []


@pytest.mark.parametrize('change, message', [
    ({'samples': 99}, 'samples'),
    ({'features': 99}, 'features'),
    ({'versions': {'AC_version': '2.0.0'}}, 'AC'),
])
def test_a_count_that_differs_from_the_archive_stops_the_batch(built, change, message):
    built.site.stored = dict(built.site.stored, **change)
    with pytest.raises(upload.Stop, match=message):
        built.send()
    assert [s['id'] for s in built.site.sent] == [ID_A], 'the second project must not go up'
    assert built.records()[0]['misses'], 'what went up is still recorded'
    # It did go up, so a second run moves on, and doesn't send it twice.
    with pytest.raises(upload.Stop):
        built.send()
    assert [s['id'] for s in built.site.sent] == [ID_A, ID_B]


def test_a_failed_job_stops_the_batch_and_can_be_retried(built):
    good = built.site.stored
    built.site.stored = {'state': 'failed', 'error': 'it broke', 'restored_project_id': ID_A}
    with pytest.raises(upload.Stop, match='it broke'):
        built.send()
    assert len(built.site.sent) == 1 and built.records() == []
    built.site.stored = good
    assert built.send() == 2
    assert [s['id'] for s in built.site.sent] == [ID_A, ID_A, ID_B]


def test_a_project_that_moved_on_stops_the_batch(built):
    built.site.refuse = Refused('moved on', 'stale_version', ID_OLD)
    with pytest.raises(upload.Stop, match=ID_OLD):
        built.send()
    assert len(built.site.sent) == 1
    assert 'upload' not in built.state()


def test_a_shutdown_refusal_stops_the_batch_plainly(built):
    built.site.refuse = SiteError('dev: HTTP 503. server shutdown is pending')
    with pytest.raises(upload.Stop, match='shutdown is pending'):
        built.send()
    assert len(built.site.sent) == 1


def test_a_job_interrupted_by_a_crash_is_followed_not_sent_again(built, monkeypatch):
    def crash(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(built.site, 'replace_status', crash)
    with pytest.raises(KeyboardInterrupt):
        built.send()
    monkeypatch.undo()
    monkeypatch.setattr(upload, 'POLL_SECONDS', 0)
    assert built.state()['upload'] == {'state': 'started', 'job_id': 'new-' + ID_A, 'replaces': ID_A}
    built.site = UploadSite(*_counts_of(built.state()['result']['archive']))
    assert built.send() == 2
    assert [s['id'] for s in built.site.sent] == [ID_B]
    assert 'new-' + ID_A in built.site.polls


def test_an_outage_while_waiting_is_not_a_failure(built, monkeypatch):
    real = built.site.replace_status
    answers = [SiteError('dev: HTTP 502'), SiteError('dev: lost the connection')]

    def flaky(job_id):
        if answers:
            raise answers.pop(0)
        return real(job_id)

    monkeypatch.setattr(built.site, 'replace_status', flaky)
    assert built.send() == 2


def test_a_long_outage_stops_the_batch_without_forgetting_the_job(built, monkeypatch):
    def down(job_id):
        raise SiteError('dev: HTTP 502')

    monkeypatch.setattr(built.site, 'replace_status', down)
    monkeypatch.setattr(upload, 'OUTAGE_LIMIT_SECONDS', 0)
    with pytest.raises(upload.Stop, match='not answered'):
        built.send()
    assert built.state()['upload']['state'] == 'started'


def test_a_page_that_is_not_ready_stops_the_batch_and_is_asked_again(built, monkeypatch):
    built.site.page_ready = False
    good = built.site.stored
    built.site.stored = dict(good, page_ready=False)
    monkeypatch.setattr(upload, 'PAGE_LIMIT_SECONDS', 0)
    with pytest.raises(upload.Stop, match='has not stored what its page needs'):
        built.send()
    assert [s['id'] for s in built.site.sent] == [ID_A] and built.records() == []
    assert built.state()['upload']['state'] == 'started'

    built.site.page_ready = True
    built.site.stored = good
    assert built.send() == 2
    assert [s['id'] for s in built.site.sent] == [ID_A, ID_B], 'asked again, not sent again'
    assert built.state()['upload']['state'] == 'done'


def test_a_site_without_the_prepare_route_stops_the_batch(built, monkeypatch):
    def missing(version_id):
        raise Refused('dev: HTTP 404', 'not_found')

    monkeypatch.setattr(built.site, 'prepare_page', missing)
    with pytest.raises(upload.Stop, match='has not stored what its page needs'):
        built.send()


@pytest.mark.parametrize('fields, message', [
    ({'sample_data': 0}, 'sample_data'),
    ({'reference_genome': None}, 'reference_genome'),
    ({'reference_genome': ''}, 'reference_genome'),
])
def test_a_page_field_that_is_missing_stops_the_batch(built, fields, message):
    built.site.stored['page_fields'] = dict(built.site.stored['page_fields'], **fields)
    with pytest.raises(upload.Stop, match=message):
        built.send()
    assert len(built.site.sent) == 1
    assert built.records()[0]['misses']


def test_an_empty_context_is_only_wrong_if_the_archive_has_calls(built, monkeypatch):
    assert built.send(only=['Project A']) == 1, 'no calls, so empty is right'
    monkeypatch.setattr(upload.archive, 'context_calls', lambda path: 3)
    with pytest.raises(upload.Stop, match='ecDNA_context is empty, and the archive holds 3'):
        built.send(only=['Project B'])


def test_context_calls_are_counted_from_the_archive(tmp_path):
    path = str(tmp_path / 'x.tar.gz')
    with tarfile.open(path, 'w:gz') as tar:
        _add(tar, 'results/run.json', '{}')
        _add(tar, 'results/consolidated/p_ecDNA_context_calls.tsv', 'S1_1\tthing\nS1_2\tthing\n\n')
        _add(tar, 'results/consolidated/._p_ecDNA_context_calls.tsv', 'mac junk\n')
    assert archive.context_calls(path) == 2


def test_nothing_goes_up_until_ac_has_finished_for_everything(built):
    """AC on everything, then the review, then the upload."""
    state = built.state(ID_B)
    state['result'], state['failed'] = None, 'it broke'
    pipeline.save_state(built.machine, ID_B, state)
    with pytest.raises(upload.Stop, match='AC has not finished for 1 project.*Project B'):
        built.send()
    assert built.site.sent == []


def test_a_project_never_run_holds_the_upload_too(built, workspace):
    built.plan['projects'].append(workspace.entry(project_id=ID_OLD, name='Project C'))
    with pytest.raises(upload.Stop, match='Project C'):
        built.send()
    assert built.site.sent == []


def test_only_is_the_way_to_send_part_of_a_plan(built):
    state = built.state(ID_B)
    state['result'], state['failed'] = None, 'it broke'
    pipeline.save_state(built.machine, ID_B, state)
    assert built.send(only=['Project A']) == 1
    assert [s['id'] for s in built.site.sent] == [ID_A]


def test_a_summary_older_than_any_archive_in_the_plan_is_refused(built):
    """Even one this upload isn't sending: the review is of the whole run."""
    path = built.state(ID_B)['result']['archive']
    future = os.path.getmtime(path) + 1000
    os.utime(path, (future, future))
    with pytest.raises(upload.Stop, match='Project B was built after'):
        built.send(only=['Project A'])
    assert built.site.sent == []


def test_a_new_version_that_is_not_the_latest_stops_the_batch(built):
    built.site.latest = False
    with pytest.raises(upload.Stop, match='not the latest'):
        built.send()


def test_a_flagged_project_waits_until_it_is_accepted(built):
    state = built.state()
    state['result']['expected_samples'] = state['result']['archive_samples'] + 1
    pipeline.save_state(built.machine, ID_A, state)
    assert built.send() == 1
    assert [s['id'] for s in built.site.sent] == [ID_B]
    assert built.send(accept_flagged=True) == 1
    assert [s['id'] for s in built.site.sent] == [ID_B, ID_A]


def test_only_limits_the_upload(built):
    assert built.send(only=['Project B']) == 1
    assert [s['id'] for s in built.site.sent] == [ID_B]


def test_a_metadata_sheet_goes_with_its_project(built, tmp_path):
    sheet = tmp_path / 'sheet.tsv'
    sheet.write_text('x')
    built.plan['projects'][0]['metadata_sheet'] = str(sheet)
    built.plan['projects'][1]['metadata_sheet'] = str(tmp_path / 'gone.tsv')
    assert built.send() == 1, 'a sheet that has gone is a reason not to send that project'
    assert built.site.sent[0]['metadata'] == str(sheet)


def test_the_project_list_reads_a_sheet_relative_to_itself(tmp_path):
    listing = tmp_path / 'list.tsv'
    listing.write_text('project_name\tmetadata_sheet\nAlpha\tsheets/a.tsv\nBeta\t\n')
    wanted = plan.read_project_file(str(listing))
    assert wanted[0]['metadata_sheet'] == str(tmp_path / 'sheets' / 'a.tsv')
    assert wanted[1]['metadata_sheet'] == ''


def test_a_dry_run_changes_nothing_and_reports_what_would_be_stored(built):
    assert upload.run_dry_runs(built.plan, built.machine, built.site) == 0
    assert all(s['dry_run'] for s in built.site.sent) and len(built.site.sent) == 2
    assert 'upload' not in built.state() and built.records() == []
    text = open(os.path.join(built.machine.output_dir, upload.DRY_RUN_FILE)).read()
    assert 'Project A' in text and 'Nothing flagged.' in text
    assert '0.00 GB of new files' in text


def test_a_dry_run_report_includes_what_the_summary_flagged(built):
    state = built.state()
    state['result']['expected_samples'] = state['result']['archive_samples'] + 1
    pipeline.save_state(built.machine, ID_A, state)
    upload.run_dry_runs(built.plan, built.machine, built.site)
    text = open(os.path.join(built.machine.output_dir, upload.DRY_RUN_FILE)).read()
    assert 'Nothing flagged.' not in text
    assert 'Project A: The site has' in text


def test_a_dry_run_keeps_going_after_a_failure_and_says_so(built):
    built.site.prediction = {'state': 'failed', 'error': 'no run.json'}
    assert upload.run_dry_runs(built.plan, built.machine, built.site) == 2
    assert 'no run.json' in open(os.path.join(built.machine.output_dir, upload.DRY_RUN_FILE)).read()


def test_a_real_upload_is_compared_with_its_dry_run(built):
    upload.run_dry_runs(built.plan, built.machine, built.site)
    built.site.sent.clear()
    built.site.stored = dict(built.site.stored, files=dict(built.site.stored['files'], new=99))
    with pytest.raises(upload.Stop, match='predicted 1 and the site stored 99'):
        built.send()
    assert len(built.site.sent) == 1


def test_a_project_too_big_for_the_record_limit_waits(built):
    built.site.prediction = dict(built.site.prediction, record={'bytes': 17_000_000, 'limit_bytes': 16_777_216, 'fits': False})
    upload.run_dry_runs(built.plan, built.machine, built.site)
    built.site.sent.clear()
    assert built.send() == 0
    assert built.site.sent == []


def test_upload_needs_the_plans_site_and_a_token(built, monkeypatch):
    from amprepo_reclassify import cli

    config = os.path.join(built.machine.workdir, 'machine.json')
    built.machine.save(config)
    plan.save_plan(built.plan, built.machine.plan_path)
    with pytest.raises(SystemExit, match='The plan is for dev'):
        cli.main(['--config', config, 'upload', '--target', 'prod'])
    with pytest.raises(SystemExit, match='token'):
        cli.main(['--config', config, 'upload', '--target', 'dev'])
    with pytest.raises(SystemExit):
        cli.main(['--config', config, 'upload'])


# ---- the site's keep-current list ----

HEAD_B = dict(HEAD_A, id=ID_B, project_name='Beta', previous_versions=[])
ID_PRIVATE = 'd' * 24


def _site_plan(listed, readable, include=(), exclude=()):
    """A plan the way `plan --site` makes one with no project file."""
    site = FakeSite(None)
    site.list_projects = lambda: readable
    wanted = plan.wanted_from_site(listed)
    wanted += [plan.wanted_from_argument(value, plan.INCLUDED) for value in include]
    return plan.build_plan(wanted, site, exclude=exclude, listed=len(listed))


def _row(head):
    return {'id': head['id'], 'project_name': head['project_name'], 'visibility': 'public',
            'sample_count': 3}


def test_the_site_list_is_the_plan():
    made = _site_plan([_row(HEAD_A), _row(HEAD_B)], [HEAD_A, HEAD_B])
    assert [(e['name'], e['source']) for e in made['projects']] == [
        ('Alpha', plan.FROM_SITE), ('Beta', plan.FROM_SITE)]
    assert made['list'] == {'from_site': True, 'site_count': 2, 'included': [], 'excluded': []}
    assert "2 on dev's keep-current list" in plan.format_plan(made)


@pytest.mark.parametrize('value', ['Beta', ID_B])
def test_exclude_takes_a_name_or_an_id(value):
    made = _site_plan([_row(HEAD_A), _row(HEAD_B)], [HEAD_A, HEAD_B], exclude=[value])
    assert [e['name'] for e in made['projects']] == ['Alpha']
    assert made['list']['excluded'] == [{'value': value, 'matched': ['Beta']}]
    assert 'excluded by hand: Beta' in plan.format_plan(made)


def test_exclude_takes_an_older_versions_id_too():
    made = _site_plan([_row(HEAD_A)], [HEAD_A], exclude=[ID_OLD])
    assert made['projects'] == []


def test_an_exclude_that_matches_nothing_is_said_not_ignored():
    made = _site_plan([_row(HEAD_A)], [HEAD_A], exclude=['Typo'])
    assert len(made['projects']) == 1
    assert '--exclude Typo matched nothing' in plan.format_plan(made)


def test_include_adds_to_the_list_once():
    made = _site_plan([_row(HEAD_A)], [HEAD_A, HEAD_B], include=['Beta', 'Alpha'])
    assert sorted((e['name'], e['source']) for e in made['projects']) == [
        ('Alpha', plan.FROM_SITE), ('Beta', plan.INCLUDED)]
    assert made['list']['included'] == ['Beta']


def test_a_listed_project_the_token_cannot_read_is_marked_not_dropped():
    hidden = {'id': ID_PRIVATE, 'project_name': 'Hidden', 'visibility': 'hidden_public', 'sample_count': 9}
    made = _site_plan([_row(HEAD_A), hidden], [HEAD_A])
    entry = next(e for e in made['projects'] if e['name'] == 'Hidden')
    assert entry['status'] == plan.NO_ACCESS and entry['id'] == ID_PRIVATE
    assert 'admin' in entry['note']


def test_a_hidden_public_project_missing_from_the_listing_is_read_by_id(monkeypatch):
    """The listing leaves out hidden-public projects the caller isn't a member of."""
    hidden_head = dict(HEAD_A, id=ID_PRIVATE, project_name='Hidden', previous_versions=[])
    site = FakeSite(None)
    site.list_projects = lambda: [HEAD_A]
    site.project = lambda project_id: hidden_head if project_id == ID_PRIVATE else {}
    row = {'id': ID_PRIVATE, 'project_name': 'Hidden', 'visibility': 'hidden_public', 'sample_count': 3}
    made = plan.build_plan(plan.wanted_from_site([row]), site, listed=1)
    assert [(e['name'], e['status']) for e in made['projects']] == [('Hidden', plan.UPDATE)]
    assert plan.is_head(site, {ID_A}, ID_PRIVATE)
    hidden_head['is_latest_version'] = False
    assert not plan.is_head(site, {ID_A}, ID_PRIVATE)


def test_a_given_list_still_says_so():
    site = FakeSite(None)
    site.list_projects = lambda: [HEAD_A]
    made = plan.build_plan([plan.wanted_from_argument('Alpha')], site)
    assert made['list']['from_site'] is False
    assert "not dev's keep-current list" in plan.format_plan(made)


def test_the_change_summary_names_the_exceptions():
    made = _site_plan([_row(HEAD_A), _row(HEAD_B)], [HEAD_A, HEAD_B], exclude=['Beta'])
    text = report.combined_summary(made, {})
    assert "1 on dev's keep-current list" not in text
    assert "2 on dev's keep-current list" in text and 'excluded by hand: Beta' in text


def test_reading_the_list_posts_with_the_token(post_server):
    server, site = post_server
    server.answers.append((200, {'projects': [_row(HEAD_A)]}, {}))
    assert site.keep_current_list() == [_row(HEAD_A)]
    assert server.posts[0]['path'] == '/upload_api/keep-current/'
    assert server.posts[0]['token'] == 'Token secret'


def test_reading_the_list_needs_a_token():
    with pytest.raises(SiteError, match='admin token'):
        Site('dev', token=None, base_url='http://127.0.0.1:1').keep_current_list()


def test_plan_with_no_list_given_reads_the_sites(tmp_path, monkeypatch):
    from amprepo_reclassify import cli

    machine = Machine(workdir=str(tmp_path / 'work'), jobs=1, bfb_threads=1)
    os.makedirs(machine.workdir)
    config = str(tmp_path / 'machine.json')
    machine.save(config)
    site = FakeSite(None)
    site.list_projects = lambda: [HEAD_A, HEAD_B]
    site.keep_current_list = lambda: [_row(HEAD_A), _row(HEAD_B)]
    monkeypatch.setattr(cli, '_site', lambda machine, name: site)

    assert cli.main(['--config', config, 'plan', '--site', 'dev', '--exclude', 'Beta']) == 0
    saved = plan.load_plan(machine.plan_path)
    assert [e['name'] for e in saved['projects']] == ['Alpha']

    assert cli.main(['--config', config, 'plan', '--site', 'dev', '--project', 'Beta']) == 0
    saved = plan.load_plan(machine.plan_path)
    assert [e['name'] for e in saved['projects']] == ['Beta'] and not saved['list']['from_site']

    site.keep_current_list = lambda: []
    with pytest.raises(SystemExit, match='list is empty'):
        cli.main(['--config', config, 'plan', '--site', 'dev'])


# ---- AC from a source tree, and Slurm ----

def _git(path, *args):
    import subprocess
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
                   + list(args), check=True, capture_output=True)


def _ac_checkout(path):
    """A git tree shaped like AC, with the stub as amplicon_classifier.py."""
    (path / 'ampclasslib').mkdir(parents=True)
    (path / 'ampclasslib' / '_version.py').write_text(
        '__ampliconclassifier_version__ = "%s"\n' % pins.AC_VERSION)
    (path / 'amplicon_classifier.py').write_text(STUB_AC)
    (path / 'requirements.txt').write_text('numpy\n')
    _git(path, 'init', '-q')
    _git(path, 'add', '.')
    _git(path, 'commit', '-q', '-m', 'stub')
    import subprocess
    return subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, check=True).stdout.strip()


FAKE_PYTHON = '''#!/bin/bash
# Answers the tool probe with the pinned versions, runs the stub aggregator for -m, and is the
# real interpreter otherwise.
case "$*" in *"print('TOOLS "*) echo 'TOOLS %(tools)s'; exit 0;; esac
if [ "$1" = -I ] && [ "$2" = -m ] && [ "$3" = AmpliconSuiteAggregator ]; then
  shift 3; exec %(python)s %(asa)s "$@"
fi
exec %(python)s "$@"
'''

FAKE_SBATCH = '''#!%(python)s
"""Runs the job script at once, as the job would run, and says its id."""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_SLURM_LOG'], 'a') as log:
    log.write(json.dumps(['sbatch'] + args) + '\\n')
options = dict(a[2:].split('=', 1) for a in args if a.startswith('--') and '=' in a)
counter = os.environ['FAKE_SLURM_LOG'] + '.ids'
job = str(len(open(counter).read().split()) + 101 if os.path.exists(counter) else 101)
open(counter, 'a').write(job + '\\n')
with open(options['output'].replace('%%j', job), 'w') as out:
    subprocess.run(['bash', args[-1]], cwd=options['chdir'], stdout=out, stderr=subprocess.STDOUT,
                   env=dict(os.environ, SLURM_JOB_ID=job))
print(job)
'''

FAKE_SRUN = '''#!%(python)s
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_SLURM_LOG'], 'a') as log:
    log.write(json.dumps(['srun'] + args) + '\\n')
command = args[args.index('bash'):]
sys.exit(subprocess.run(command, env=dict(os.environ, SLURM_JOB_ID='900')).returncode)
'''


@pytest.fixture
def cluster(workspace, tmp_path, monkeypatch):
    """The workspace as a Slurm machine: an AC checkout, a fake interpreter and fake Slurm."""
    ac_src = tmp_path / 'ac_src'
    commit = _ac_checkout(ac_src)
    monkeypatch.setattr(pins, 'AC_COMMIT', commit)
    found = {'bfbarchitect': pins.BFBARCHITECT, 'solver': pins.REQUIRED_SOLVER,
             'gurobi': pins.GUROBI, 'aggregator': pins.aggregator_pin()}
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    values = {'python': sys.executable, 'asa': str(tmp_path / 'stub_asa.py'),
              'tools': json.dumps(found)}
    for name, text in [('python', FAKE_PYTHON), ('sbatch', FAKE_SBATCH), ('srun', FAKE_SRUN),
                       ('squeue', '#!/bin/bash\ncat "$FAKE_SQUEUE" 2>/dev/null; true\n'),
                       ('sinfo', '#!/bin/bash\nprintf "node1 idle\\nnode2 down*\\n"\n')]:
        (bin_dir / name).write_text(text % values if '%(' in text else text)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    slurm_log = tmp_path / 'slurm_calls.jsonl'
    monkeypatch.setenv('FAKE_SLURM_LOG', str(slurm_log))
    monkeypatch.setenv('FAKE_SQUEUE', str(tmp_path / 'queue.txt'))
    data_repo = tmp_path / 'data_repo'
    (data_repo / 'GRCh38').mkdir(parents=True)
    (data_repo / 'GRCh38' / 'file_list.txt').write_text('')
    monkeypatch.setenv('AA_DATA_REPO', str(data_repo))
    monkeypatch.setattr(slurm, 'POLL_SECONDS', 0)
    workspace.machine = Machine(
        workdir=str(tmp_path / 'work'), jobs=2, bfb_threads=1, ac_src=str(ac_src),
        python_path=str(bin_dir / 'python'),
        executor={'kind': 'slurm', 'cpus': 2, 'mem': '1G'})
    workspace.ac_src = ac_src
    workspace.slurm_calls = lambda: [json.loads(line) for line in slurm_log.read_text().splitlines()] \
        if slurm_log.exists() else []
    return workspace


def _slurm_run(cluster, *entries):
    tools = environment.tools_for(cluster.machine)
    plan_ = {'site': 'dev', 'target_ac': '2.0.1', 'created': 'now',
             'projects': list(entries) or [cluster.entry()]}
    return slurm.run_plan(plan_, cluster.machine, tools, cluster.site)


def test_ac_runs_from_a_checked_source_tree(cluster):
    found = environment.read_ac_source(str(cluster.ac_src))
    assert found['ac'] == pins.AC_VERSION and found['ac_commit'] == pins.AC_COMMIT
    assert found['ac_changed'] == [] and 'amplicon_classifier.py' in found['ac_files']
    (cluster.ac_src / 'amplicon_classifier.py').write_text('changed')
    changed = environment.read_ac_source(str(cluster.ac_src))
    problems = environment.version_problems(dict(changed, **{k: GOOD[k] for k in (
        'bfbarchitect', 'solver', 'gurobi')}, aggregator='8.3.1'), '8.3.1')
    assert problems == ['the AC checkout has uncommitted changes to 1 file(s), first '
                        'amplicon_classifier.py']


def test_a_project_runs_as_a_slurm_job(cluster):
    assert _slurm_run(cluster) == 0
    result = cluster.state()['result']
    assert os.path.exists(result['archive'])
    (sbatch,) = cluster.slurm_calls()
    assert '--cpus-per-task=2' in sbatch and '--mem=1G' in sbatch
    # The job sets AC's --jobs from the CPUs it may use, not from the machine's setting.
    expected = max(1, len(os.sched_getaffinity(0)) // cluster.machine.bfb_threads)
    assert '--jobs %d ' % expected in cluster.ac_calls.read_text()
    project_dir = os.path.join(cluster.machine.projects_dir, ID_A)
    with open(os.path.join(project_dir, 'slurm-101.out')) as handle:
        assert 'can use' in handle.read()
    # The job ran a copy of this code in the workdir, not this checkout.
    with open(os.path.join(project_dir, slurm.JOB_SCRIPT)) as handle:
        script = handle.read()
    assert os.path.join(cluster.machine.workdir, 'code') in script and PACKAGE_DIR not in script


def test_every_project_is_submitted_and_slurm_schedules_them(cluster):
    entries = [cluster.entry(), cluster.entry(ID_B, 'Project B')]
    assert _slurm_run(cluster, *entries) == 0
    calls = [c for c in cluster.slurm_calls() if c[0] == 'sbatch']
    assert len(calls) == 2
    assert not any('--dependency=singleton' in c for c in calls)
    names = {a for c in calls for a in c if a.startswith('--job-name=')}
    assert names == {'--job-name=%s' % re.sub(r'[^A-Za-z0-9._-]+', '_', e['name']) for e in entries}
    assert '--job-name=Project_B' in names


def test_max_running_puts_jobs_in_lanes_slurm_runs_one_at_a_time(cluster):
    cluster.machine = dataclasses.replace(cluster.machine,
                                          executor=dict(cluster.machine.executor, max_running=1))
    entries = [cluster.entry(), cluster.entry(ID_B, 'Project B')]
    assert _slurm_run(cluster, *entries) == 0
    calls = [c for c in cluster.slurm_calls() if c[0] == 'sbatch']
    # Both are submitted at once; the shared name and singleton make Slurm run one at a time.
    assert len(calls) == 2
    assert all('--job-name=amprepo-lane0' in c and '--dependency=singleton' in c for c in calls)
    assert any('--comment=Project_B' in c for c in calls)


def test_a_node_with_different_ac_files_runs_nothing(cluster):
    tools = environment.tools_for(cluster.machine)
    (cluster.ac_src / 'amplicon_classifier.py').write_text('print("not the checked AC")')
    plan_ = {'site': 'dev', 'target_ac': '2.0.1', 'created': 'now', 'projects': [cluster.entry()]}
    assert slurm.run_plan(plan_, cluster.machine, tools, cluster.site) == 1
    failed = cluster.state()['failed']
    assert 'not ready' in failed and 'amplicon_classifier.py' in failed
    assert cluster.ac_calls.read_text() == ''


def test_a_job_still_queued_is_followed_not_submitted_again(cluster, monkeypatch):
    project_dir = os.path.join(cluster.machine.projects_dir, ID_A)
    os.makedirs(project_dir)
    with open(os.path.join(project_dir, slurm.JOB_RECORD), 'w') as handle:
        json.dump({'jobs': [{'id': '77'}]}, handle)
    answers = iter([{'77'}, set()])
    monkeypatch.setattr(slurm, 'queued', lambda: next(answers))
    assert _slurm_run(cluster) == 1
    assert cluster.slurm_calls() == []
    assert 'job 77 ended before the project finished' in cluster.state()['failed']


def test_a_queued_job_is_never_restarted_under_it(cluster, monkeypatch):
    # A state from before AC options were recorded would restart the project, but its job is
    # still working in the project's directory.
    project_dir = os.path.join(cluster.machine.projects_dir, ID_A)
    os.makedirs(os.path.join(project_dir, 'ac_new'))
    working = os.path.join(project_dir, 'ac_new', 'in_progress')
    open(working, 'w').close()
    with open(os.path.join(project_dir, slurm.JOB_RECORD), 'w') as handle:
        json.dump({'jobs': [{'id': '77'}]}, handle)
    pipeline.save_state(cluster.machine, ID_A, {
        'id': ID_A, 'tools': environment.tools_for(cluster.machine).description, 'source': 'site',
        'done': ['download', 'unpack', 'control', 'stage', 'classify'], 'validation': {},
        'failed': None, 'result': None})
    answers = iter([{'77'}, set()])
    monkeypatch.setattr(slurm, 'queued', lambda: next(answers))
    _slurm_run(cluster)
    assert os.path.exists(working)
    assert cluster.slurm_calls() == []


def test_preflight_checks_each_available_node(cluster):
    checks = environment.Checks()
    slurm.check_nodes(cluster.machine, environment.tools_for(cluster.machine), {'GRCh38'}, checks)
    assert checks.failed == 0
    assert [call[0] for call in cluster.slurm_calls()] == ['srun']
    assert '--nodelist=node1' in cluster.slurm_calls()[0]


def test_a_named_conda_environment_is_activated_for_each_tool(tmp_path):
    machine = Machine(workdir='/w', jobs=1, bfb_threads=1, ac_src='/ac', conda_env='tools',
                      conda_sh='/c/etc/profile.d/conda.sh')
    assert machine.tool_prefix()[-2:] == ['/c/etc/profile.d/conda.sh', 'tools']
    script = slurm.job_script(machine, '/w/code/x', ['job', '/w/spec.json'])
    assert 'conda activate tools' in script and 'unset PYTHONPATH' in script
    assert 'unset DISPLAY WAYLAND_DISPLAY' in script
    path = tmp_path / 'job.sh'
    path.write_text(script)
    import subprocess
    assert subprocess.run(['bash', '-n', str(path)]).returncode == 0


def test_setup_offers_slurm_but_does_not_switch_it_on(cluster, tmp_path, capsys):
    from amprepo_reclassify import cli
    config = tmp_path / 'machine.json'
    assert cli.main(['--config', str(config), 'setup', '--workdir', str(tmp_path / 'w2'),
                     '--jobs', '4', '--bfb-threads', '2', '--ac-src', str(cluster.ac_src),
                     '--python', cluster.machine.python_path]) == 0
    assert 'Slurm is available here' in capsys.readouterr().out
    assert Machine.load(str(config)).executor == {'kind': 'local'}
