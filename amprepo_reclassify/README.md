# amprepo_reclassify

Moves projects on the site to a new AmpliconClassifier (AC) release, in bulk. AC runs on your
machine, and a person reviews the changes before anything goes back to the site. The design and its
reasoning are in [docs/ac-update-workflow.md](../docs/ac-update-workflow.md).

**This is for site admins.** Only `upload` writes to a site, and it needs an admin's token. Anyone
else updates a project through its edit page.

## What you need

- Python 3.9 or later, git and gzip
- A Gurobi licence. BFBArchitect moves to another solver if Gurobi fails, and the tool refuses that.
- `$AA_DATA_REPO` with the references your projects use. `preflight` prints how to get a missing one.
- An API token from your site profile. `plan` uses it to see private projects you belong to, and
  `upload` needs it to be a site admin's. Even an admin only sees private projects they belong to.

## Steps

The order is AC on everything, then a person reviews, then the upload. Run these from the top of a
checkout of this repo.

```bash
# Once per machine. Clones AC at the pinned tag and makes an environment with its dependencies
# and the pinned tools, in about a minute.
python -m amprepo_reclassify setup --workdir /big/disk/ac_update \
    --jobs 16 --bfb-threads 2 --token-file prod=~/.amprepo_token

# The dry run. Lists what would be updated, saved to <workdir>/plan.json. The projects are the
# ones on the site's keep-current list, less any --exclude, plus any --include.
python -m amprepo_reclassify plan --site prod --exclude <id or exact name>

# Checks tools, solver, references, disk and the site. Exits 1 if a run shouldn't start.
python -m amprepo_reclassify preflight

# Downloads, runs AC, swaps in its output and checks each project, smallest first.
python -m amprepo_reclassify run

# The combined change summary to review, saved to <workdir>/output/summary.md.
python -m amprepo_reclassify summary

# Optional: what the site would store. Changes nothing. Useful once, before a campaign's uploads.
python -m amprepo_reclassify upload --target prod --dry-run

# Sends each project, smallest first, and checks what the site stored.
python -m amprepo_reclassify upload --target prod
```

New archives and one report per project go in `<workdir>/output/`. Files you can delete after
uploading are in `<workdir>/projects/<id>/`.

**What `run` does to a project:** it downloads and unpacks the site's archive, checks its samples,
and runs AC's own `make_input.sh` on `results/samples/`. If those pairs match the samples
`run.json` names, AC classifies them. The new archive is the site's with its
`results/consolidated_classification/` replaced by AC's output, and nothing else changed: the
site aggregates every archive it takes in, which writes `run.json` again from AC's tables. `run`
aggregates the new archive once here as a check, so a failure shows here rather than on the
site. That copy isn't uploaded.

**Choosing `--jobs` and `--bfb-threads`:** their product shouldn't exceed the machine's threads.
On a 32-thread machine, `--jobs 16 --bfb-threads 2` has worked well. Settings are saved in
`amprepo_reclassify/machine.json`, which git ignores.

**Which AC:** the tag in `pins.py`, run from a source tree rather than a package, because a release
is tagged on GitHub before it reaches any package index. `setup` clones it into the workdir, or
`--ac-src` names a checkout you already have. Either way it has to be at the pinned commit with no
uncommitted changes, and `preflight` warns when GitHub has a newer release than the pin.

**Your own environment instead:** `--conda-env NAME` uses a named conda environment, activated
for each tool, and `--python PATH` uses that interpreter's environment. Either needs AC's
requirements, BFBArchitect, gurobipy and the aggregator at the pinned versions. `preflight` says
which differ.

**Which projects:** by default, every project whose owners ticked "Keep this project's
classifications up to date", read from the site with your admin token. `--exclude` and `--include`
take an id or exact name and can be repeated; the plan and the summary name each one. Private
projects can't opt in. Hidden-public ones can, and your admin token reads them by id.

**Your own list instead,** for a test run: `--projects FILE` or `--project <id or exact name>`.
The file is a TSV with a `project_name` or `latest_id` column, or both. Lines starting with
`#` are comments. An id from an older version still resolves to the latest one. Three optional columns
are for projects that need them: `ref` sets AC's reference, and `extra_ac_flags` adds AC flags
such as `--filter_similar`, for an archive whose AC logs don't record them. `metadata_sheet` is
described under Uploading, and `archive` under "Using your own archive". To use the dev site, use
`plan --site dev` with a `dev=` token file.

**Using your own archive:** an `archive` column gives a local `.tar.gz` to use in place of the
site's download, with its path relative to the list. Use it when the site's archive needs fixing
first: fix it outside this tool, then reclassify the fixed copy. `plan` records the file's size and
sha256, and `run` refuses the file if it has changed since. Switching a project to or from a local
archive starts it again. The summary flags a project built this way. Once the fixed archive is on
the site, later campaigns don't need the column.

**Keep this list out of the repo.** The repo is public, and a list can name private projects.

## Running for a long time

`run` can take many hours, so start it with `nohup` or in `tmux`, and don't put a timeout on it.
If it stops, run it again. Each project keeps a state file, finished steps aren't repeated, and a
project that failed is retried. One project failing doesn't stop the others. `run --only <id or
name>` limits a run to some projects in the plan.

A project stops rather than producing an archive if:

- any amplicon was solved without Gurobi
- AC or the aggregator fails
- the archive isn't laid out as the site's aggregator writes archives today, with
  `results/run.json` and `results/samples/`. All 27 keep-current archives on prod were, on
  2026-11-09.
- the archive has a structural problem: a sample with the same AA graph file in more than one
  place, such as a nested copy of its AA results, AA results that aren't a folder, or an empty AA
  graph or cycles file. Nothing is run. Fix the archive, then run again, with the `archive` column
  if the fix isn't on the site yet.
- AC's input doesn't match `run.json`: an amplicon found twice, a sample `run.json` doesn't
  name, or a sample with graph files but no raw `_cycles.txt`, as in an archive with only AC's
  annotated cycles. Nothing is run.

## Running on a Slurm cluster

If `sbatch` is on this machine, `setup` says so, but projects still run here until you choose
otherwise:

```bash
python -m amprepo_reclassify setup --workdir /shared/disk/ac_update --jobs 16 --bfb-threads 2 \
    --conda-env NAME --executor slurm --cpus 12 --mem 24G \
    --token-file prod=~/.amprepo_token
```

`run` then downloads each project on this machine and submits one job for the rest of it. It
submits every project and leaves the scheduling to Slurm. Every
node needs:

- the workdir and the AC checkout at the same paths, on storage they share
- the environment, set up on each node before the run, unless nodes share a home directory.
  The tool never installs anything on a node.
- `$AA_DATA_REPO`, at the path it has where you run `run`, with the references

Each job checks its node first: the tools against the pins, AC's files against the ones checked
where you ran `run`, the references and the workdir. If anything differs, the project fails with
the reason and nothing runs. `preflight` runs the same check as a short job on each available
node, up to eight. Jobs run a copy of this code in `<workdir>/code/`, so nodes don't need this
checkout, and changing it doesn't change jobs already running.

- **CPUs:** a job runs AC with `--jobs` set to the CPUs it may use divided by `--bfb-threads`.
  Linux counts hardware threads, so a 12-core job on a two-thread-per-core node runs AC with
  `--jobs 12 --bfb-threads 2`.
- **Job names:** each job is named for its project, with anything other than letters, digits,
  `.`, `_` and `-` turned into `_`. Plain `squeue` shows only the first 8 characters of a name;
  `squeue -o "%.8i %.10P %.40j %.2t %.10M %R"` shows 40.
- **`--max-running N`** limits how many project jobs run at once; by default there is no limit.
  Every job is still submitted straight away: the jobs share N names and are submitted with
  `--dependency=singleton`, so Slurm starts a job only when the one before it in its lane has
  finished. Those jobs are named for their lane, so the project name is in the job's comment
  instead (`squeue -o "%.8i %.12j %.40k %.2t %.10M %R"`). Lanes are per user, so the limit covers all your reclassify runs together. Use it when
  a Gurobi licence allows fewer sessions than the cluster has room for. A project solved without
  Gurobi fails either way, as it does when running locally, so it can be run again under a
  lower limit.
- **Which nodes** is Slurm's choice. To keep jobs to some nodes, pass a partition, or
  `--sbatch-arg=--exclude=NODES` for the rest.
- **Other options:** `--partition`, `--time`, `--sbatch-arg` (for example `--account=X`), and
  `--job-setup`, a shell line each job runs before activating the environment.
- **`run` waits** for its jobs and reports each one. If you stop it, the jobs keep running, and
  running it again follows them without submitting them twice. A job's log is
  `<workdir>/projects/<id>/slurm-<job>.out`.
- `run --executor local` runs here instead, with `--jobs` from setup.
- Uploads always run where you run `upload`, never in a job.

## What to check in the summary

`summary` lists these under "Check before uploading". Each one holds its project back from
`upload` until you accept it:

- a sample count that differs between the site and the new archive, aggregated here
- a project built from a local archive rather than the site's
- AC settings that couldn't be carried over: per-sample logs that disagree, a config file that AC
  logged, or an option this tool doesn't know
- a reference that differs from the site's
- a large `run.json`, which may reach the site's 16 MiB record limit, until `upload --dry-run`
  says the site's record fits

Under "Notes" it lists what the review should see but that holds nothing back:

- where AC's settings came from, when that wasn't an AC log: run metadata, which can be older
  than the results, or AC's defaults, with the site's reference
- non-default AC flags carried over from the original run. Flags AC already has on by default,
  such as `--report_complexity` in AC 2.0.1, aren't carried or listed.
- samples that had no CNV BED in the site's archive to begin with

When a new version of this tool changes these checks, `run` reports each built project again from
what it has already built. It doesn't download anything or run AC again, and it leaves a project
alone once its upload has started.

## Uploading

`upload` won't start until AC has finished for every project in the plan, or while the change summary
is older than any archive, and it asks you to type the site's name. If a project can't be built, take
it out of the plan, or name what to send with `--only`. `--target` has to match the plan, because ids
belong to one site. It sends one project at a time. For each one, it:

1. sends the archive with the version it was built from, so a project that has moved on is refused
2. waits for the site to finish making the new version
3. asks the site to store `sample_data`, `ecDNA_context` and `reference_genome`, which a version
   otherwise doesn't have until its page is first opened, and a script can't open it
4. compares the site's sample and feature counts and those fields with the archive, its AC version
   with the target, and, if you ran a dry run, the files stored with the prediction

**Anything that goes wrong stops the batch.** Run `upload` again to carry on: finished projects are
skipped, and a project whose job was still running is followed, not sent again.

Every project gets a new version, even if no classes changed. A project already on the target AC
has no archive to send. Project members stay as they are. You become the new version's creator, and
the audit log records that you did it. Subscribers get the usual email.

A project with something under "Check before uploading", in the summary or the dry run, is skipped
until you pass `--accept-flagged`. Use `--only` to accept just the ones you've looked at.

To send a metadata sheet with a project's update, add a `metadata_sheet` column to the project list.
Its path is relative to the list.

`upload --dry-run` writes `<workdir>/output/dry_run.md`. It sends the copy aggregated here,
because the site's dry run reads `run.json` as sent, without aggregating it first. Each real upload adds a line to
`<workdir>/output/uploads.jsonl` with the new version and the one it replaced. To undo an upload,
delete the new version from the project's page, which makes the one before it current again.

## What the plan's status means

| Status | Meaning | `run` |
|---|---|---|
| `update` | On an older AC | Runs it |
| `current` | Already on the target AC | Skips it |
| `no-archive` | The site has no stored archive to download | Skips it |
| `unknown-ref` | No AC reference for the site's reference name | Skips it. Add a `ref` column. |
| `not-found` | Not visible with this token | Skips it |
| `ambiguous` | Several projects have this name | Skips it. Give the id. |

If a project gets a newer version after `plan`, `run` refuses it and asks you to plan again.

## For the next AC release

Change the versions and the commit in `pins.py`, then run `setup` again. A changed tool version
makes each project start again, apart from its download. So does a change in the AC options
detected from a project's input, for example after a fix to how they are read: a project is kept
only while it was classified with the options detected now. The aggregator isn't in `pins.py`: it comes
from this repo's `requirements.txt`, so it follows what the site runs. A new aggregator goes to PyPI,
then prod, and only then should you reclassify.

## Tests

```bash
python -m pytest amprepo_reclassify
```

Run it from the top of the checkout. The tests are in `amprepo_reclassify/tests/`, apart from the
site's, and use a small synthetic archive and a stubbed AC and aggregator, so they need no Gurobi
licence. This package imports nothing from `caper/`, and its own `pytest.ini` keeps the site's
`conftest.py` out, so the tests need neither Django, a database nor the site's `config.sh`.
