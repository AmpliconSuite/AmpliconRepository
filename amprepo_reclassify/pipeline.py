"""`run`: download, AC, rebuild, check and report, per project and resumable.

Each project works in its own directory, keyed by version id, with a state file. A step
clears its own output before it starts and is marked done only after it succeeds, so a crash
repeats at most the step it was in. A change of tools starts the project again.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import traceback

from . import archive, plan as plan_module, report
from .archive import ProjectError
from .site_api import SiteError

# AC can't request a solver. BFBArchitect autodetects one and moves to MOSEK or CBC if Gurobi
# fails, including mid-run, and only logs that. These are its log messages.
SOLVER_FALLBACK = re.compile(r'Gurobi unavailable|retrying with')
STATE_FILE = 'state.json'


def log(message):
    print(message, flush=True)


def _run_logged(label, command, path, cwd=None, env=None):
    """Run a command, showing its output and keeping it in a file."""
    with open(path, 'w') as out:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', cwd=cwd, env=env)
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            out.write(line)
    if process.wait():
        raise ProjectError('%s exited with %d. Its output is in %s' % (label, process.returncode, path))


def refuse_solver_fallback(ac_dir):
    """Stop if any amplicon in this AC run was solved without Gurobi."""
    hits = []
    for dirpath, _, names in os.walk(ac_dir):
        for name in names:
            if name.endswith(('.log', '.out')):
                with open(os.path.join(dirpath, name), errors='replace') as handle:
                    hits += ['%s: %s' % (name, line.strip()) for line in handle
                             if SOLVER_FALLBACK.search(line)]
    if hits:
        raise ProjectError('%d solver fallback line(s): these results did not all use Gurobi. '
                           'First: %s' % (len(hits), hits[0]))


def safe_name(name, fallback):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_') or fallback


class ProjectRun:
    def __init__(self, entry, machine, tools, site):
        self.entry = entry
        self.machine = machine
        self.tools = tools
        self.site = site
        self.dir = os.path.join(machine.projects_dir, entry['id'])
        self.name = safe_name(entry['name'], entry['id'])
        self.tgz = os.path.join(self.dir, entry['id'] + '.tar.gz')
        self.unpacked = os.path.join(self.dir, 'download')
        self.stage_dir = os.path.join(self.dir, 'stage')
        self.ac_dir = os.path.join(self.dir, 'ac_new')
        self.control_dir = os.path.join(self.dir, 'control')
        self.build_dir = os.path.join(self.dir, 'build')
        self.aux_dir = os.path.join(self.dir, 'aux_inputs')
        local = entry.get('local_archive')
        self.source = 'local %s' % local['sha256'] if local else 'site'
        self.ac_prefix = os.path.join(self.ac_dir, self.name)
        self.state = self._load_state()

    def _state_path(self):
        return os.path.join(self.dir, STATE_FILE)

    def _load_state(self):
        os.makedirs(self.dir, exist_ok=True)
        fresh = {'id': self.entry['id'], 'tools': self.tools.description, 'source': self.source,
                 'done': [], 'validation': {}, 'failed': None, 'result': None}
        try:
            with open(self._state_path()) as handle:
                state = json.load(handle)
        except FileNotFoundError:
            return fresh
        changed_source = state.get('source', 'site') != self.source
        if changed_source or state.get('tools') != self.tools.description:
            log('  %s changed since this project\'s last run, so it starts again'
                % ('the input archive' if changed_source else 'tools'))
            for name in os.listdir(self.dir):
                path = os.path.join(self.dir, name)
                if path == self.tgz and not changed_source:
                    continue
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            return fresh
        return state

    def _save_state(self):
        partial = self._state_path() + '.tmp'
        with open(partial, 'w') as handle:
            json.dump(self.state, handle, indent=1)
        os.replace(partial, self._state_path())

    def _step(self, name, function, output_dir=None):
        if name in self.state['done']:
            log('  %s: already done' % name)
            return
        log('  %s ...' % name)
        if output_dir:
            shutil.rmtree(output_dir, ignore_errors=True)
            os.makedirs(output_dir)
        function()
        self.state['done'].append(name)
        self._save_state()

    # ---- the steps ----

    def _download(self):
        local = self.entry.get('local_archive')
        if local:
            self._copy_local(local)
        # A version's archive doesn't change, and only a complete file gets this name.
        elif not os.path.exists(self.tgz):
            self.site.download(self.entry['id'], self.tgz)

    def _copy_local(self, local):
        """The project list's archive instead of the site's, if it is still the file `plan` saw."""
        if not os.path.isfile(local['path']):
            raise ProjectError('the local archive %s is not there' % local['path'])
        partial = self.tgz + '.partial'
        shutil.copyfile(local['path'], partial)
        if os.path.getsize(partial) != local['bytes'] or plan_module.sha256(partial) != local['sha256']:
            os.remove(partial)
            raise ProjectError('%s has changed since `plan` recorded it. Run `plan` again.' % local['path'])
        os.replace(partial, self.tgz)

    def _unpack(self):
        archive.extract(self.tgz, self.unpacked)
        archive.clean_mac_junk(self.unpacked)

    def _aggregate(self, inputs, out_name, build_dir):
        output = os.path.join(build_dir, out_name + '.tar.gz')
        _run_logged('the aggregator', self.tools.asa_cmd + ['--files'] + inputs + ['-o', out_name],
                    os.path.join(build_dir, 'aggregator_console.out'),
                    cwd=build_dir, env=self.tools.env())
        if not os.path.exists(output):
            raise ProjectError('the aggregator did not produce %s' % output)
        subprocess.run(['gzip', '-t', output], check=True)
        return output

    def _control(self):
        """Rebuild the original AA and AC without reclassifying. Anything lost here is the
        original layout defeating the aggregator, not the new AC."""
        built = self._aggregate([archive.results_root(self.unpacked)], self.name + '_control', self.control_dir)
        dropped = archive.plots_in_tree(self.unpacked) - archive.plots_in_archive(built)
        self.state['validation']['control_dropped'] = sorted(dropped)

    def _reference(self, settings):
        """A project list's override, then what AC used last time, which falls back to the site."""
        return self.entry.get('ref_override') or settings['ref']

    def _classify(self, samples, settings, flags):
        ref = self._reference(settings)
        if not ref:
            raise ProjectError('no reference for AC. Add a ref column to the project list.')
        # Checked before AC because AC takes hours on a large project and only fails at the end.
        repeated = archive.repeated_names(self.stage_dir, '_graph.txt')
        if repeated:
            raise ProjectError('%d AA graph file(s) are staged more than once, first %s. AC would '
                               'classify them twice.' % (len(repeated), repeated[0]))
        command = self.tools.ac_cmd + ['--ref', ref, '--AA_results', self.stage_dir + '/',
                                       '-o', self.ac_prefix, '--verbose_classification',
                                       '--jobs', str(self.machine.jobs),
                                       '--bfb_threads', str(self.machine.bfb_threads)] + flags
        _run_logged('AC', command, os.path.join(self.ac_dir, 'ac_console.out'), env=self.tools.env())
        refuse_solver_fallback(self.ac_dir)
        # AC pairs <amplicon>_graph.txt with raw <amplicon>_cycles.txt. An archive with only
        # AC's annotated cycles gives an empty AC.input, and every amplicon would become No amp.
        ac_input = os.path.join(self.ac_dir, 'AC.input')
        graphs = len(archive.find_files(self.stage_dir, '_graph.txt'))
        if graphs and (not os.path.exists(ac_input) or os.path.getsize(ac_input) == 0):
            raise ProjectError('AC.input is empty but %d AA graph files were staged, so raw '
                               '_cycles.txt files are missing from the archive.' % graphs)
        table = self.ac_prefix + '_result_table.tsv'
        new_calls, _ = report.load_result_table(table)
        classified = {sample for sample, _ in new_calls}
        unseen = [s for s in samples if s not in classified]
        if unseen:
            report.add_no_amp_rows(table, unseen, ref)
            log('  added no-amplification rows for %d sample(s), first %s' % (len(unseen), unseen[0]))

    def _build(self):
        inputs = [self.stage_dir, self.ac_dir]
        # The aggregator recognises an AUX folder only among the subdirectories of an input.
        shutil.rmtree(self.aux_dir, ignore_errors=True)
        aux = archive.aux_dirs(self.unpacked)
        if aux:
            os.makedirs(self.aux_dir)
            for index, path in enumerate(aux):
                shutil.copytree(path, os.path.join(self.aux_dir, '%d' % index, os.path.basename(path)))
            inputs += [os.path.join(self.aux_dir, '%d' % index) for index in range(len(aux))]
        built = self._aggregate(inputs, self.name + '_reclass', self.build_dir)
        validation = self.state['validation']
        control = os.path.join(self.control_dir, self.name + '_control.tar.gz')
        # The control is the aggregator's own repack of the input, so anything it has that the
        # rebuild lacks was lost here. The reclassifier only replaces AC's output.
        missing = archive.missing_paths(control, built)
        if missing:
            raise ProjectError('the rebuilt archive lacks %d file(s) that the input has, first %s'
                               % (len(missing), missing[0]))
        # Only meaningful when the input already has the aggregator's layout; an older layout
        # moves every path.
        validation['repack_dropped'] = (archive.missing_paths(self.tgz, control)
                                        if archive.has_member(self.tgz, 'results/run.json') else [])
        validation['final_dropped'] = sorted(archive.kept_content(control) - archive.kept_content(built))
        manifest = archive.run_json_summary(built)
        validation['archive_samples'] = len(manifest['samples'])
        validation['run_json_bytes'] = manifest['bytes']
        validation['new_content_bytes'] = archive.new_content_bytes(self.tgz, built)
        os.makedirs(self.machine.output_dir, exist_ok=True)
        final = os.path.join(self.machine.output_dir, self.name + '_reclass.tar.gz')
        shutil.move(built, final)
        validation['archive'] = final
        validation['archive_bytes'] = os.path.getsize(final)

    def _report(self, samples, settings, flags):
        new_calls, new_version = report.load_result_table(self.ac_prefix + '_result_table.tsv')
        old_calls = report.load_old_calls(archive.old_ac_dirs(self.unpacked))
        validation = self.state['validation']
        result = dict(
            validation, name=self.entry['name'], id=self.entry['id'],
            url=self.site.project_url(self.entry['id']),
            ref=self._reference(settings), site_ref=self.entry.get('ac_ref'),
            # The API's version, because an archive without an AC log only has an older one.
            current_ac=self.entry.get('current_ac'), new_ac=new_version,
            tools=self.tools.description, settings=settings, flags=flags,
            extra_ac_flags=self.entry.get('extra_ac_flags'),
            local_archive=self.entry.get('local_archive'),
            expected_samples=self.entry.get('samples'),
            discovered_samples=len(samples),
            cnv_beds=sum(1 for paths in samples.values() if paths.get('cnv_calls_bed')),
            calls=report.compare_calls(old_calls, new_calls))
        text = report.project_report(result)
        path = os.path.join(self.machine.output_dir, self.name + '_reclass_report.md')
        with open(path, 'w') as handle:
            handle.write(text)
        result['report'] = path
        self.state['result'] = result
        log('\n' + text)

    # ---- one project ----

    def execute(self):
        if self.state['result']:
            log('  already built: %s' % self.state['result']['archive'])
            return
        self.state['failed'] = None
        self._step('download', self._download)
        self._step('unpack', self._unpack, self.unpacked)
        samples = archive.discover_samples(self.unpacked)
        if not samples:
            raise ProjectError('no AA results found in the archive')
        problems = archive.structure_problems(samples)
        if problems:
            where = 'the local archive' if self.entry.get('local_archive') else 'the archive on the site'
            raise ProjectError('%s has a structural problem, so nothing was run. Fix it there, then '
                               'run again. %d sample(s), first %s' % (where, len(problems), problems[0]))
        settings = archive.recover_settings(self.unpacked, self.entry.get('reference'))
        flags = list(settings['flags'])
        flags += [f for f in (self.entry.get('extra_ac_flags') or '').split() if f not in flags]
        log('  %d samples. Settings from %s: --ref %s %s' % (
            len(samples), settings['source'], self._reference(settings),
            ' '.join(flags) or '(no other flags)'))
        for note in settings['notes'] + settings['info']:
            log('    note: %s' % note)
        self._step('control', self._control, self.control_dir)
        self._step('stage', lambda: archive.stage_samples(samples, self.stage_dir), self.stage_dir)
        self._step('classify', lambda: self._classify(samples, settings, flags), self.ac_dir)
        self._step('build', self._build, self.build_dir)
        self._step('report', lambda: self._report(samples, settings, flags))
        self._save_state()


def save_state(machine, project_id, state):
    """Write a project's state file, for a later stage that adds to it."""
    path = os.path.join(machine.projects_dir, project_id, STATE_FILE)
    with open(path + '.tmp', 'w') as handle:
        json.dump(state, handle, indent=1)
    os.replace(path + '.tmp', path)


def load_states(machine, entries):
    """Whatever state each project in a plan has on disk."""
    states = {}
    for entry in entries:
        if not entry.get('id'):
            continue
        try:
            with open(os.path.join(machine.projects_dir, entry['id'], STATE_FILE)) as handle:
                states[entry['id']] = json.load(handle)
        except FileNotFoundError:
            pass
    return states


def run_plan(plan, machine, tools, site, only=None):
    """Work through a plan, smallest first. One project failing doesn't stop the rest.
    Returns the number that failed."""
    heads = {p['id'] for p in site.list_projects() if p.get('is_latest_version', True)}
    failures = 0
    for entry in plan['projects']:
        if only and entry['name'] not in only and entry.get('id') not in only:
            continue
        log('\n%s\n%s (%s)' % ('=' * 78, entry['name'], entry.get('id')))
        if entry['status'] != plan_module.UPDATE:
            log('  skipped: %s. %s' % (entry['status'], entry.get('note') or ''))
            continue
        if not plan_module.is_head(site, heads, entry['id']):
            log('  FAILED: this project has a newer version than the plan. Run `plan` again.')
            failures += 1
            continue
        project = ProjectRun(entry, machine, tools, site)
        try:
            project.execute()
        except Exception as error:  # a full run takes many hours, so one project can't end it
            if not isinstance(error, (ProjectError, SiteError)):
                log(traceback.format_exc())
            project.state['failed'] = str(error)
            project._save_state()
            log('  FAILED: %s' % error)
            failures += 1
    return failures
