"""`setup` and `preflight`: the pinned tools on this machine, and whether a run can start."""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from . import pins, plan as plan_module
from .site_api import Site, SiteError

# Runs inside the tool environment. -I keeps the current directory off its import path.
PROBE = r'''
import importlib.metadata as metadata, json
import AmpliconSuiteAggregator, bfbarchitect, gurobipy
from bfbarchitect.BFBArchitect import detect_solver
ac = metadata.distribution('AmpliconClassifier')
direct = json.loads(ac.read_text('direct_url.json') or '{}')
print('TOOLS ' + json.dumps({
    'ac': ac.version,
    'ac_commit': direct.get('vcs_info', {}).get('commit_id'),
    'bfbarchitect': bfbarchitect.__version__,
    'solver': detect_solver(),
    'gurobi': '.'.join(map(str, gurobipy.gurobi.version()[:3])),
    'aggregator': AmpliconSuiteAggregator.__version__,
}))
'''


class ToolProblem(Exception):
    pass


@dataclasses.dataclass
class Tools:
    """How to call AC and the aggregator, and what they are."""
    ac_cmd: list
    asa_cmd: list
    description: str
    path_prefix: str = ''

    def env(self):
        env = dict(os.environ)
        if self.path_prefix:
            env['PATH'] = self.path_prefix + os.pathsep + env.get('PATH', '')
        return env


def probe(machine):
    """What's installed in the environment. Raises ToolProblem if it can't be read."""
    if not os.path.exists(machine.python):
        raise ToolProblem('no environment at %s. Run `setup`.' % machine.env_dir)
    result = subprocess.run([machine.python, '-I', '-c', PROBE], capture_output=True, text=True)
    found = [line[6:] for line in result.stdout.splitlines() if line.startswith('TOOLS ')]
    if result.returncode or not found:
        raise ToolProblem('could not read the tools in %s:\n%s%s'
                          % (machine.env_dir, result.stdout, result.stderr))
    return json.loads(found[0])


def version_problems(found, aggregator_pin):
    """Each way the installed tools differ from the pins. Results shouldn't be built on any."""
    problems = []
    if found['ac'] != pins.AC_VERSION:
        problems.append('AC is %s, not %s' % (found['ac'], pins.AC_VERSION))
    if found['ac_commit'] != pins.AC_COMMIT:
        problems.append('AC is from commit %s, not %s (tag %s)'
                        % (found['ac_commit'], pins.AC_COMMIT[:7], pins.AC_TAG))
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


def tools_for(machine):
    """The real tools, refusing if they aren't the pinned ones."""
    found = probe(machine)
    problems = version_problems(found, pins.aggregator_pin())
    if problems:
        raise ToolProblem('; '.join(problems))
    return Tools(ac_cmd=[machine.python, machine.ac_script],
                 asa_cmd=[machine.python, '-I', '-m', 'AmpliconSuiteAggregator'],
                 description=describe(found),
                 path_prefix=os.path.dirname(machine.python))


def install(machine):
    """Create the environment with the pinned tools. Other packages float, on purpose."""
    if not hasattr(tarfile, 'data_filter'):
        raise ToolProblem('Python %s has no safe tar extraction. Use a current 3.9 or later.'
                          % sys.version.split()[0])
    os.makedirs(machine.workdir, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'venv', machine.env_dir], check=True)
    pip = [machine.python, '-m', 'pip', 'install', '-q']
    subprocess.run(pip + ['--upgrade', 'pip'], check=True)
    subprocess.run(pip + ['AmpliconClassifier @ git+%s@%s' % (pins.AC_REPO, pins.AC_TAG),
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


def _check_tools(checks, machine):
    try:
        found = probe(machine)
    except ToolProblem as error:
        checks.fail(str(error))
        return
    problems = version_problems(found, pins.aggregator_pin())
    for problem in problems:
        checks.fail(problem)
    if not problems:
        checks.ok(describe(found))
    try:
        latest = _pypi_aggregator()
        if latest != pins.aggregator_pin():
            checks.warn('PyPI has aggregator %s and the site is pinned to %s, so prod hasn\'t got '
                        'it yet. Prod comes first, then reclassifying.' % (latest, pins.aggregator_pin()))
    except OSError as error:
        checks.warn('could not ask PyPI for the latest aggregator: %s' % error)


def _check_threads(checks, machine):
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


def preflight(machine):
    """Print each check. Returns the number that failed."""
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
    return checks.failed
