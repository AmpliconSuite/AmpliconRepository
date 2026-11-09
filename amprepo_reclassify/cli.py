"""The subcommands. Only `upload` writes to a site."""

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys

from . import environment, pins, pipeline, plan as plan_module, report, slurm, upload as upload_module
from .machine import DEFAULT_PATH, LOCAL, SLURM, Machine, NotSetUp
from .site_api import SITES, Site, SiteError


def _ask_int(value, prompt):
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit('%s is needed. Pass it as a flag.' % prompt)
    return int(input(prompt + ': '))


def _token_files(pairs):
    files = {}
    for pair in pairs or []:
        site, separator, path = pair.partition('=')
        if not separator or site not in SITES:
            raise SystemExit('--token-file takes prod=PATH or dev=PATH, not %s' % pair)
        files[site] = os.path.abspath(os.path.expanduser(path))
    return files


def _conda_sh(given):
    """conda's shell setup file, from the conda that is running here."""
    if given:
        return os.path.abspath(os.path.expanduser(given))
    base = None
    if os.environ.get('CONDA_EXE'):
        base = os.path.dirname(os.path.dirname(os.environ['CONDA_EXE']))
    elif shutil.which('conda'):
        base = subprocess.run(['conda', 'info', '--base'], capture_output=True, text=True).stdout.strip()
    path = os.path.join(base, 'etc', 'profile.d', 'conda.sh') if base else None
    if not path or not os.path.exists(path):
        raise SystemExit('Could not find conda.sh for --conda-env. Pass --conda-sh.')
    return path


def _executor(args):
    if args.executor != SLURM:
        return {'kind': LOCAL}
    if not args.cpus or not args.mem:
        raise SystemExit('--executor slurm needs --cpus and --mem for each project\'s job.')
    return {'kind': SLURM, 'partition': args.partition, 'cpus': args.cpus, 'mem': args.mem,
            'time': args.time, 'at_once': args.at_once, 'sbatch_args': args.sbatch_arg or [],
            'job_setup': args.job_setup or []}


def cmd_setup(args):
    workdir = os.path.abspath(args.workdir)
    if args.conda_env and args.python:
        raise SystemExit('Pass --conda-env or --python, not both.')
    machine = Machine(workdir=workdir,
                      jobs=_ask_int(args.jobs, 'AC --jobs for this machine'),
                      bfb_threads=_ask_int(args.bfb_threads, 'AC --bfb_threads for this machine'),
                      token_files=_token_files(args.token_file),
                      conda_env=args.conda_env,
                      conda_sh=_conda_sh(args.conda_sh) if args.conda_env else None,
                      python_path=os.path.abspath(args.python) if args.python else None,
                      executor=_executor(args))
    os.makedirs(workdir, exist_ok=True)
    if args.ac_src:
        machine.ac_src = os.path.abspath(os.path.expanduser(args.ac_src))
    else:
        machine.ac_src = os.path.join(workdir, 'AmpliconClassifier-%s' % pins.AC_TAG)
        environment.clone_ac(machine.ac_src)
    if not args.conda_env and not args.python:
        environment.install(machine)
    machine.save(args.config)
    print('Saved %s. Next: `plan`, then `preflight`.' % args.config)
    if machine.executor_kind == LOCAL and shutil.which('sbatch'):
        print('Slurm is available here. To run each project as a Slurm job, run setup again with '
              '--executor slurm --cpus N --mem SIZE, and an environment every node has.')
    print(environment.describe(environment.inspect(machine)))
    return 0


def _site(machine, name):
    return Site(name, token=machine.token(name))


def cmd_plan(args):
    machine = Machine.load(args.config)
    site = _site(machine, args.site)
    wanted = [plan_module.wanted_from_argument(p) for p in args.project or []]
    if args.projects:
        wanted += plan_module.read_project_file(args.projects)
    listed = None
    if not wanted:
        try:
            wanted = plan_module.wanted_from_site(site.keep_current_list())
        except SiteError as error:
            raise SystemExit('Could not read the keep-current list: %s' % error)
        listed = len(wanted)
    wanted += [plan_module.wanted_from_argument(p, plan_module.INCLUDED) for p in args.include or []]
    if not wanted:
        raise SystemExit('Nothing to plan: %s\'s keep-current list is empty and nothing was '
                         'included.' % args.site)
    plan = plan_module.build_plan(wanted, site, exclude=args.exclude or [], listed=listed)
    plan_module.save_plan(plan, machine.plan_path)
    print(plan_module.format_plan(plan))
    print('\nSaved to %s' % machine.plan_path)
    return 0


def _check_nodes(machine):
    def check(checks, plan):
        try:
            tools = environment.tools_for(machine)
        except environment.ToolProblem:
            checks.warn('Slurm nodes not checked until the tools here pass')
            return
        refs = {slurm._reference_for(e) for e in (plan or {}).get('projects', [])
                if e['status'] == plan_module.UPDATE}
        slurm.check_nodes(machine, tools, refs, checks)
    return check


def cmd_preflight(args):
    machine = Machine.load(args.config)
    node_check = _check_nodes(machine) if machine.executor_kind == SLURM else None
    failed = environment.preflight(machine, node_check)
    print('\n%s' % ('%d check(s) failed.' % failed if failed else 'Ready.'))
    return 1 if failed else 0


def _load_plan(machine):
    if not os.path.exists(machine.plan_path):
        raise SystemExit('No plan at %s. Run `plan` first.' % machine.plan_path)
    return plan_module.load_plan(machine.plan_path)


def cmd_run(args):
    machine = Machine.load(args.config)
    plan = _load_plan(machine)
    if plan['target_ac'] != pins.AC_VERSION:
        raise SystemExit('The plan targets AC %s and this code pins %s. Run `plan` again.'
                         % (plan['target_ac'], pins.AC_VERSION))
    kind = args.executor or machine.executor_kind
    tools = environment.tools_for(machine)
    print('Tools: %s' % tools.description)
    site = _site(machine, plan['site'])
    if kind == SLURM:
        failures = slurm.run_plan(plan, machine, tools, site, only=args.only)
    else:
        failures = pipeline.run_plan(plan, dataclasses.replace(machine, executor={'kind': LOCAL}),
                                     tools, site, only=args.only)
    print('\n%d project(s) failed. Run this again to retry them.' % failures if failures
          else '\nFinished. Next: `summary`.')
    return 1 if failures else 0


def cmd_summary(args):
    machine = Machine.load(args.config)
    plan = _load_plan(machine)
    text = report.combined_summary(plan, pipeline.load_states(machine, plan['projects']))
    os.makedirs(machine.output_dir, exist_ok=True)
    path = os.path.join(machine.output_dir, 'summary.md')
    with open(path, 'w') as handle:
        handle.write(text)
    print(text)
    print('Saved to %s' % path)
    return 0


def cmd_upload(args):
    machine = Machine.load(args.config)
    plan = _load_plan(machine)
    # Ids belong to one site, and saying which is the point of this flag.
    if args.target != plan['site']:
        raise SystemExit('The plan is for %s, not %s. Run `plan --site %s` first.'
                         % (plan['site'], args.target, args.target))
    site = _site(machine, args.target)
    if not site.token:
        raise SystemExit('`upload` needs a site admin\'s token for %s. Add it with `setup --token-file`.'
                         % args.target)
    if args.dry_run:
        return 1 if upload_module.run_dry_runs(plan, machine, site, only=args.only) else 0
    upload_module.run_uploads(plan, machine, site, only=args.only,
                              accept_flagged=args.accept_flagged, assume_yes=args.yes)
    return 0


def cmd_job(args):
    return slurm.check_job(args.spec) if args.check else slurm.run_job(args.spec)


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m amprepo_reclassify', description=__doc__)
    parser.add_argument('--config', default=DEFAULT_PATH, help='this machine\'s settings file')
    commands = parser.add_subparsers(dest='command', required=True)

    setup = commands.add_parser('setup', help='install the pinned tools; save this machine\'s settings')
    setup.add_argument('--workdir', required=True, help='for the environment, work files and output')
    setup.add_argument('--jobs', type=int, help='AC --jobs on this machine')
    setup.add_argument('--bfb-threads', type=int, help='AC --bfb_threads on this machine')
    setup.add_argument('--token-file', action='append', metavar='SITE=PATH',
                       help='file holding an API token, for example prod=~/.amprepo_token')
    setup.add_argument('--ac-src', help='an AC checkout at the pinned tag to use, instead of '
                       'cloning one into the workdir')
    setup.add_argument('--conda-env', help='a named conda environment with AC\'s dependencies and '
                       'the pinned tools, instead of making one in the workdir')
    setup.add_argument('--conda-sh', help='conda\'s etc/profile.d/conda.sh, if setup can\'t find it')
    setup.add_argument('--python', help='an interpreter whose environment has the tools, instead '
                       'of making one')
    setup.add_argument('--executor', choices=[LOCAL, SLURM], default=LOCAL,
                       help='run projects here, or one Slurm job each')
    slurm_options = setup.add_argument_group('with --executor slurm')
    slurm_options.add_argument('--partition', help='the Slurm partition, if not the default')
    slurm_options.add_argument('--cpus', type=int, help='CPUs per project job (--cpus-per-task)')
    slurm_options.add_argument('--mem', help='memory per project job, for example 24G')
    slurm_options.add_argument('--time', help='time limit per project job, for example 2-00:00:00')
    slurm_options.add_argument('--at-once', type=int, default=2,
                               help='most project jobs in the queue at once, for the Gurobi '
                               'licence (default 2; 0 for no limit)')
    slurm_options.add_argument('--sbatch-arg', action='append',
                               help='another sbatch option, such as --account=X; repeatable')
    slurm_options.add_argument('--job-setup', action='append',
                               help='a shell line each job runs before activating the '
                               'environment; repeatable')
    setup.set_defaults(func=cmd_setup)

    plan = commands.add_parser('plan', help='the dry run: what would be updated')
    plan.add_argument('--site', choices=sorted(SITES), default='prod')
    plan.add_argument('--projects', help='TSV with project_name and/or latest_id columns, '
                      'used instead of the site\'s keep-current list')
    plan.add_argument('--project', action='append',
                      help='a project id or exact name, instead of the site\'s list; repeatable')
    plan.add_argument('--include', action='append',
                      help='a project id or exact name to add to the list; repeatable')
    plan.add_argument('--exclude', action='append',
                      help='a project id or exact name to leave out; repeatable')
    plan.set_defaults(func=cmd_plan)

    preflight = commands.add_parser('preflight', help='check tools, solver, references, disk and site')
    preflight.set_defaults(func=cmd_preflight)

    run = commands.add_parser('run', help='download, AC, rebuild, check and report; resumable')
    run.add_argument('--only', action='append', help='a project name or id from the plan; repeatable')
    run.add_argument('--executor', choices=[LOCAL, SLURM],
                     help='override the machine\'s setting, for example to run here')
    run.set_defaults(func=cmd_run)

    summary = commands.add_parser('summary', help='combined change summary for review')
    summary.set_defaults(func=cmd_summary)

    upload = commands.add_parser('upload', help='send reviewed archives to the site; site admins only')
    upload.add_argument('--target', choices=sorted(SITES), required=True,
                        help='the site to send to, which must be the plan\'s')
    upload.add_argument('--dry-run', action='store_true',
                        help='ask the site what each upload would store, and change nothing')
    upload.add_argument('--only', action='append', help='a project name or id from the plan; repeatable')
    upload.add_argument('--accept-flagged', action='store_true',
                        help='also send projects with something under "Check before uploading"')
    upload.add_argument('--yes', action='store_true', help='don\'t ask for confirmation')
    upload.set_defaults(func=cmd_upload)

    job = commands.add_parser('job', help='what each Slurm job runs; not for running by hand')
    job.add_argument('spec')
    job.add_argument('--check', action='store_true', help='check this node only')
    job.set_defaults(func=cmd_job)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (NotSetUp, SiteError, environment.ToolProblem, slurm.SlurmError, OSError) as error:
        print('ERROR: %s' % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
