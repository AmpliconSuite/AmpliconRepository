# `amprepo_reclassify`: updating site projects to a new AmpliconClassifier release

Status: design, nothing built in this repo yet. Written 2026-11-06.

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

Each stage is one subcommand. Only `upload` writes to a site.

| Stage | Does | Writes to |
|---|---|---|
| `setup` | Creates a virtual environment with the pinned tools; records this machine's AC threading | local |
| `preflight` | Checks tools, solver, `$AA_DATA_REPO`, disk, token and site | nothing |
| `plan` | The dry run: lists each project's latest version, reference, recovered AC settings, sizes and expected work | nothing |
| `run` | Download, AC, rebuild, validate, report, per project, resumable | local |
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
`--bfb_threads` and stores them in the machine's ignored config file (oreo, 32 threads:
`--jobs 16 --bfb_threads 2`). `preflight` warns if their product exceeds the machine's
threads.

**The local aggregator comes from PyPI, at the site's version.** The site runs the
aggregator on every upload, so the site decides how the stored project is packed. The
local copy is for checks before upload: catching dropped samples, including samples
without amplicons, and producing the `run.json` that the record-size check reads. `setup`
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

**The list of projects comes from the site.** The keep-current checkbox is the index;
`plan` reads it through the API with the operator's token. Until the checkbox exists,
`plan` takes a local file. Version ids change with every update, so they're looked up
at `plan` time, never stored.

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

**The keep-current checkbox and index,** as already agreed: off by default, in the
same project, a new row in the version history, and wording that doesn't promise it's
fully automatic, because a person reviews changes first.

## Easy to forget

- **Subscriber emails.** Each new version emails its subscribers. On prod on 2026-11-06,
  4 of 89 current projects had a subscriber, one each and two people in all, and all four
  projects are on the keep-current list. That's acceptable for now, so these updates
  send the normal emails.
- **A base that moves.** A project can change between `run` and `upload`, hence
  `expected_latest_id`.
- **Recovered settings can mislead.** A flag recovered from an archive shows how that
  version ran, and an archive without an AC log falls back to older metadata. The
  per-project override stays for a project that really needs non-default flags. None is
  known today: DCIS - Chmelova's current version ran with defaults.
- **Non-AA projects.** CoRAL cell lines hasn't been through this script.
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
- **Checking the result.** After each upload, confirm through the API the AC version,
  sample count and feature count, open the project page once so derived fields are
  written, and record files shared against the prediction. Stop the batch on a miss.
- **Undo.** Keep a record of each new version id and what it replaced. Old versions are
  never removed to make room.
- **Resuming.** A full run takes many hours, so per-project state must survive a crash
  without repeating finished work.
- **Disk.** `preflight` should compare free space with the sum of what `plan` predicts.
  Work directories hold the download, the unpacked tree and two rebuilt trees.
- **Tests.** CI has no Gurobi licence, so the workflow's tests need a small synthetic
  archive and a stubbed AC.

## Order of work

`upload` goes through the site's own upload path, like any other user, and shutdown mode
refuses it during a deploy. So it doesn't need the operator lock (#710), which is wanted
by 2026-11-16 and can be built alongside this.

1. `setup`, `preflight`, `plan`, `run` and `summary`, moved into the repo.
2. The upload route with `dry_run` and `expected_latest_id`: local, dev, check-in, prod.
3. `upload`, tested end to end against dev.
4. The keep-current checkbox and index, then point `plan` at it.
