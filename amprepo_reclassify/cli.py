"""The subcommands. Only `upload`, which isn't built yet, would write to a site."""

import argparse
import os
import sys

from . import environment, pins, pipeline, plan as plan_module, report
from .machine import DEFAULT_PATH, Machine, NotSetUp
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


def cmd_setup(args):
    machine = Machine(workdir=os.path.abspath(args.workdir),
                      jobs=_ask_int(args.jobs, 'AC --jobs for this machine'),
                      bfb_threads=_ask_int(args.bfb_threads, 'AC --bfb_threads for this machine'),
                      token_files=_token_files(args.token_file))
    environment.install(machine)
    machine.save(args.config)
    print('Saved %s. Next: `plan`, then `preflight`.' % args.config)
    print(environment.describe(environment.probe(machine)))
    return 0


def _site(machine, name):
    return Site(name, token=machine.token(name))


def cmd_plan(args):
    machine = Machine.load(args.config)
    wanted = [plan_module.wanted_from_argument(p) for p in args.project or []]
    if args.projects:
        wanted += plan_module.read_project_file(args.projects)
    if not wanted:
        raise SystemExit('Name projects with --projects FILE or --project ID_OR_NAME.')
    plan = plan_module.build_plan(wanted, _site(machine, args.site))
    plan_module.save_plan(plan, machine.plan_path)
    print(plan_module.format_plan(plan))
    print('\nSaved to %s' % machine.plan_path)
    return 0


def cmd_preflight(args):
    failed = environment.preflight(Machine.load(args.config))
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
    tools = environment.tools_for(machine)
    print('Tools: %s' % tools.description)
    failures = pipeline.run_plan(plan, machine, tools, _site(machine, plan['site']),
                                 only=args.only, allow_untested_tool=args.allow_untested_tool)
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
    setup.set_defaults(func=cmd_setup)

    plan = commands.add_parser('plan', help='the dry run: what would be updated')
    plan.add_argument('--site', choices=sorted(SITES), default='prod')
    plan.add_argument('--projects', help='TSV with project_name and/or latest_id columns')
    plan.add_argument('--project', action='append', help='a project id or exact name; repeatable')
    plan.set_defaults(func=cmd_plan)

    preflight = commands.add_parser('preflight', help='check tools, solver, references, disk and site')
    preflight.set_defaults(func=cmd_preflight)

    run = commands.add_parser('run', help='download, AC, rebuild, check and report; resumable')
    run.add_argument('--only', action='append', help='a project name or id from the plan; repeatable')
    run.add_argument('--allow-untested-tool', action='store_true',
                     help='also run projects flagged untested-tool (CoRAL results)')
    run.set_defaults(func=cmd_run)

    summary = commands.add_parser('summary', help='combined change summary for review')
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (NotSetUp, SiteError, environment.ToolProblem, OSError) as error:
        print('ERROR: %s' % error, file=sys.stderr)
        return 1
