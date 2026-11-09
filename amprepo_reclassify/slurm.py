"""Running each project as a Slurm job, when this machine's settings say so.

The machine that runs `run` downloads each archive, then submits one job per project for the
rest of its steps, on whichever node Slurm picks. Every node needs the workdir and the AC
checkout at the same paths, and the environment named in the settings. Each job checks its node
before it starts and fails the project, with the reason, if anything differs. Uploads stay on
the submitting machine.
"""

import dataclasses
import datetime
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time

from . import environment, pins, pipeline
from .machine import Machine
from .site_api import Site

POLL_SECONDS = 60
OUTAGE_LIMIT_SECONDS = 60 * 60
# Checking every node of a large cluster one at a time would take too long. Each job checks its
# own node anyway, so preflight checks a sample.
MAX_NODES_CHECKED = 8
JOB_SPEC = 'slurm_job.json'
JOB_SCRIPT = 'slurm_job.sh'
# Written only by the submitting machine, so it never races the job's own state file.
JOB_RECORD = 'slurm_jobs.json'
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
UNAVAILABLE = ('down', 'drain', 'drng', 'fail', 'maint', 'unk', 'futr', 'inval', 'boot', 'pow')
CHECK_MARKER = 'NODE-CHECK '


class SlurmError(Exception):
    pass


def log(message):
    print(message, flush=True)


def settings(machine):
    """The Slurm settings, refusing an incomplete set."""
    executor = dict(machine.executor or {})
    missing = [key for key in ('cpus', 'mem') if not executor.get(key)]
    if missing:
        raise SlurmError('the Slurm settings have no %s. Run `setup --executor slurm` again.'
                         % ' or '.join(missing))
    return executor


def usable_cpus():
    """The CPUs this process may run on. Inside a Slurm job that is its allocation, counted the
    way Linux counts them, so hardware threads count even where Slurm counts cores."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


# ---- what a job runs ----

def snapshot_code(machine):
    """A copy of this package in the workdir for jobs to run. A node may not see this checkout,
    and a long run shouldn't change code under jobs that are already running."""
    names = sorted(n for n in os.listdir(PACKAGE_DIR) if n.endswith('.py'))
    digest = hashlib.sha256()
    contents = {}
    for name in names + ['requirements.txt']:
        path = pins.SITE_REQUIREMENTS if name == 'requirements.txt' else os.path.join(PACKAGE_DIR, name)
        with open(path, 'rb') as handle:
            contents[name] = handle.read()
        digest.update(name.encode() + b'\0' + hashlib.sha256(contents[name]).digest())
    root = os.path.join(machine.workdir, 'code')
    dest = os.path.join(root, digest.hexdigest()[:16])
    if os.path.isdir(dest):
        return dest
    os.makedirs(root, exist_ok=True)
    partial = tempfile.mkdtemp(dir=root, prefix='.partial-')
    os.makedirs(os.path.join(partial, 'amprepo_reclassify'))
    for name, data in contents.items():
        # pins finds the site's requirements.txt one level above the package.
        target = (os.path.join(partial, name) if name == 'requirements.txt'
                  else os.path.join(partial, 'amprepo_reclassify', name))
        with open(target, 'wb') as handle:
            handle.write(data)
    try:
        os.rename(partial, dest)
    except OSError:  # another run made the same copy first
        shutil.rmtree(partial)
    return dest


def job_script(machine, code_dir, args):
    """A bash script that sets up the environment and runs one subcommand of this tool."""
    executor = machine.executor or {}
    lines = ['#!/bin/bash',
             '# Written by amprepo_reclassify, and again on each submission.',
             'set -eo pipefail',
             'unset PYTHONPATH PYTHONHOME',
             'export PYTHONNOUSERSITE=1']
    data_repo = os.environ.get('AA_DATA_REPO')
    if data_repo:
        lines.append('export AA_DATA_REPO=%s' % shlex.quote(data_repo))
    lines.append('echo "amprepo_reclassify on $(hostname), Slurm job ${SLURM_JOB_ID:-none}, $(date)"')
    lines += list(executor.get('job_setup') or [])
    if machine.conda_env:
        lines += ['source %s' % shlex.quote(machine.conda_sh),
                  'conda activate %s' % shlex.quote(machine.conda_env)]
        python = 'python'
    else:
        python = shlex.quote(machine.python)
    start = ('import sys; sys.path.insert(0, sys.argv[1]); '
             'from amprepo_reclassify.cli import main; sys.exit(main(sys.argv[2:]))')
    lines.append('exec %s -I -B -c %s %s' % (python, shlex.quote(start),
                                            ' '.join(shlex.quote(a) for a in [code_dir] + args)))
    return '\n'.join(lines) + '\n'


def _write(path, text):
    with open(path + '.tmp', 'w') as handle:
        handle.write(text)
    os.replace(path + '.tmp', path)


def _spec(machine, tools, site_name, refs, entry=None):
    return {'entry': entry, 'site': site_name, 'refs': sorted(r for r in refs if r),
            'machine': dict(machine.to_dict(), token_files={}),
            'tools': {'description': tools.description, 'ac_commit': tools.ac_commit,
                      'ac_files': tools.ac_files}}


def _expected_tools(spec):
    return environment.Tools(ac_cmd=[], asa_cmd=[], **spec['tools'])


def node_problems(machine, refs):
    """What this node lacks apart from the tools: references and a writable workdir."""
    problems = []
    data_repo = os.environ.get('AA_DATA_REPO')
    if not data_repo or not os.path.isdir(data_repo):
        problems.append('$AA_DATA_REPO is %s here' % (data_repo or 'not set'))
    else:
        for ref in refs:
            problem = environment.check_reference(data_repo, ref)
            if problem:
                problems.append(problem)
    try:
        with tempfile.NamedTemporaryFile(dir=machine.workdir, prefix='.node-check-'):
            pass
    except OSError as error:
        problems.append('cannot write to the workdir %s: %s' % (machine.workdir, error))
    return problems


def _reference_for(entry):
    return entry.get('ref_override') or entry.get('ac_ref')


def run_job(spec_path):
    """Inside a Slurm job: check this node, then run the project's steps after its download."""
    with open(spec_path) as handle:
        spec = json.load(handle)
    machine = Machine(**spec['machine'])
    entry = spec['entry']
    host = socket.gethostname()
    try:
        tools = environment.tools_on_node(machine, _expected_tools(spec))
        problems = node_problems(machine, spec['refs'])
        if problems:
            raise environment.ToolProblem('%s: %s' % (host, '; '.join(problems)))
    except environment.ToolProblem as error:
        message = 'this node is not ready, so nothing ran: %s' % error
        state = pipeline.load_states(machine, [entry]).get(entry['id'], {})
        state['failed'] = message
        pipeline.save_state(machine, entry['id'], state)
        log('  FAILED: ' + message)
        return 1
    cpus = usable_cpus()
    machine = dataclasses.replace(machine, jobs=max(1, cpus // machine.bfb_threads))
    log('  %s can use %d CPUs here: AC --jobs %d --bfb_threads %d. Tools: %s'
        % (host, cpus, machine.jobs, machine.bfb_threads, tools.description))
    project = pipeline.ProjectRun(entry, machine, tools, Site(spec['site']))
    try:
        project.execute(offline=True)
    except Exception as error:  # the job's exit status and the state file both say so
        pipeline.record_failure(project, error)
        return 1
    return 0


def check_job(spec_path):
    """Inside a Slurm job: check this node only, printing one line per finding."""
    with open(spec_path) as handle:
        spec = json.load(handle)
    machine = Machine(**spec['machine'])
    host = socket.gethostname()
    problems = []
    try:
        tools = environment.tools_on_node(machine, _expected_tools(spec))
        log('%sok %s: %s, %d usable CPUs' % (CHECK_MARKER, host, tools.description, usable_cpus()))
    except environment.ToolProblem as error:
        problems.append(str(error))
    problems += ['%s: %s' % (host, p) for p in node_problems(machine, spec['refs'])]
    for problem in problems:
        log('%sFAIL %s' % (CHECK_MARKER, problem))
    return 1 if problems else 0


# ---- the submitting machine ----

def _slurm(command, timeout=120):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SlurmError('%s is not on PATH here' % command[0])
    except subprocess.TimeoutExpired:
        raise SlurmError('%s did not answer in %d s' % (command[0], timeout))
    if result.returncode:
        raise SlurmError('%s exited with %d: %s' % (command[0], result.returncode,
                                                    (result.stderr or result.stdout).strip()))
    return result.stdout


def queued():
    """The ids of this user's jobs that Slurm still has, in any state."""
    return set(_slurm(['squeue', '--me', '--noheader', '--format=%i']).split())


def _record(project):
    try:
        with open(os.path.join(project.dir, JOB_RECORD)) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {'jobs': []}


def _last_job(project):
    jobs = _record(project)['jobs']
    return jobs[-1]['id'] if jobs else None


def _log_path(project, job_id):
    return os.path.join(project.dir, 'slurm-%s.out' % job_id)


def submit(project, machine, tools, site_name, code_dir):
    executor = settings(machine)
    spec_path = os.path.join(project.dir, JOB_SPEC)
    script_path = os.path.join(project.dir, JOB_SCRIPT)
    _write(spec_path, json.dumps(_spec(machine, tools, site_name, [_reference_for(project.entry)],
                                       project.entry), indent=1))
    _write(script_path, job_script(machine, code_dir, ['job', spec_path]))
    # Cleared before the job exists, so an old failure can't be read as this job's.
    project.state['failed'] = None
    project._save_state()
    command = ['sbatch', '--parsable', '--job-name=amprepo-%s' % project.entry['id'][:8],
               '--nodes=1', '--ntasks=1', '--cpus-per-task=%s' % executor['cpus'],
               '--mem=%s' % executor['mem'], '--output=%s' % os.path.join(project.dir, 'slurm-%j.out'),
               '--chdir=%s' % project.dir]
    if executor.get('partition'):
        command.append('--partition=%s' % executor['partition'])
    if executor.get('time'):
        command.append('--time=%s' % executor['time'])
    command += list(executor.get('sbatch_args') or []) + [script_path]
    job_id = _slurm(command).strip().split(';')[0]
    if not job_id:
        raise SlurmError('sbatch did not say which job it made')
    record = _record(project)
    record['jobs'].append({'id': job_id, 'submitted': datetime.datetime.now().astimezone().isoformat(),
                           'log': _log_path(project, job_id)})
    _write(os.path.join(project.dir, JOB_RECORD), json.dumps(record, indent=1))
    return job_id


def _last_line(path):
    try:
        with open(path, errors='replace') as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return '(no log at %s, so the job may never have started)' % path
    return lines[-1] if lines else '(the log is empty)'


def _finished(project, job_id):
    """Report a job Slurm no longer has. Returns 1 if the project failed."""
    state = pipeline.load_states(project.machine, [project.entry]).get(project.entry['id'], {})
    name = project.entry['name']
    if state.get('result'):
        log('%s: built by Slurm job %s: %s' % (name, job_id, state['result']['archive']))
        return 0
    if not state.get('failed'):
        state['failed'] = ('Slurm job %s ended before the project finished. The last line of its '
                           'log is: %s' % (job_id, _last_line(_log_path(project, job_id))))
        pipeline.save_state(project.machine, project.entry['id'], state)
    log('%s: FAILED in Slurm job %s: %s\n  log: %s' % (name, job_id, state['failed'],
                                                      _log_path(project, job_id)))
    return 1


def _wait_for_queue(since):
    """The queue, or None during an outage shorter than the limit."""
    try:
        return queued()
    except SlurmError as error:
        if time.monotonic() - since > OUTAGE_LIMIT_SECONDS:
            raise SlurmError('Slurm has not answered for %d minutes: %s. The jobs may still be '
                             'running; run this again to follow them.'
                             % (OUTAGE_LIMIT_SECONDS // 60, error))
        log('  Slurm did not answer, trying again: %s' % error)
        return None


def run_plan(plan, machine, tools, site, only=None):
    """Download each project here, run the rest as Slurm jobs, and wait for them. Running it
    again follows jobs still in the queue rather than submitting them twice. Returns the number
    of projects that failed."""
    executor = settings(machine)
    at_once = executor.get('at_once') or None
    code_dir = snapshot_code(machine)
    failures = 0
    waiting, running = [], {}
    active = queued()
    for entry, refusal in pipeline.runnable(plan, site, only):
        if refusal:
            log('  FAILED: ' + refusal)
            failures += 1
            continue
        project = pipeline.ProjectRun(entry, machine, tools, site)
        if project.state['result']:
            log('  already built: %s' % project.state['result']['archive'])
            continue
        job_id = _last_job(project)
        if job_id in active:
            log('  Slurm job %s is still in the queue, so this follows it' % job_id)
            running[job_id] = project
            continue
        waiting.append(project)
    log('\n%d project(s) to submit, %d already in the queue, at most %s at once.'
        % (len(waiting), len(running), at_once or 'any number'))
    answered = time.monotonic()
    try:
        while waiting or running:
            while waiting and (not at_once or len(running) < at_once):
                project = waiting.pop(0)
                log('%s: downloading here, then submitting' % project.entry['name'])
                try:
                    project.prepare()
                    job_id = submit(project, machine, tools, site.name, code_dir)
                except Exception as error:
                    pipeline.record_failure(project, error)
                    failures += 1
                    continue
                log('%s: Slurm job %s, log %s' % (project.entry['name'], job_id,
                                                   _log_path(project, job_id)))
                running[job_id] = project
            if not running:
                break
            time.sleep(POLL_SECONDS)
            active = _wait_for_queue(answered)
            if active is None:
                continue
            answered = time.monotonic()
            for job_id in [j for j in running if j not in active]:
                failures += _finished(running.pop(job_id), job_id)
    except KeyboardInterrupt:
        log('\nStopped waiting. Submitted jobs keep running: %s. Run this again to follow them, '
            'or scancel them.' % (', '.join(sorted(running)) or 'none'))
        raise
    return failures


def nodes(partition=None):
    """(name, state) for each node Slurm would use, from sinfo."""
    command = ['sinfo', '--noheader', '--Node', '--format=%N %t']
    if partition:
        command.append('--partition=%s' % partition)
    found = {}
    for line in _slurm(command).splitlines():
        parts = line.split()
        if len(parts) == 2:
            found.setdefault(parts[0], parts[1])
    return sorted(found.items())


def check_nodes(machine, tools, refs, checks):
    """Preflight on each node: a short job there checks what a project's job would need."""
    executor = settings(machine)
    try:
        found = nodes(executor.get('partition'))
    except SlurmError as error:
        checks.fail(str(error))
        return
    if not found:
        checks.fail('Slurm lists no nodes%s' % (' in partition %s' % executor['partition']
                                                if executor.get('partition') else ''))
        return
    code_dir = snapshot_code(machine)
    check_dir = os.path.join(machine.workdir, 'node_check')
    os.makedirs(check_dir, exist_ok=True)
    spec_path = os.path.join(check_dir, JOB_SPEC)
    script_path = os.path.join(check_dir, JOB_SCRIPT)
    _write(spec_path, json.dumps(_spec(machine, tools, None, refs), indent=1))
    _write(script_path, job_script(machine, code_dir, ['job', '--check', spec_path]))
    if len(found) > MAX_NODES_CHECKED:
        checks.warn('Slurm lists %d nodes; checking the first %d. Each job checks its own node.'
                    % (len(found), MAX_NODES_CHECKED))
    for name, state in found[:MAX_NODES_CHECKED]:
        if state.rstrip('*~#!%$@^-').startswith(UNAVAILABLE) or state.endswith('*'):
            checks.warn('%s is %s, so it was not checked' % (name, state))
            continue
        command = ['srun', '--nodes=1', '--ntasks=1', '--cpus-per-task=1', '--mem=2G',
                   '--time=10', '--immediate=120', '--nodelist=%s' % name,
                   '--job-name=amprepo-check', '--chdir=%s' % check_dir]
        if executor.get('partition'):
            command.append('--partition=%s' % executor['partition'])
        command += list(executor.get('sbatch_args') or []) + ['bash', script_path]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as error:
            checks.warn('could not check %s: %s' % (name, error))
            continue
        lines = [l[len(CHECK_MARKER):] for l in result.stdout.splitlines() if l.startswith(CHECK_MARKER)]
        if not lines:
            checks.warn('could not check %s (busy, or the job did not start): %s'
                        % (name, (result.stderr or result.stdout).strip()[-500:]))
            continue
        for line in lines:
            kind, _, message = line.partition(' ')
            (checks.ok if kind == 'ok' else checks.fail)('node ' + message)
