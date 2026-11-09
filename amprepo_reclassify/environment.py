"""`setup` and `preflight`: the pinned tools on this machine, and whether a run can start."""

import dataclasses
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.request

from . import pins, plan as plan_module
from .site_api import Site, SiteError

# Runs inside the tool environment. -I keeps the current directory off its import path. AC isn't
# installed there: it runs from a source tree, read by `read_ac_source`.
PROBE = r"""
import json
import AmpliconSuiteAggregator, bfbarchitect, gurobipy
from bfbarchitect.BFBArchitect import detect_solver
print('TOOLS ' + json.dumps({
    'bfbarchitect': bfbarchitect.__version__,
    'solver': detect_solver(),
    'gurobi': '.'.join(map(str, gurobipy.gurobi.version()[:3])),
    'aggregator': AmpliconSuiteAggregator.__version__,
}))
"""

AC_VERSION_LINE = re.compile(r"""__ampliconclassifier_version__\s*=\s*['"]([^'"]+)['"]""")
# Variables that would put other code in front of the environment's.
UNSET = ('PYTHONPATH', 'PYTHONHOME')


class ToolProblem(Exception):
    pass


@dataclasses.dataclass
class Tools:
    """How to call AC and the aggregator, and what they are."""
    ac_cmd: list
    asa_cmd: list
    description: str
    # AC's own make_input.sh, which lists the graph and cycles pairs AC would classify.
    make_input_cmd: list = None
    path_prefix: str = ''
    # The AC tree's commit and each tracked file's sha256, so a node without git can show it has
    # the same code.
    ac_commit: str = None
    ac_files: dict = None

    def env(self):
        return tool_env(self.path_prefix)


def tool_env(path_prefix=''):
    env = {k: v for k, v in os.environ.items() if k not in UNSET}
    env['PYTHONNOUSERSITE'] = '1'
    # AC only writes plots to files. Without this, matplotlib picks its backend from whatever
    # display a job inherits: on 2026-11-09, Slurm jobs on oreo and acamas had WAYLAND_DISPLAY
    # but no DISPLAY, so it chose Tk, which failed, and all 1,587 of their BFB plots were lost.
    env['MPLBACKEND'] = 'Agg'
    if path_prefix:
        env['PATH'] = path_prefix + os.pathsep + env.get('PATH', '')
    return env


def probe(machine):
    """What's installed in the environment. Raises ToolProblem if it can't be read."""
    if machine.conda_env and not machine.conda_sh:
        raise ToolProblem('the conda environment %s is set but not conda.sh. Run `setup` again.'
                          % machine.conda_env)
    if not machine.conda_env and not os.path.exists(machine.python):
        raise ToolProblem('no Python at %s. Run `setup`.' % machine.python)
    where = ('the conda environment %s' % machine.conda_env if machine.conda_env
             else 'the environment of %s' % machine.python)
    result = subprocess.run(machine.tool_prefix() + [machine.python, '-I', '-c', PROBE],
                            capture_output=True, text=True, env=tool_env())
    found = [line[6:] for line in result.stdout.splitlines() if line.startswith('TOOLS ')]
    if result.returncode or not found:
        raise ToolProblem('could not read the tools in %s:\n%s%s' % (where, result.stdout, result.stderr))
    return json.loads(found[0])


def ac_version(ac_src):
    path = os.path.join(ac_src, 'ampclasslib', '_version.py')
    try:
        with open(path) as handle:
            match = AC_VERSION_LINE.search(handle.read())
    except OSError:
        raise ToolProblem('no AC at %s: %s is missing' % (ac_src, path))
    if not match:
        raise ToolProblem('%s does not say which AC version it is' % path)
    return match.group(1)


def file_hashes(root, names):
    """Each file's sha256, or None for one that isn't there."""
    hashes = {}
    for name in names:
        path = os.path.join(root, name)
        if os.path.islink(path):
            content = ('symlink to ' + os.readlink(path)).encode()
        else:
            try:
                with open(path, 'rb') as handle:
                    content = handle.read()
            except FileNotFoundError:
                hashes[name] = None
                continue
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashes


def _git(ac_src, *args):
    try:
        result = subprocess.run(['git', '-C', ac_src] + list(args), capture_output=True, text=True)
    except FileNotFoundError:
        raise ToolProblem('git is needed to check the AC checkout at %s' % ac_src)
    if result.returncode:
        raise ToolProblem('git could not read %s: %s' % (ac_src, result.stderr.strip()))
    return result.stdout


def read_ac_source(ac_src):
    """The AC tree at ac_src: version, commit, uncommitted changes and file hashes. Uses git."""
    if not ac_src:
        raise ToolProblem('no AC checkout is set for this machine. Run `setup` again.')
    version = ac_version(ac_src)
    names = [n for n in _git(ac_src, 'ls-files', '-z').split('\0') if n]
    return {'ac': version,
            'ac_commit': _git(ac_src, 'rev-parse', 'HEAD').strip(),
            'ac_changed': [line[3:] for line in
                           _git(ac_src, 'status', '--porcelain', '--untracked-files=no').splitlines()],
            'ac_files': file_hashes(ac_src, names)}


def inspect(machine):
    """Everything `version_problems` and `describe` read, on this machine."""
    found = probe(machine)
    found.update(read_ac_source(machine.ac_src))
    return found


def version_problems(found, aggregator_pin, check_commit=True):
    """Each way the installed tools differ from the pins. Results shouldn't be built on any.
    A node checks AC by its files instead of its commit, since it may have no git."""
    problems = []
    if found['ac'] != pins.AC_VERSION:
        problems.append('AC is %s, not %s' % (found['ac'], pins.AC_VERSION))
    if check_commit and found['ac_commit'] != pins.AC_COMMIT:
        problems.append('AC is from commit %s, not %s (tag %s)'
                        % (found['ac_commit'], pins.AC_COMMIT[:7], pins.AC_TAG))
    if found.get('ac_changed'):
        problems.append('the AC checkout has uncommitted changes to %d file(s), first %s'
                        % (len(found['ac_changed']), found['ac_changed'][0]))
    if found['bfbarchitect'] != pins.BFBARCHITECT:
        problems.append('BFBArchitect is %s, not %s' % (found['bfbarchitect'], pins.BFBARCHITECT))
    if found['gurobi'] != pins.GUROBI:
        problems.append('Gurobi is %s, not %s' % (found['gurobi'], pins.GUROBI))
    if found['aggregator'] != aggregator_pin:
        problems.append('the aggregator is %s, not the site\'s %s' % (found['aggregator'], aggregator_pin))
    if found['solver'] != pins.REQUIRED_SOLVER:
        problems.append('BFBArchitect would use %s, not %s. Check the Gurobi licence.'
                        % (found['solver'], pins.REQUIRED_SOLVER))
    return problems


def describe(found):
    return 'AC %s (%s), BFBArchitect %s, solver %s %s, aggregator %s' % (
        found['ac'], (found['ac_commit'] or '?')[:7], found['bfbarchitect'],
        found['solver'], found['gurobi'], found['aggregator'])


def _tools(machine, found):
    prefix = machine.tool_prefix()
    return Tools(ac_cmd=prefix + [machine.python, machine.ac_script],
                 make_input_cmd=prefix + ['bash', os.path.join(machine.ac_src, 'ampclasslib',
                                                               'make_input.sh')],
                 asa_cmd=prefix + [machine.python, '-I', '-m', 'AmpliconSuiteAggregator'],
                 description=describe(found),
                 path_prefix='' if machine.conda_env else os.path.dirname(machine.python),
                 ac_commit=found['ac_commit'], ac_files=found['ac_files'])


def tools_for(machine):
    """The real tools, refusing if they aren't the pinned ones."""
    found = inspect(machine)
    problems = version_problems(found, pins.aggregator_pin())
    if problems:
        raise ToolProblem('; '.join(problems))
    return _tools(machine, found)


def ac_differences(ac_src, expected_files):
    """The AC files here that differ from the ones checked elsewhere."""
    here = file_hashes(ac_src, expected_files)
    return sorted(name for name, digest in expected_files.items() if here[name] != digest)


def tools_on_node(machine, expected):
    """The tools on a compute node, refusing unless they are the ones the submitting machine
    checked. A node may have no git, so AC is compared by its files' contents."""
    problems = []
    try:
        found = probe(machine)
        found.update(ac=ac_version(machine.ac_src), ac_commit=expected.ac_commit,
                     ac_files=expected.ac_files)
    except ToolProblem as error:
        raise ToolProblem('%s: %s' % (socket.gethostname(), error))
    problems += version_problems(found, pins.aggregator_pin(), check_commit=False)
    differ = ac_differences(machine.ac_src, expected.ac_files)
    if differ:
        problems.append('%d AC file(s) at %s differ from the submitting machine\'s, first %s'
                        % (len(differ), machine.ac_src, differ[0]))
    tools = _tools(machine, found)
    if not problems and tools.description != expected.description:
        problems.append('the tools here are %s, not %s' % (tools.description, expected.description))
    if problems:
        raise ToolProblem('%s: %s' % (socket.gethostname(), '; '.join(problems)))
    return tools


def clone_ac(dest):
    """A checkout of the pinned AC tag. Reuses one already there if it is at the pinned commit."""
    if not os.path.exists(dest):
        subprocess.run(['git', '-c', 'advice.detachedHead=false', 'clone', '--quiet', '--depth', '1',
                        '--branch', pins.AC_TAG, pins.AC_REPO, dest], check=True)
    commit = _git(dest, 'rev-parse', 'HEAD').strip()
    if commit != pins.AC_COMMIT:
        raise ToolProblem('%s is at commit %s, not %s (tag %s)' % (dest, commit, pins.AC_COMMIT[:7], pins.AC_TAG))


def install(machine):
    """Create an environment with AC's dependencies and the pinned tools. Others float, on purpose."""
    if not hasattr(tarfile, 'data_filter'):
        raise ToolProblem('Python %s has no safe tar extraction. Use a current 3.9 or later.'
                          % sys.version.split()[0])
    os.makedirs(machine.workdir, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'venv', machine.env_dir], check=True)
    pip = [machine.python, '-m', 'pip', 'install', '-q']
    subprocess.run(pip + ['--upgrade', 'pip'], check=True)
    subprocess.run(pip + ['-r', os.path.join(machine.ac_src, 'requirements.txt'),
                          'bfbarchitect==%s' % pins.BFBARCHITECT,
                          'gurobipy==%s' % pins.GUROBI,
                          'AmpliconSuiteAggregator==%s' % pins.aggregator_pin()], check=True)


# ---- preflight ----

class Checks:
    def __init__(self):
        self.failed = 0

    def ok(self, message):
        print('  ok    ' + message)

    def warn(self, message):
        print('  WARN  ' + message)

    def fail(self, message):
        print('  FAIL  ' + message)
        self.failed += 1


def check_reference(data_repo, ref):
    """What's wrong with one reference in $AA_DATA_REPO, or None."""
    ref_dir = os.path.join(data_repo, ref)
    file_list = os.path.join(ref_dir, 'file_list.txt')
    if not os.path.exists(file_list):
        return 'no %s' % file_list
    with open(file_list) as handle:
        names = [line.split()[1] for line in handle if len(line.split()) >= 2]
    # Some references name a slot they don't use as `#unused`.
    names = [n for n in names if not n.startswith('#')]
    missing = [n for n in names if not os.path.exists(os.path.join(ref_dir, n))]
    if missing:
        return '%s is missing %d named file(s), first %s' % (ref, len(missing), missing[0])
    return None


def _pypi_aggregator():
    request = urllib.request.Request('https://pypi.org/pypi/AmpliconSuiteAggregator/json',
                                     headers={'User-Agent': 'amprepo_reclassify'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['info']['version']


def _github_ac_release():
    """The tag of AC's latest GitHub release."""
    owner_repo = re.sub(r'^https://github.com/|\.git$', '', pins.AC_REPO)
    request = urllib.request.Request('https://api.github.com/repos/%s/releases/latest' % owner_repo,
                                     headers={'User-Agent': 'amprepo_reclassify',
                                              'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get('tag_name')


def _check_tools(checks, machine):
    try:
        found = inspect(machine)
    except ToolProblem as error:
        checks.fail(str(error))
        return
    problems = version_problems(found, pins.aggregator_pin())
    for problem in problems:
        checks.fail(problem)
    if not problems:
        checks.ok(describe(found))
    try:
        newest = _github_ac_release()
        if newest and newest != pins.AC_TAG:
            checks.warn('GitHub\'s latest AC release is %s and this code pins %s. A new campaign '
                        'starts by updating pins.py.' % (newest, pins.AC_TAG))
    except (OSError, ValueError) as error:
        checks.warn('could not ask GitHub for the latest AC release: %s' % error)
    try:
        latest = _pypi_aggregator()
        if latest != pins.aggregator_pin():
            checks.warn('PyPI has aggregator %s and the site is pinned to %s, so prod hasn\'t got '
                        'it yet. Prod comes first, then reclassifying.' % (latest, pins.aggregator_pin()))
    except OSError as error:
        checks.warn('could not ask PyPI for the latest aggregator: %s' % error)


def _check_threads(checks, machine):
    if machine.executor_kind == 'slurm':
        checks.ok('each Slurm job sets AC --jobs from the CPUs it is given, with --bfb_threads %d'
                  % machine.bfb_threads)
        return
    used, available = machine.jobs * machine.bfb_threads, os.cpu_count() or 0
    message = '--jobs %d --bfb_threads %d use %d of %d threads' % (
        machine.jobs, machine.bfb_threads, used, available)
    if used > available:
        checks.warn(message)
    else:
        checks.ok(message)


def _check_site(checks, machine, site_name):
    try:
        token = machine.token(site_name)
    except OSError as error:
        checks.fail('could not read the %s token file: %s' % (site_name, error))
        return
    try:
        count = len(Site(site_name, token=token).list_projects())
    except SiteError as error:
        checks.fail(str(error))
        return
    checks.ok('%s answers, %d projects visible' % (site_name, count))
    if not token:
        checks.warn('no %s token configured: only public projects are visible, and `upload` '
                    'will need one' % site_name)


def _check_references(checks, entries):
    refs = sorted({e['ac_ref'] for e in entries if e['status'] == plan_module.UPDATE})
    data_repo = os.environ.get('AA_DATA_REPO')
    if not data_repo:
        checks.fail('$AA_DATA_REPO is not set')
        return
    if not os.path.isdir(data_repo):
        checks.fail('$AA_DATA_REPO is %s, which is not a directory' % data_repo)
        return
    for ref in refs:
        problem = check_reference(data_repo, ref)
        if problem:
            checks.fail('%s. Get it with `AmpliconSuite-pipeline.py --download_repo %s`, or from '
                        'refs.ampliconrepository.org/data/module_support_files/AmpliconArchitect/'
                        '%s.tar.gz' % (problem, ref, ref))
        else:
            checks.ok('reference %s' % ref)


def _check_disk(checks, machine, entries):
    needed = plan_module.predicted_disk(entries)
    free = shutil.disk_usage(machine.workdir).free
    message = '%.1f GB free at %s, about %.1f GB needed' % (free / 1e9, machine.workdir, needed / 1e9)
    if free < needed:
        checks.fail(message)
    else:
        checks.ok(message)


def preflight(machine, node_check=None):
    """Print each check. Returns the number that failed. node_check(checks, plan) adds more."""
    checks = Checks()
    plan = plan_module.load_plan(machine.plan_path) if os.path.exists(machine.plan_path) else None
    _check_tools(checks, machine)
    _check_threads(checks, machine)
    _check_site(checks, machine, plan['site'] if plan else 'prod')
    if plan:
        _check_references(checks, plan['projects'])
        _check_disk(checks, machine, plan['projects'])
    else:
        print('  No plan yet, so references and disk are unchecked. Run `plan`, then this again.')
    if node_check:
        node_check(checks, plan)
    return checks.failed
