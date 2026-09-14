# Teardown ledger — nightly-restart / memory-leak campaign (opened 2026-09-02)

Everything this campaign creates that costs storage is recorded here, so it can
be removed when the campaign ends. Large re-aggregation and upload runs are the
point of the exercise, and HMF and PCAWG are the inputs that make a leak
visible — which also makes them the inputs that fill GridFS and S3 fastest.

**Nothing is deleted from this list automatically. Work through it at the end.**

## Close-out, 2026-09-14

The soak is concluded (issue #629 has the numbers; docs/server-ssh-access.md
"The daily restart, retired 2026-09-03" has the write-up). Status of every row
below:

| Item | Outcome |
|---|---|
| dev probe cron | removed 2026-09-14 05:37 UTC; backup `/home/ubuntu/crontab.bak-20260914T053706Z` |
| prod probe cron | removed 2026-09-14 05:41 UTC with approval; backup `/home/ubuntu/crontab.bak-20260914T054109Z` |
| restart crons, both hosts | **kept commented, by decision** (Jens, 2026-09-14), with the verdict written in the comment block |
| both CSV series | archived off-AWS (dev and prod kept separate), md5-verified against the hosts; originals left in place |
| dev `coamp_sampler.sh`, `coamp_sample.log`, `disable_restart.py` | deleted 2026-09-14 |
| prod `disable_restart.py`, `memory_probe.py.bak-20260903T065719Z` | deleted 2026-09-14 with approval |
| dev / prod checkouts | both on tag `v4.1.9_091326`; the branch merged, nothing to move |
| projects, GridFS, S3 | **none to delete** — measured 2026-09-14: 0 of 160 `caper-dev` project documents have an `_id` generated on or after 2026-09-02 |
| `memory_probe.py` | **stays, by decision** — ops tool for the next memory question (instance sizing) |
| `leak_repro.py`, `coamp_drive.py` | untouched; not asked about |

## What does and does not need teardown

- `leak_repro.py --scenario aggregate` **creates nothing to clean up.** It calls
  `Aggregator` directly the way `views._process_and_aggregate_files` does, and
  removes its `tempfile.mkdtemp` work directory in a `finally`. No project
  document, no GridFS payload, no S3 object.
- **Real uploads and real project edits do need teardown** — each one writes a
  project document, a GridFS tarball, and (with `S3_FILE_DOWNLOADS=TRUE`) an S3
  object. A re-aggregation additionally leaves the superseded version behind,
  which holds its own payload.
- Failed uploads leave residue of their own; `cleanup_failed_upload_residue.py`
  runs on dev at 03:30 Sunday, but do not rely on it to tidy up after a
  deliberate campaign.

## Instruments installed (remove when done)

| Where | What | Removal |
|---|---|---|
| dev host crontab | `* * * * * docker exec amplicon-dev … memory_probe.py --once` | `crontab -e`, delete the line and its comment block |
| prod host crontab | the same probe line, added 2026-09-03 with approval | `crontab -e`, delete the line and its comment block |
| dev checkout | on branch `memory-probe-nightly-restart` (was detached at `93af56a`) | `git checkout` the release ref when the branch merges |
| dev + prod logs | `memory_probe.csv` (~1.5 MB/day each), plus a rotated `.csv.<stamp>` from the column change | **keep** — they are the measurement; move somewhere durable |
| prod checkout | on branch `memory-probe-nightly-restart` since 2026-09-03 07:27 (was tag `v4.0.0_090226`) | `git checkout` the 4.1.0 tag once it is cut |
| prod host | `/home/ubuntu/memory_probe.py.bak-20260903T065719Z`, the pre-update probe | delete once the branch is merged |
| dev host crontab | nightly restart `15 0 * * *` **commented out** 2026-09-03 15:53 | delete the leading `#` to restore; backup at `/home/ubuntu/crontab.bak-20260903T155351Z` |
| dev host | `/home/ubuntu/coamp_sampler.sh` + `coamp_sample.log`, `/home/ubuntu/disable_restart.py` | delete; the sampler self-terminates after an hour |

The in-place edits to dev's `gunicorn_config.py` are gone: dev now runs the
branch from git, so nothing is hand-patched and nothing is waiting to be lost
to the next checkout.

## Deliberate outages caused (all on dev, all recovered)

- **2026-09-03 07:18** — neo4j OOM-killed by the kernel during three
  back-to-back HMF+PCAWG co-amplification analyses. Not intended, recovered by
  itself in twenty seconds. Nothing to undo; the graph cache rebuilds.
- **2026-09-03 16:04 and 16:0x** — neo4j stopped and started deliberately to
  confirm the user-facing retry message. Roughly 35 s each. Nothing to undo.

## Projects created

**None so far, and none by the work of 2026-09-02.** Nothing in this campaign
has written a project document, a GridFS payload or an S3 object. Specifically:

- The dev co-amplification runs (3 analyses of `PCAWG cutoff passed`,
  6a8009d4dccf2fb29759c408, driven over HTTP as `ai_agent_jl`) only read
  projects and wrote Neo4j cache entries. Those are cleared from
  `/admin-clear-cache/?clear_graphs=true`, which the driver already calls before
  each iteration; nothing accumulates.
- The local re-aggregation runs (TCGA_agg 98 MB ×4, PCAWG_agg 1.7 GB ×3) ran
  the aggregator into `/var/tmp/leakrepro_agg_*` work dirs, each up to 4.9 GB,
  each removed by the harness afterwards. Verified empty on 2026-09-02.

If a real upload or project edit is done later — the highest-fidelity version
of the re-aggregation test, and the only one that exercises GridFS and S3 —
record it here:

| Date | Server | Project name / id | Input | Deleted? |
|---|---|---|---|---|
| | | | | |

## Local scratch

Local runs go to the dockerised mongo (`localhost:27017`, db `caper-dev`), not
the shared cluster, so local residue costs nothing but laptop disk. Purge with
`purge-local-db.py` if it gets large.
