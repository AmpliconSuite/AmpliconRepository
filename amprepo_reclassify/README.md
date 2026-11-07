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

# Optional: what the site would store. Changes nothing. Useful once, before a campaign's uploads.
python -m amprepo_reclassify upload --target prod --dry-run

# Sends each project, smallest first, and checks what the site stored.
python -m amprepo_reclassify upload --target prod
```

Rebuilt archives and one report per project go in `<workdir>/output/`. Files you can delete after
uploading are in `<workdir>/projects/<id>/`.

**Choosing `--jobs` and `--bfb-threads`:** their product shouldn't exceed the machine's threads.
On a 32-thread machine, `--jobs 16 --bfb-threads 2` has worked well. Settings are saved in
`amprepo_reclassify/machine.json`, which git ignores.

**Project list:** a TSV with a `project_name` or `latest_id` column, or both. Lines starting with
`#` are comments. An id from an older version still resolves to the latest one. Three optional columns
are for projects that need them: `ref` sets AC's reference, and `extra_ac_flags` adds AC flags
such as `--filter_similar`. `metadata_sheet` is described under Uploading, and `archive` under
"Using your own archive". For one project, use `plan --project <id or exact name>`. To use the
dev site, use `plan --site dev` with a `dev=` token file.

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
- AC finds no usable input, which happens when an archive has only annotated cycles files
- the archive has a structural problem: a sample with the same AA graph file in more than one
  place, such as a nested copy of its AA results, or an empty AA graph or cycles file. Nothing is
  run. Fix the archive, then run again, with the `archive` column if the fix isn't on the site yet.
- the rebuilt archive lacks a file that the input has, apart from the old AC results it replaces.
  Staging carries each sample's other files, and folders marked with an `AUX_DIR` file are passed
  to the aggregator, so nothing should be lost.

## What to check in the summary

`summary` lists these under "Check before uploading":

- a sample count that differs between the site and the rebuilt archive
- plots or data files that the rebuild dropped
- a project built from a local archive rather than the site's
- files that the aggregator's plain repack of the site's archive dropped, which says the stored
  archive is unusual
- non-default AC flags recovered from an archive, or a config file that AC logged
- a reference that differs from the site's
- settings that came from run metadata rather than an AC log, which can be older than the results
- a large `run.json`, which may reach the site's 16 MiB record limit

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

`upload --dry-run` writes `<workdir>/output/dry_run.md`. Each real upload adds a line to
`<workdir>/output/uploads.jsonl` with the new version and the one it replaced. To undo an upload,
delete the new version from the project's page, which makes the one before it current again.

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
