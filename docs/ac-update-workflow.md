# `amprepo_reclassify`: updating site projects to a new AmpliconClassifier release

Status: written 2026-11-06. Every stage is built, in
[`amprepo_reclassify/`](../amprepo_reclassify/README.md), and so is the site's route. The
keep-current checkbox and the list `plan` reads from it were added 2026-11-07. CUGA was updated on
prod with this on 2026-11-07, from a local list.

## Goal

When AmpliconClassifier (AC) has a new release, a site admin with a licence and a
token can move the projects we keep current onto it, from any machine, without storage
growing out of proportion to what actually changed.

AC runs on the operator's machine, not on the site. Results go back to the site as
a new version of the same project, and a person reviews a change summary before
anything is uploaded.

## What exists today

A lab-internal script, `reclassify.py`, downloads a project's archive, reruns AC,
rebuilds the archive with the aggregator, checks nothing was dropped, and writes a
change report. A person then uploads each archive through the edit form. It works on
one machine, with hard-coded paths and a named conda environment.

## Stages

Each stage is one subcommand. Only `upload` writes to a site. The order is AC on everything, a
person reviews, then the upload: `upload` refuses while any project in the plan is unbuilt, unless
`--only` names what to send.

| Stage | Does | Writes to |
|---|---|---|
| `setup` | Clones AC at the pinned tag; makes an environment with the pinned tools, or names one; records threading and where projects run | local |
| `plan` | The dry run: lists each project's latest version, reference, archive size and expected work | local |
| `preflight` | Checks tools, solver, `$AA_DATA_REPO`, disk, token and site | nothing |
| `run` | Download, check AC's input, AC, swap in AC's output, aggregate as a check, report, per project, resumable | local |
| `summary` | Combined change summary, record-size check and predicted storage for review | local |
| `upload --target dev\|prod` | Uploads reviewed archives, smallest first, and verifies each | the site |

Testing a new release follows the same stages: `run` and `upload --target dev` on one
or two small dev projects, a person checks them, and only then the end-to-end run for
prod. Every prod upload batch is its own explicit ask, as with any prod write.

## Decisions

**Install AC, not AmpliconSuite-pipeline.** The pipeline adds alignment and
AmpliconArchitect, which this never runs. On 2026-11-06, a clean virtual environment
with AC from its `v2.0.1` tag, the aggregator and `gurobipy` installed in 21 seconds
(1.2 GB) and ran 6 samples with Gurobi. AC isn't on PyPI, so it installs from the git
tag. BFBArchitect and the aggregator are on PyPI.

**Run AC from a source tree at the pinned tag (changed 2026-11-08).** A release is tagged
on GitHub before it reaches conda, and AC isn't on PyPI, so AC isn't installed into the
environment at all: `setup` clones the tag, or `--ac-src` names a checkout. The
environment holds AC's requirements and the other pinned tools, which are all on PyPI,
so it can be an ordinary named conda environment that a cluster's own tooling copies
between nodes. Where `run` starts, git checks the checkout's commit and that it has no
uncommitted changes. A node may have no git, so each job compares the sha256 of every
tracked AC file with the submitting machine's instead. `preflight` warns when GitHub's
latest release is newer than the pin. The pin itself changes by hand, per campaign,
because one campaign has to run one AC.

**Slurm is optional, one job per project.** Without `--executor slurm` nothing changes.
With it, `run` downloads each archive where it runs, so nodes need no network or token,
and submits a job for the rest of the project's steps. Nodes need the workdir and the
AC checkout on shared storage at the same paths, and the environment set up by hand
before the run; the tool never installs on a node. Jobs run a copy of this package in
the workdir, because a node may not see the checkout and a long run shouldn't change
code under running jobs. A job sets AC's `--jobs` from the CPUs it may use
(`sched_getaffinity`), because some clusters allocate cores while Linux and AC count
hardware threads. `--at-once` caps concurrent project jobs for the Gurobi licence;
the safe value depends on the licence and is measured per cluster. Uploads never run
in a job.

**`preflight` checks `$AA_DATA_REPO` rather than installing references.** The variable
must be set and hold every reference the project list needs, each with its
`file_list.txt` and the files it names. The tool doesn't fetch them: a missing
reference is a one-time job, and `preflight` prints the command for it,
`AmpliconSuite-pipeline.py --download_repo <ref>`. AC's README documents
`refs.ampliconrepository.org/data/module_support_files/AmpliconArchitect/<ref>.tar.gz`
as the same data, for a machine without the pipeline. Each is 1.1 to 1.2 GB (measured
2026-11-06). The 29 projects listed on 2026-11-06 use GRCh37, GRCh38, GRCh38_viral, hg19
and mm10.

**Pin AC, BFBArchitect and Gurobi; leave the rest unpinned.** The clean install above
picked up newer numpy, pandas and matplotlib than the lab's conda environment. Calls and
gene lists matched, 16 values in the classification profiles differed at about the 16th
decimal, and a BFB plot differed byte for byte. That level of difference is acceptable,
so a full lockfile isn't needed. It may cost some storage between versions made on
different machines, because file sharing matches on exact bytes.

**Threading is per-machine configuration.** `setup` asks for AC's `--jobs` and
`--bfb_threads` and stores them in the machine's ignored config file (on a 32-thread machine,
`--jobs 16 --bfb_threads 2`). `preflight` warns if their product exceeds the machine's
threads.

**The new archive is the site's, with only its AC folder replaced.** A site archive is
already aggregated, and the site aggregates whatever it takes in again. So `run` doesn't
rebuild anything: it replaces `results/consolidated_classification/` with AC's new output
and leaves every other file as it was. The aggregator writes `run.json` from AC's result
tables and never reads the old one. On 2026-11-09, for all 20 projects then built, the
aggregator's output from the swapped archive was identical, file by file and byte by byte,
to its output from an archive the old tool had rebuilt from parts.

**AC's input is checked before AC runs.** `run` runs AC's own `make_input.sh` on
`results/samples/` and compares its pairs with the samples `run.json` names: an amplicon
found twice, a sample `run.json` doesn't name, or a sample with graph files but no raw
cycles stops the project. Then AC classifies that `AC.input` with `-i`. On all 25 unpacked
keep-current archives on 2026-11-09 its pairs were the same as the old per-sample staging's.

**The local aggregator comes from PyPI, at the site's version.** The site runs the
aggregator on every upload, so the site decides how the stored project is packed. `run`
aggregates the new archive once as a check, so a failure shows on this machine rather than
as a failed version on the site. That output isn't uploaded, but three things use it: the
sample count and record-size checks read its `run.json`, the site's dry run is sent it
because the dry run reads `run.json` without aggregating, and the upload compares what the
site stored with it. `setup`
installs the version pinned in this repo's `requirements.txt`, which was 8.3.1 on both
servers and the latest on PyPI on 2026-11-06. A new aggregator goes to PyPI, then prod,
and reclassifying comes after that, so the two match in practice. `preflight` warns if
PyPI is ahead of the pin, which means prod hasn't got the new version yet.

**Assume AC's default configuration.** At `v2.0.1`, AC rewrites its default config file
if it differs from the shipped values, so non-default values can only come from flags or
`--config`. Flags are logged. `--config` logs the file's path but not its values, which
is an accepted risk. It's a smaller risk for the two stringency settings: `min_flow` and
`decomposition_strictness` in a config file have no effect at `v2.0.1`, because the
command-line defaults override them (tested 2026-11-06). A log line of `Configuration loaded from: default config` is the
normal case, and `reclassify.py`'s warning wrongly fires on it, which is why Tang's
report carried one. The tool warns only when the line names a file.

**Require Gurobi by default.** AC can't request a solver. BFBArchitect autodetects one
and moves to MOSEK or CBC if Gurobi fails, including mid-run. `preflight` refuses if
Gurobi wouldn't be chosen, and `run` stops a project if a fallback appears in its
logs. Both were tested on 2026-11-06, with a missing licence. Whether a CBC-only
machine should ever be allowed depends on comparing the two solvers' results, which
hasn't been done.

**The code lives in this repo,** in a top-level `amprepo_reclassify/` directory with its
own requirements, importing nothing from `caper/`. The site's upload endpoint and this
client change together, which is the reason to keep them in one place. The repo is
public, so project lists, tokens and machine paths stay out of it: the project list
comes from the site, and per-machine settings go in an ignored config file.

**The list of projects comes from the site.** The keep-current checkbox is the index.
With no list given, `plan` reads it from `upload_api/keep-current/` with the operator's
admin token, and `--exclude` and `--include` (an id or exact name, repeatable) make the
exceptions for one run. The plan and the change summary both say how many came from the
site's list and name each exception, and an `--exclude` that matches nothing is said, not
ignored. The list holds public and hidden-public projects. A hidden-public project isn't in
the API's project listing unless the caller is a member, so `plan` and `run` ask for it by
id, which an admin token can read. A project on the list that the token still can't read is
marked `no-access` rather than left out without a word.
`--projects FILE` or `--project` still give a list instead, for a test run. Version ids
change with every update, so `plan` looks them up,
and an id from an older version resolves to the latest one. The plan is saved locally for
`preflight` and `run`, and `run` refuses a project that has moved on since.

**`plan` doesn't show recovered AC settings.** They're only in the archive, so they need a
download. They appear in each project's report and in `summary`, where any non-default
flag is flagged before upload. `plan` gets sizes with a one-byte read of the stored
archive, because the API has no size field. On 2026-11-06 that took 28 seconds for all 29
projects, and each had a stored archive.

**The tool identifies itself.** Its API calls use the User-Agent
`amprepo_reclassify/<version>`, which both sites accepted on 2026-11-06, where the
lab script sent a browser's. The token goes only to the site, never to the storage URL the
site redirects to.

**Some things stop a project; others go to the reviewer.** A failed tool, an empty
AC input that doesn't match `run.json` or a solver fallback stops that project. A differing
sample count or an unexpected reference still produces an archive, and it's flagged in `summary`,
because a person reviews every update anyway.

**CoRAL results run like AmpliconArchitect's.** CoRAL writes the same `_graph.txt` and
`_cycles.txt` layout that AC reads, so the plan doesn't treat them differently.

## Validation so far

TCGA (101 samples, GRCh37) on 2026-11-06, using AC `v2.0.1`, BFBArchitect 1.0.2 and
Gurobi, took 32 minutes through the existing script: download, AC, rebuild, validate
and report. No project was uploaded. It was compared with an October run of the same
samples, made from the source AA results with a pre-release AC commit and BFBArchitect
1.0.1.

- **Starting from the site's archive gives the same answer as starting from the source
  AA results.** All 228 features match, and the only differences in the result table
  are file paths. The classification profiles, gene list, FAN calls, ecDNA counts,
  feature properties, context calls, complexity and similarity tables match too.
- **Every BFB plot changes with BFBArchitect 1.0.2.** All 12 plots differ, by 1.2 to
  3.4% of pixels. The titles, scores, copy-number profiles and foldback positions look
  the same when viewed side by side; the x-axis range and foldback arrow widths differ.
  This matches the plot-span part of BFBArchitect's fix. The same fix's chromosome-name
  matching made no visible difference on these 12 plots, and its new error for missing
  copy-number bins did not occur. This is one GRCh37 project.
- **BFB text files change with any BFBArchitect version.** All 24 differ only in the
  version on their first line, so they can't be shared across a BFBArchitect release.
- **October's cohort outputs shouldn't be reused as they are,** because their BFB plots
  and text come from 1.0.1. They should be rebuilt with the final versions. Only TCGA
  has been compared so far.

The script's report gave TCGA's old AC version as 0.4.12, while the API says 2.0.0. The
archive has no AC log and its result table says `NA`, so the script fell back to the
original pipeline run's metadata. The version in the report should come from the API,
and settings recovered this way can be older than the results they sit beside.

## Needed on the site

**The upload route.** The form needs a browser session. The API has `upload_api/`, which
creates a project, and `add_samples_to_project_api/`, which adds samples. Neither replaces
a project's results with a token, so there's `POST upload_api/replace/<project id>/`.

- **Only site admins, by API token.** Anyone with an account can make a token, so the
  route also requires `is_staff`. It takes no browser session, so a cookie can't reach it.
- **It takes `expected_latest_id`** and refuses if the project has a newer version than the
  one the archive was built from. The check runs after the body has arrived and just before
  the write. Two requests in the same moment could still both pass, as two form edits can.
- **`dry_run` is required, as `true` or `false`,** so a mistyped request can't write. That
  only means each request says which it is. Nothing makes an upload wait for a dry run: a
  campaign runs them once at the start, to see the sizes and any problems, and
  `expected_latest_id` catches a project that moves on after that. A dry run reads the
  archive and reports sample and feature counts, tool versions, record size against the
  limit, files shared and written, and files that `run.json` names but the archive lacks.
  Its only database write is the task tracker's own row.
- **Both run in the background and answer 202 with a job id,** because a large project
  can take longer than a request should stay open. `POST upload_api/replace-status/<job id>/`
  reports the state. After a real replacement it reports what was stored, to compare with
  the dry run.
- **Metadata, description, alias, publication link and subscribers carry forward** because
  the route calls the code the form's replace calls. **The members carry forward too,** so
  an admin refreshing a project doesn't join it. The form now does the same. The admin is the
  new version's creator, a field that says who ran each version, and the audit log also
  records who did it. View and download counts start at zero on a new version, as with any edit, and the
  page shows the total across versions.
- **The whole project archive is counted apart.** Each version stores a new one, because it
  holds the version's id, so an update with no changed file still costs that much.
- **Reconstruction directories stored before #712 are written once more.** They carry real
  timestamps and owners in their tar headers, so they can't match the deterministic packing
  used now, even with identical contents. On dev on 2026-11-06, 8 samples of unchanged data
  shared 301 files and wrote 8 directories (26 MB). The dry run shows this per project.

**Where the route lives.** `/api/v1/` is documented as read-only, so the route is outside
it. It's under `upload_api/` because the WAF lets non-browser POSTs through there, while a
new top-level path meets Bot Control (tested on both sites 2026-11-06), and we'd rather not
change the WAF. GETs under `upload_api/` also meet Bot Control, which is why the status call
is a POST. A 500 MB body reached dev through the load balancer in 52 s, about 10 MB/s from
the lab, so a gunicorn worker's 900 s limit allows about 9 GB at that speed.

**The keep-current checkbox and index.** "Keep this project's classifications up to
date", on the create and edit forms, with the explanation in a tooltip. It's off by default,
and the wording doesn't promise it's fully automatic, because a person reviews changes
first. Owners, members and admins can change it, the same people who can edit the project.
It isn't available for private projects: the box is greyed out while Visibility is Private,
and the form refuses the pair, including when an opted-in project is made private with the
box still ticked. A private project's data stays with its members, and its name stays out
of the operator's plan files. Public and hidden-public projects can opt in, and an admin's
API token can read a hidden-public project, as anyone with the link can on the site; a
private one stays members-only through the API. It's stored as `keep_current` and is
chain-level (`project_fields.CHAIN_LEVEL`):

- Changing only the checkbox edits the project in place, with no new version.
- A new version, including the replace route's, carries the project's setting.
- Deleting the newest version leaves the setting where the project now is. If version 3
  is deleted and version 1 becomes current, version 1 takes version 3's setting, because
  promotion copies chain-level fields from the deleted head.

The flag isn't in the public API. The admin list is a POST, for the WAF reason above. A
project made private some other way keeps its stored flag but drops off the list until it
isn't private.

## Easy to forget

- **Subscriber emails.** Each new version emails its subscribers. On prod on 2026-11-06,
  4 of 89 current projects had a subscriber, one each and two people in all, and all four
  projects are on the keep-current list. That's acceptable for now, so these updates
  send the normal emails.
- **A base that moves.** A project can change between `run` and `upload`, hence
  `expected_latest_id`.
- **Recovered settings can mislead.** A flag recovered from an archive shows how that
  version ran, and an archive without an AC log falls back to older metadata. Flags are
  meant to come from the archive's own AC logs, so a project on the site's list needs no
  settings of its own. The `extra_ac_flags` column of a `--projects` file is the fallback
  for an archive whose logs don't record them. None is known to need it today: DCIS -
  Chmelova's current version ran with defaults.
- **Metadata that travels with an update.** Hartwig's richer sheet goes up with its AC
  replacement, and it sits close to the record limit, so `dry_run` must cover the pair.
- **Solver time limits.** Gurobi has a time limit, so results near it could depend on
  machine load and may not reproduce byte for byte even when pinned. Not measured yet.
- **A failed upload used to lose the alias.** The edit clears the alias from the old
  version to hand it on, and a rollback didn't put it back, in the form as well. Fixed with
  the route.
- **Site load.** Upload one project at a time. GridFS writes took about 0.30 s/MiB in
  autumn 2026, so a 2 GB archive holds the site's write path for about 10 minutes.
  Shutdown mode refuses uploads, and `upload` should say so plainly.
- **Checking the result.** After each upload, compare the site's sample and feature counts
  and its page fields with the archive's own, its AC version with the target, and, if there was
  a dry run, the files stored with the prediction. Stop the batch on a miss.
- **A new version's page fields.** A version has no `sample_data`, `ecDNA_context` or
  `reference_genome` until its page is first opened, and the API reports a blank reference
  until then. The form's user always opens it, but a script can't: the WAF refuses a non-browser
  page request (403, dev, 2026-11-06), and a private page needs a login. So `upload` asks for
  this with `POST upload_api/replace-prepare/<version id>/`, and the status reports `page_ready`.
  It reads the archive from the primary, because a failed read stores an empty context for good.
  These fields have to be there, so `upload` stops if they aren't. `sample_data` should have one
  row per distinct sample name, not per `sample_count`: read-only on 2026-11-07, that held on all
  83 of prod's and 41 of dev's current versions, and `sample_count` on 76 and 37. An empty
  `ecDNA_context` is only wrong if the archive holds calls. All 10 of prod's empty ones hold none.
- **Undo.** Keep a record of each new version id and what it replaced. Old versions are
  never removed to make room.
- **Resuming.** A full run takes many hours, so per-project state must survive a crash
  without repeating finished work.
- **Disk.** `preflight` should compare free space with the sum of what `plan` predicts.
  Work directories hold the download, the unpacked tree, AC's output, the new archive and
  its aggregated check.
- **Tests.** CI has no Gurobi licence, so the workflow's tests need a small synthetic
  archive and a stubbed AC.

## Order of work

`upload` goes through the site's own upload path, like any other user, and shutdown mode
refuses it during a deploy. So it doesn't need the operator lock (#710), which is wanted
by 2026-11-16 and can be built alongside this.

1. `setup`, `preflight`, `plan`, `run` and `summary`, moved into the repo. Done.
2. The upload route with `dry_run` and `expected_latest_id`: local, dev, check-in, prod.
3. `upload`, tested end to end against dev. Non-admins keep using the edit form: this is
   for admins updating in bulk.
4. The keep-current checkbox and index, then point `plan` at it. Built 2026-11-07.
