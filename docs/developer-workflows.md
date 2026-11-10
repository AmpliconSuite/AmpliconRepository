# Developer workflows

Setup rules that apply to every session (sourcing `caper/config.sh`, the operator
lock) stay in [AGENTS.md](../AGENTS.md). This page holds the procedures you reach
for less often. For the stores and the data model, read
[backend-architecture.md](backend-architecture.md); for running the tests, read
[testing.md](testing.md).

## Local dev server

```bash
source caper/config.sh && cd caper && python manage.py runserver
# visit http://localhost:8000
```

## The dev server landing gate

`dev.ampliconrepository.org` shows a landing page instead of its home page. The passphrase
is **`amprepodev`** — published here, in README.md and in the
[wiki](https://github.com/AmpliconSuite/AmpliconRepository/wiki/Development-Server), because
it is a shibboleth to keep wanderers and crawlers out, not a secret. For a dev server
account, contact Jens Luebeck (jluebeck@ucsd.edu).

Implementation is `caper/caper/dev_gate.py`, second in `MIDDLEWARE` right after the health
check. Three properties matter when changing anything near it:

- **Home page only.** Deep links, the REST API and the OAuth callbacks are not gated, which
  is why no exemption list exists. Do not widen this without re-reading the module docstring.
- **It never authenticates.** It runs above `SessionMiddleware`, so it cannot see
  `request.user` and cannot admit an admin by login. That is deliberate — dev exists largely
  to test anonymous behaviour.
- **It must not touch MongoDB.** The landing page is rendered with `render_to_string` and no
  request object, so no template context processor runs (`shutdown_mode` alone would be a
  Mongo round trip). Keep it that way.

Off unless `DEV_GATE_ENABLED=True` is exported, so production and local checkouts are
unchanged; export it locally to see the gate. `DEV_ROBOTS_DISALLOW_ALL` is the separate,
later stage of de-indexing dev — README.md explains why the two cannot be flipped together.

## AWS credentials for S3 (config.sh has `S3_*=TRUE`)

The app authenticates to S3 via the boto3 profile `amprepo` (`AWS_PROFILE_NAME`), read from
`~/.aws`. If that profile is SSO-based, refresh the ~8-hour token before running anything
that touches S3 (uploads, downloads, static sync):
```bash
aws sso login --profile amprepo
```
A static `[amprepo]` block in `~/.aws/credentials` overrides the SSO profile — remove/rename
it to use SSO. On the dev/prod EC2 servers the target is an IAM instance role (no keys, auto
refresh); permanent access keys are being retired.

## Docker dev (simplest for new setup)

```bash
mkdir -p logs tmp .git
# host ~/.aws is mounted into the container; if using SSO, `aws sso login --profile amprepo`
# on the host first so a valid token is present.
docker compose -f docker-compose-dev.yml build --no-cache
docker compose -f docker-compose-dev.yml up -d
# visit http://localhost:8000
docker compose -f docker-compose-dev.yml down
```

## Create a project from the command line (local)

```bash
set -a; source caper/config.sh; set +a
LOCAL_SITE_PASSWORD=... python scripts/dev/local_upload_project.py --user <username> \
    --name "My project" --description "..." --visibility public \
    [--metadata samples.csv] results.tar.gz
```
Signs in through the site and submits the create-project form the way the
page's own upload script does, so the running server builds the project exactly
as for a browser upload, then waits until aggregation finishes. Local hosts only.
Checked 2026-10-29: a GBM39 project and a 13-feature Waddell project, each made
both ways, had identical documents and index rows apart from IDs, names and
times.

`manage.py create_project` (local path, HTTP URL or `s3://` URI) calls the view
inside its own process, which exits before the background aggregation finishes.
Projects made that way end up `aggregation_failed`, with no `AA_PNG_file` on
their features.

## Clean up local MongoDB / GridFS data

Prefer the safe two-stage cleanup before any full local purge:

```bash
source caper/config.sh

# 1. Remove unreachable project docs and their attached GridFS/S3/tmp files.
(cd caper && python ../scripts/maintenance/cleanup_orphaned_projects.py --dry-run)
(cd caper && python ../scripts/maintenance/cleanup_orphaned_projects.py)

# 2. Remove remaining GridFS blobs not referenced by reachable projects.
python scripts/maintenance/purge-local-db.py --smart-gridfs
python scripts/maintenance/purge-local-db.py --smart-gridfs --execute
```

Useful read-only reports:

```bash
source caper/config.sh && python scripts/maintenance/purge-local-db.py --gridfs-usage-by-type --limit 25
source caper/config.sh && python scripts/maintenance/purge-local-db.py --gridfs-usage-by-project --limit 25
source caper/config.sh && python scripts/maintenance/purge-local-db.py --tarfile-report --limit 25
```

`scripts/maintenance/cleanup_orphaned_projects.py` protects all reachable projects, previous versions, soft-deleted current projects, and redirect tombstones before deleting orphan project documents plus their GridFS/S3/tmp payloads. `scripts/maintenance/purge-local-db.py` is dry-run by default; destructive operations require `--execute`. Its default `--reference-scope reachable` is conservative, and `--reference-strategy recursive` protects any ObjectId-like value in reachable project documents. Use `--reference-strategy app-fields` only when you intentionally want a more aggressive GridFS cleanup based on known GridFS fields.

The legacy full wipe is now explicit and local-dev-only: `python scripts/maintenance/purge-local-db.py --all-project-data --execute`.

For project lineage audits (including orphaned and stranded documents), run
`python scripts/diagnostics/validate_project_lineage.py --help`; it
classifies documents through `project_status`, so it does not read the legacy `delete`/`current`
flags itself.

To restore a version that was deleted from a project's history, use
`scripts/maintenance/recover_deleted_version.py`; it is dry-run by default and writes only with
`--apply`. It **refuses** a version whose `payload_purged` flag is set: deletion
purges the GridFS payload, so clearing the markers would leave a version that
resolves by URL and has no data behind it. Such a version is already visible in
the history table, marked as deleted.

## Shutdown mode during a deploy

The deploy runbooks in the
[wiki](https://github.com/AmpliconSuite/AmpliconRepository/wiki) open by
enabling shutdown mode and close by disabling it. Without a browser session,
use `caper/caper/shutdown_mode.py` from the server's checkout, which the container mounts
at `/srv`:
```bash
docker exec -w /srv/caper amplicon-dev /opt/venv/bin/python -m caper.shutdown_mode --expect-db caper-dev status
docker exec -w /srv/caper amplicon-dev /opt/venv/bin/python -m caper.shutdown_mode --expect-db caper-dev on     # refuses while tasks run
docker exec -w /srv/caper amplicon-dev /opt/venv/bin/python -m caper.shutdown_mode --expect-db caper-dev wait   # blocks until none run
docker exec -w /srv/caper amplicon-dev /opt/venv/bin/python -m caper.shutdown_mode --expect-db caper-dev off
```
On prod the container is `amplicon-prod` and the database `caper`; `on` and
`off` there are prod writes. Its docstring explains why "no running tasks" means
no task younger than about an hour.
