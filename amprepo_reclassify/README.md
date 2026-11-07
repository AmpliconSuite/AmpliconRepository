# amprepo_reclassify

Moves projects on the site to a new AmpliconClassifier (AC) release. AC runs on your machine, and
a person reviews the changes before anything goes back to the site. The design and its reasoning
are in [docs/ac-update-workflow.md](../docs/ac-update-workflow.md).

`upload` isn't built yet, so nothing here can write to a site. Every command reads from the site's
API, or works locally.

## What you need

- Python 3.9 or later, git and gzip
- A Gurobi licence. BFBArchitect moves to another solver if Gurobi fails, and the tool refuses that.
- `$AA_DATA_REPO` with the references your projects use. `preflight` prints how to get a missing one.
- An API token from your site profile, to see private or hidden projects you belong to

## Steps

Run these from the top of a checkout of this repo.

```bash
# Once per machine. Creates an environment with the pinned tools, in about 20 seconds.
python -m amprepo_reclassify setup --workdir /big/disk/ac_update \
    --jobs 16 --bfb-threads 2 --token-file prod=~/.amprepo_token

# The dry run. Lists what would be updated, saved to <workdir>/plan.json.
python -m amprepo_reclassify plan --projects my_projects.tsv

# Checks tools, solver, references, disk and the site. Exits 1 if a run shouldn't start.
python -m amprepo_reclassify preflight

# Downloads, runs AC, rebuilds and checks each project, smallest first.
python -m amprepo_reclassify run

# The combined change summary to review, saved to <workdir>/output/summary.md.
python -m amprepo_reclassify summary
```

Rebuilt archives and one report per project go in `<workdir>/output/`. Files you can delete after
uploading are in `<workdir>/projects/<id>/`.

**Choosing `--jobs` and `--bfb-threads`:** their product shouldn't exceed the machine's threads.
On a 32-thread machine, `--jobs 16 --bfb-threads 2` has worked well. Settings are saved in
`amprepo_reclassify/machine.json`, which git ignores.

**Project list:** a TSV with a `project_name` or `latest_id` column, or both. Lines starting with
`#` are comments. An id from an older version still resolves to the latest one. Two optional columns
are for projects that need them: `ref` sets AC's reference, and `extra_ac_flags` adds AC flags
such as `--filter_similar`. For one project, use `plan --project <id or exact name>`. To use the
dev site, use `plan --site dev` with a `dev=` token file.

**Keep this list out of the repo.** The repo is public, and a list can name private projects.

## Running for a long time

`run` can take many hours, so start it with `nohup` or in `tmux`, and don't put a timeout on it.
If it stops, run it again. Each project keeps a state file, finished steps aren't repeated, and a
project that failed is retried. One project failing doesn't stop the others. `run --only <id or
name>` limits a run to some projects in the plan.

A project stops rather than producing an archive if:

- any amplicon was solved without Gurobi
- AC or the aggregator fails
- AC finds no usable input, which happens when an archive has only annotated cycles files

## What to check in the summary

`summary` lists these under "Check before uploading":

- a sample count that differs between the site and the rebuilt archive
- plots or data files that the rebuild dropped
- non-default AC flags recovered from an archive, or a config file that AC logged
- a reference that differs from the site's
- settings that came from run metadata rather than an AC log, which can be older than the results
- a large `run.json`, which may reach the site's 16 MiB record limit

## What the plan's status means

| Status | Meaning | `run` |
|---|---|---|
| `update` | On an older AC | Runs it |
| `current` | Already on the target AC | Skips it |
| `untested-tool` | CoRAL results, which haven't been through this workflow | Skips it, unless you pass `--allow-untested-tool` |
| `no-archive` | The site has no stored archive to download | Skips it |
| `unknown-ref` | No AC reference for the site's reference name | Skips it. Add a `ref` column. |
| `not-found` | Not visible with this token | Skips it |
| `ambiguous` | Several projects have this name | Skips it. Give the id. |

If a project gets a newer version after `plan`, `run` refuses it and asks you to plan again.

## For the next AC release

Change the versions and the commit in `pins.py`, then run `setup` again. A changed tool version
makes each project start again, apart from its download. The aggregator isn't in `pins.py`: it comes
from this repo's `requirements.txt`, so it follows what the site runs. A new aggregator goes to PyPI,
then prod, and only then should you reclassify.

## Tests

`pytest tests/test_amprepo_reclassify.py` uses a small synthetic archive and a stubbed AC and
aggregator, so it needs no Gurobi licence. This package imports nothing from `caper/`.
