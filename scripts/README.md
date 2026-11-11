# scripts/

Everything operators and developers run by hand or from cron, other than the Django
application itself. One row per file; `tests/test_scripts_readme.py` fails when a file here
has no row, or a row has no file, so this page cannot drift from the directory.

**Adding a script?** Add its row in the same commit. If you cannot say what it is for, who
runs it and when it ends, it is not ready to be committed. A script written for one
campaign says so in **Status**, and is deleted when the campaign is over: git history keeps
it, and `docs/` records what it did.

## Running things

```bash
# From the repository root, with the environment loaded (see AGENTS.md):
source caper/config.sh && python scripts/maintenance/<name>.py --help
```

Scripts put `caper/` on `sys.path` themselves, so the application package imports work
from any directory. Four tools that the application also imports are modules of the
package, not scripts (last table): run those from the Django project directory, as
`python -m caper.<name>`, or in a container with `docker exec -w /srv/caper`.

Every script that writes to a database checks the operator lock first (`--no-lock "<reason>"`
is the escape hatch; `tests/test_ops_lock.py` enforces it). Most default to a dry run and say
so in their usage text; read-only reports say so in their docstring. See AGENTS.md before
pointing anything at dev or prod.

**Status** values:

- **scheduled**: runs from cron on the hosts. Never delete as dead; moving or renaming it
  means editing both crontabs in the same deploy window.
- **standing**: an operator runs it when the situation it exists for arises.
- **campaign**: written for one piece of work; retire it when that ends.
- **under review**: kept deliberately while something else decides it (named in the row).
- **deploy**, **dev**: part of how the servers are run, or how a laptop is set up.

## deploy/

| File | What it is | Added | Status |
|---|---|---|---|
| `logrotate-ampliconrepo.conf` | Log rotation, copied to `/etc/logrotate.d/` by hand on each host | 2026-08-18 `1d60b88` | deploy |
| `start-server.sh` | `docker run` for the app container: memory cap, restart policy, environment | 2023-02-09 `cde9eb3` | deploy |
| `start-neo4j-container.sh` | Starts the Neo4j container with its conf and data directories | 2025-03-17 `2bee4ef` | deploy |
| `stop-neo4j-container.sh` | Stops it | 2025-03-17 `2bee4ef` | deploy |
| `sync_static_to_s3.sh` | Pushes `caper/static/` to `s3://amprepobucket/<env>/static/` | 2025-04-02 `8fe9b5a` | deploy |
| `wedge-watchdog.sh` | Source of prod's `/usr/local/bin/wedge-watchdog` (root cron, every minute). The host runs an installed **copy**: edit here, then reinstall | 2026-08-07 `c05b1fc` | scheduled |

## maintenance/

Writers take the operator lock; the first column says which of them are also on a timer.

| File | What it is | Added | Status |
|---|---|---|---|
| `backfill_project_status.py` | Corrections to `projects` that the application can no longer read around; the lineage validator (I1) tells operators to run it | 2026-08-27 `2aac02c` | under review (PR #748, same question) |
| `build_gene_map.py` | Builds the gene-name to NCBI Gene ID table in `caper/gene_map/` | 2026-10-29 `319f0a9` | standing: rerun when AmpliconClassifier's gene GFFs change |
| `cleanup_failed_upload_residue.py` | Deletes GridFS files a failed ingestion left behind | 2026-09-01 `4448232` | scheduled (weekly, both hosts) |
| `cleanup_orphaned_projects.py` | Deletes projects no code path can reach, with protections | 2026-03-24 `fcef14c` | standing |
| `clear_stale_uploads.py` | Removes upload placeholders that crashed before becoming projects | 2026-08-27 `48a91ff` | standing |
| `download_refs.py` | Fetches the AmpliconArchitect reference builds and checks their md5sums; the only backup of what `refs.ampliconrepository.org` serves | 2026-09-01 `fe64151` | standing |
| `purge-local-db.py` | Reports and removes local MongoDB/GridFS residue; imports `cleanup_orphaned_projects.py`, so the two stay together | 2023-04-06 `1d89134` | standing (local development). **Known defect, not from the move:** it fails to start from a shell (`AppRegistryNotReady`, since `005e1cc` imported `caper.feature_index` at module top); it only runs under the test suite, where Django is already set up |
| `recover_deleted_version.py` | Restores a version deleted from a project's history | 2026-04-20 `13bdf24` | standing |
| `snapshot_storage.py` | Records the daily storage snapshot behind the admin statistics page | 2026-09-01 `0d25917` | scheduled (daily, both hosts) |
| `sweep_gridfs_unreferenced.py` | Deletes GridFS files no project references, from a reviewed snapshot | 2026-08-31 `20f3f05` | standing |
| `sweep_s3_unreferenced.py` | Removes S3 objects no project document in any database references | 2026-08-31 `4d4d4e6` | standing |

## diagnostics/

Read-only unless the row says otherwise.

| File | What it is | Added | Status |
|---|---|---|---|
| `check_disk.py` | Emails an alert when the host's disk is low (#517) | 2026-03-31 `73e94c9` | scheduled (every 5 minutes, both hosts; output goes to `/dev/null`, so a failure is silent) |
| `report_gridfs_orphans.py` | Accounts for every GridFS file in exactly one bucket; deletes nothing | 2026-08-29 `4931346` | standing |
| `report_upload_rates.py` | How fast each upload wrote to GridFS, from the backlinks | 2026-10-13 `fa0b4a4` | standing |
| `smoke_api_genes.py` | Smoke test of gene resolution in the feature API, against a running site | 2026-10-29 `ac3b6d9` | standing |
| `validate_project_lineage.py` | Checks the project version-history invariants against a database | 2026-08-26 `4bf10a1` | standing |
| `verify_indexes.py` | Verifies the `projects` indexes the application creates at startup exist | 2026-01-27 `ea9f5bc` | standing |

## memory_auditing/

The toolkit that measured the nightly restart (campaign closed 2026-09-14, see
`docs/server-ssh-access.md`) and was kept to answer the same question again. The three move
together: `leak_repro.py` imports `memory_probe.py`, and `coamp_drive.py` is run against a
server that `memory_probe.py` is sampling.

| File | What it is | Added | Status |
|---|---|---|---|
| `memory_probe.py` | Per-worker rss/pss/uss plus cgroup memory for the gunicorn tier; `--once` is the cron form | 2026-09-02 `87f6eed` | standing (not scheduled since 2026-09) |
| `leak_repro.py` | Runs one suspect operation in a loop and measures what it leaves behind | 2026-09-02 `87f6eed` | standing |
| `coamp_drive.py` | Drives repeated co-amplification analyses at a running site over HTTP | 2026-09-02 `87f6eed` | standing |

## dev/

| File | What it is | Added | Status |
|---|---|---|---|
| `local_upload_project.py` | Creates a project on a **local** site exactly as the upload form does | 2026-10-29 `26aa765` | dev |
| `run_django_command.sh` | Runs `manage.py` with `caper/config.sh` sourced; AGENTS.md's entry point | 2026-03-03 `b4d2703` | dev |

## Modules of the application package

These began as scripts at the repository root. The application imports three of them, so
they live in `caper/caper/`. Run them from the Django project directory.

| Module | What it is | Added | Status |
|---|---|---|---|
| `python -m caper.ops_lock` | The operator lock for writes to dev and prod (`docs/ops-lock-spec.md`) | 2026-11-06 `0f8f087` | standing |
| `python -m caper.shutdown_mode` | Reads or toggles the flag behind `/admin-prepare-shutdown/` | 2026-09-24 `ec0c95f` | standing |
| `python -m caper.backup_sqlite` | Off-host backup of `caper.sqlite3` without its sessions; the admin backups page imports it | 2026-08-31 `3322f6e` | scheduled (nightly 00:05, both hosts) |
| `python -m caper.dump_metadata` | Dumps every collection except the GridFS payload, for an off-AWS copy; the admin backups page imports it | 2026-08-31 `ae4685b` | standing |

## Scheduled jobs

The `ubuntu` crontab on **each host** runs these four, by path into the checkout (bind-mounted
at `/srv` in the container). `<env>` is `dev` or `prod`; `<db>` is `caper-dev` or `caper`.
Times differ per host so the two never run at once; the weekly and daily jobs are given once
per host below, so copying a line onto a host keeps that host's slot. Moving or
renaming any of the four means editing both crontabs in the same deploy window, because
nothing else will notice a job that silently stopped (`check_disk.py` writes to `/dev/null`).

```cron
# nightly SQLite backup (a module of the package: run from /srv/caper)
5 0 * * *    docker exec -w /srv/caper amplicon-<env> bash -c 'set -a; source /srv/caper/config.sh; set +a; /opt/venv/bin/python -m caper.backup_sqlite --db /srv/caper/caper.sqlite3 --execute' >> /home/ubuntu/backup_sqlite.log 2>&1
# disk alert, every five minutes, on the host
*/5 * * * *  sh /home/ubuntu/AmpliconRepository-<env>/caper/config.sh && python3 /home/ubuntu/AmpliconRepository-<env>/scripts/diagnostics/check_disk.py > /dev/null 2>&1
# weekly failed-upload cleanup. Time fields: dev `30 3 * * 0`, prod `30 4 * * 0` (Sunday)
<dev: 30 3 | prod: 30 4> * * 0   docker exec -w /srv amplicon-<env> bash -c 'set -a; source /srv/caper/config.sh; set +a; /opt/venv/bin/python /srv/scripts/maintenance/cleanup_failed_upload_residue.py --expect-db <db> --execute --undo-record /srv/logs/failed-upload-cleanup-undo.jsonl' >> /home/ubuntu/failed_upload_cleanup.log 2>&1
# daily storage snapshot. Time fields: dev `0 2 * * *`, prod `30 2 * * *`
<dev: 0 2 | prod: 30 2> * * *   docker exec -w /srv amplicon-<env> bash -c 'set -a; source /srv/caper/config.sh; set +a; /opt/venv/bin/python /srv/scripts/maintenance/snapshot_storage.py --expect-db $DB_NAME' >> /home/ubuntu/storage_snapshot.log 2>&1
```

Prod's root crontab separately runs `/usr/local/bin/wedge-watchdog` every minute, an
installed copy of `deploy/wedge-watchdog.sh`, so repository moves do not affect it.

## `caper/stop-server.sh`, the one stop script

The only stop script is the one inside the container's tree, `caper/stop-server.sh`: it waits
up to 30 minutes for background tasks, then stops the server process. It is not under
`scripts/` because dev's `/home/ubuntu/stop-and-start-repo.sh`, which lives on the host and
not in this repository, runs it by path (`docker exec -t amplicon-dev /srv/caper/stop-server.sh`).
Moving it means editing that host script. A second, host-side wrapper used to sit at the root
and only ran this one before `docker stop`; it was removed rather than moved.

## What stays at the repository root, and why

`run-manage-py.sh` and `gunicorn_config.py`: the app container's `CMD` and gunicorn's
config path name them, and the checkout is bind-mounted over the image's `/srv`, so the
checkout's copies are the ones that run. `conftest.py`, `pytest.ini`, `Dockerfile`,
`docker-compose-dev.yml` and `requirements.txt` are read by the tools that own them from
this directory.
