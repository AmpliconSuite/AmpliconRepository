# Teardown ledger — nightly-restart / memory-leak campaign (opened 2026-09-02)

Everything this campaign creates that costs storage is recorded here, so it can
be removed when the campaign ends. Large re-aggregation and upload runs are the
point of the exercise, and HMF and PCAWG are the inputs that make a leak
visible — which also makes them the inputs that fill GridFS and S3 fastest.

**Nothing is deleted from this list automatically. Work through it at the end.**

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
| dev checkout | `/home/ubuntu/AmpliconRepository-dev/memory_probe.py` (untracked copy) | `rm`, or leave once the file is in the repo and pulled |
| dev checkout | `gunicorn_config.py` edited in place to add `pid=%(p)s` to the access log | backup at `/tmp/gunicorn_config.py.bak`; superseded once the branch lands |
| dev logs | `/srv/logs/memory_probe.csv` (~1.5 MB/day) | **keep** — it is the measurement; move it somewhere durable |

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
