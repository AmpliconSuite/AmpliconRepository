# AGENTS.md — AmpliconRepository

A genomics data repository (ampliconrepository.org) for storing, browsing, and analysing DNA amplicon results produced by [AmpliconSuiteAggregator](https://github.com/AmpliconSuite/AmpliconSuiteAggregator). Built with Django + Mezzanine CMS.

---

**For the backend itself — the stores, the project/version data model, the
deletion paths and the maintenance tooling — read
[docs/backend-architecture.md](docs/backend-architecture.md). It is the map;
this file is the setup.**

---

## Critical: Environment Setup Before Any Django Command

**Always** source `caper/config.sh` before running any Django management command. It sets all required env vars (MongoDB URI, OAuth secrets, S3, Neo4j, email).

```bash
# Required pattern
source caper/config.sh && cd caper && python manage.py <command>

# Or use the helper script from project root
./scripts/dev/run_django_command.sh <command>
```

Never commit `caper/config.sh` or `caper/.env` to version control.

---

## Critical: The Operator Lock Before Any Write to Dev or Prod

**Before any write to dev or prod**, run `python -m caper.ops_lock --expect-db <db> status`. Wrap the
work in `python -m caper.ops_lock --expect-db <db> run --holder H --owner O --reason "..." -- <command>`,
or `acquire` it and `release` it when done. Use `acquire` for a deploy, because a restart kills
`run` inside the container.

`ops_lock` and `shutdown_mode` are modules of the application package, so run them from the
Django project directory: `cd caper` locally, or on a server
`docker exec -w /srv/caper amplicon-<env> /opt/venv/bin/python -m caper.ops_lock ...`. If the lock is held, report the holder and
reason to your user and use `wait`. Do not `break` it or work around it. On prod, each of
these is still a write that needs its own explicit ask.

What counts as a write: the `--apply` or `--execute` mode of a maintenance script, `manage.py`
commands that write, deploys, container and Neo4j restarts, `python -m caper.shutdown_mode on/off`,
migrations and backfills, and test runs or manual testing on dev that create, edit or delete
projects. Reads, reports and dry runs don't need it, and neither does your own local database.

The repo's write tools refuse without the lock on dev and prod. `--no-lock "<reason>"` is for
emergencies, and its reason is logged. The lock only coordinates operators: uploads through
the site don't need it, and users are kept out by shutdown mode. See
[docs/ops-lock-spec.md](docs/ops-lock-spec.md).

---

## Critical: WAF Changes Go Through `python -m amprepo_waf`

One AWS WAF web ACL, `Amplicon_WAF` (us-east-1), sits in front of **both** the prod and dev
load balancers, so every change to it is a production change and none can be tried on dev
first. Change it only with `python -m amprepo_waf edit <name>`, where each change is a named
function in `amprepo_waf/edits.py`. Never hand-run `aws wafv2 update-web-acl`: it replaces
the whole ACL, and a field left out is deleted. The tool carries every field forward, refuses
a result that breaks the rule order or a response body a rule still uses, and keeps an undo
record. Applying (`--apply`) is a prod write: it needs its own explicit ask and prod's operator
lock. Reading (`show`, `check`, `probe`, a dry-run `edit`) needs neither. See
[amprepo_waf/README.md](amprepo_waf/README.md).

---

## Working Rules

### Writing to production

Prod and dev share one DocumentDB cluster (databases `caper` and `caper-dev`). Anything that
connects should assert the database name, and never print `DB_URI_SECRET`. A local MongoDB may
also be called `caper-dev`, so the name alone doesn't say which server you are talking to.
Read-only measurement of prod is fine.

**Every write to prod is a separate, explicit ask** to the human driving the session; approval
for one write is not approval for the next. When a write is approved:

1. **Capture a state probe first**, covering what the write should change and what it must
   leave alone. Diff two runs of it rather than reading one.
2. **Write the predicted numbers down before running**: which counts move, by how much, and
   which must not move.
3. **Stage it.** Run a few documents first (`scripts/maintenance/backfill_project_status.py
   --limit N` is the model), diff the probe, then do the rest. A staged run that misses its
   prediction is a stop, not a retry.
4. **Keep the undo record** that write scripts produce, somewhere durable and not in the repo.
   It can show exactly which paths and clients get through.

### Deploy order

Local, then dev, then check in with the human driving the session, then prod. Read the wiki
runbook page before you start: "Deploying unreleased code to dev" for a branch, "Creating a new
release for AmpRepo" for a tag. Dev can take a deploy at any time; batch what goes to prod.

### Establishing that a claim is true

Most defects here are a list or predicate kept in two places, so divergences turn up
constantly, and a divergence is easy to mistake for a fault.

- **Name the measurement that would falsify the claim, and run it before the claim goes into a
  code comment, a commit message, a report or a memory file.** An orphan count, a drift count, a
  document count, a query for the value. If no such command exists, say the claim is unverified
  in the same breath.
- **"The code permits X" is not "the data shows X."** A stale key list or an unguarded path is
  a hazard until something is measured on the other side. Report it as a hazard.
- **Trace a field to its writer, and remember earlier versions wrote it too.** The field's name
  and today's code don't tell you what old documents hold. `git log -S <field>` finds writers
  you didn't know about.
- **Date every measurement and name its scope** ("measured on prod, 2026-08-27"). Don't cite a
  working document by section number in code; state the fact instead.
- **Report times in your user's local time zone**, not UTC, and name the zone ("14:55 PST").
  Servers, logs and AWS report UTC, so convert before you write it down.
- Ask the load-bearing question ("are there orphaned files?") before the adjacent one ("do the
  key lists match?").

### Hazards that are easy to walk into

- **Starting Django is not a read.** `manage.py shell`, a script that calls `django.setup()`, and
  every server start run `CaperConfig.ready()` (`caper/caper/apps.py`), which creates indexes,
  rebuilds the site statistics when the stored snapshot is stale, sweeps orphaned temp
  directories (it skips those of tasks recorded as running), and, when `S3_STATIC_FILES` is
  `TRUE`, runs `aws s3 sync` of `caper/static/` into the environment's static prefix (it adds and
  overwrites, and never deletes). On dev and prod use the raw-pymongo modules
  (`ops_lock`, `shutdown_mode`) or export `S3_STATIC_FILES=FALSE`, and treat the rest as a write.
- **In the containers, `python` is not on the PATH.** Use `/opt/venv/bin/python`.
- **Don't `pkill -f "<pattern>"`, and write `pgrep -f "runserv[e]r"` rather than
  `pgrep -f "runserver"`.** The pattern matches the calling shell's own command line and kills
  or finds the caller.
- **A delete gets its own command and description.** Never tack one onto another command, and
  never silence its output.
- **Old project versions are load-bearing.** They back published results. Never propose
  deleting them to save space.

### This repository is public

So are the docs repository and the wiki. Don't put account IDs, credentials, internal
measurements that reveal what the WAF lets through, or anything about individual users into
them. Keep internal notes, undo records and handoff documents in a private location.

---

## Architecture Overview

### Dual-Database Design (key non-obvious detail)
The app uses **two completely separate databases**:

| Database | Purpose | Access |
|---|---|---|
| **SQLite** (`caper/caper.sqlite3`) | Django auth, sessions, Mezzanine CMS pages | Django ORM via `models.py` |
| **MongoDB** (`DB_URI_SECRET` env var) | All project/sample/feature data | PyMongo directly via `utils.py` globals |

**Do not** use Django ORM for project/sample data — all project queries go through `collection_handle` from `caper/caper/utils.py`. The `dbrouters.py` `RunsDBRouter` is a leftover artefact and not actively routing; real MongoDB access bypasses Django's ORM entirely.

### Third Database: Neo4j
Co-amplification graph data is stored in Neo4j (bolt port 7687). See `caper/caper/neo4j_utils.py`. The driver connects using `NEO4J_PASSWORD_SECRET` env var.

### Key Global Handles (defined at module level in `utils.py`)
```python
collection_handle          # MongoDB 'projects' collection (secondary-preferred reads)
collection_handle_primary  # Same collection, primary reads (for writes/admin)
audit_log_handle           # MongoDB 'project_audit_log' collection
fs_handle                  # GridFS handle (large files / tarballs)
```
These are imported directly across `views.py`, `search.py`, `site_stats.py`, etc.

---

## Code Structure

```
caper/caper/           # Main Django app
  views.py             # ~5000 lines — primary request handlers
  views_admin.py       # Admin-only pages (stats, delete, email)
  views_apis.py        # REST upload API (FileUploadView, ProjectFileAddView)
  utils.py             # MongoDB connection + all shared helpers (1000+ lines)
  models.py            # SQLite-backed Django models (auth admin actions only)
  settings.py          # All config; reads env vars set by config.sh
  neo4j_utils.py       # Co-amplification graph load/query
  search.py            # MongoDB-based project/sample search
  extra_metadata.py    # CSV/TSV/XLSX metadata attachment to samples
  sample_metadata_rows.py # Which feature row holds a sample's metadata; readers for both stored forms
  project_record_size.py # Refuses a project or metadata sheet that would not fit in one document
  gridfs_cache.py      # Django cache wrapper around GridFS reads
  tar_utils.py         # Stream-extract files from GridFS-stored tarballs
  site_stats.py        # Aggregated stats stored in MongoDB 'site_statistics'
  context_processor.py # Also stores system flags (shutdown, registration) in MongoDB
  schema_validate.py   # JSON schema validation for project documents
  project_version_cleanup.py # GridFS payload deletion + redirect tombstones for deleted versions
  management/commands/create_project.py  # CLI to create a project from local/HTTP/S3 file
caper/templates/       # Django templates (Mezzanine host-themes loader)
caper/schema/          # schema.json for validating MongoDB project documents
```

---

## Data Model (MongoDB)

Projects live in the `projects` collection. Notable fields:
- `private`: `"private"` | `"public"` | `"hidden_public"` (use `utils.normalize_visibility_field()` when reading legacy boolean values)
- `current: True` — only the latest version of a renamed/updated project
- `previous_versions` — list of prior-version entries, normally dicts with `linkid`, `date`, `AA_version`, `AC_version`, `ASP_version`, and `aggregator_version`
- `delete: False` — soft-delete flag
- `runs` — dict of run-name → list of sample dicts
- `project_members` — list of usernames/emails controlling access; form input starts as a string but MongoDB stores a list from `utils.create_user_list()`
- `AA_version`, `AC_version`, `ASP_version`, `aggregator_version` — tool version metadata that must be preserved in version history/API output
- `ecDNA_context` — lazily populated from `ecDNA_context_calls.tsv` files inside the project tarball

### Search and version-deletion conventions

Details are in [docs/data-model-notes.md](docs/data-model-notes.md). The two rules to carry:

- Keep `views.search_results()` and `search.perform_search()` semantics aligned
  when editing search or classification logic; no-amp samples are matched by
  `search._zero_feature_mask()`.
- Deleting an old version leaves a MongoDB tombstone that `utils.get_one_project()`
  resolves to the surviving project, and cleanup code must preserve it. Versions of
  one project share files, so every payload deleter must be told what to protect:
  pass what `version_file_sharing.still_needed_here()` returns. A version tagged as
  used in a publication takes a typed project name to delete; any path with nobody to
  ask must refuse it.

Files (tarballs from AmpliconSuiteAggregator) are stored in **GridFS** and referenced by ObjectId within the project document. Use `tar_utils.extract_from_project_tarfile()` to stream-extract specific paths.

---

## Developer Workflows

Procedures are in [docs/developer-workflows.md](docs/developer-workflows.md):
local dev server, the dev landing gate (`dev_gate.py`: home page only, never
authenticates, must not touch MongoDB), AWS SSO for S3 (`aws sso login --profile
amprepo`), Docker dev, creating a project from the command line
(`scripts/dev/local_upload_project.py`, not `manage.py create_project`), local MongoDB/GridFS
cleanup, `scripts/maintenance/recover_deleted_version.py`, and `caper/caper/shutdown_mode.py` during a deploy.
Deploy runbooks are in the
[wiki](https://github.com/AmpliconSuite/AmpliconRepository/wiki).


### Do NOT commit
- `caper/caper.sqlite3`
- `caper/config.sh` / `.env`
- `docs/handoff-*.md` — local-only working notes, intentionally untracked. Read
  them, never stage them. Nothing in `.gitignore` prevents `git add -A` or
  `git add docs/` from picking them up, so check `git status` before staging.
  (Other files under `docs/` — `*-todo.md`, `api-roadmap.md` — *are* tracked.)

---

## Auth & Social Login

- Uses `django-allauth` with **Google** and **Globus** OAuth2 providers.
- `CustomAccountAdapter` and `SocialAccountAdapter` (in `utils.py`) prevent username/email cross-collisions and respect the `registration_disabled` flag stored in MongoDB `system_settings`.
- `ACCOUNT_EMAIL_VERIFICATION = 'none'` — email verification is off.

---

## Mezzanine CMS Integration

Mezzanine provides the CMS page tree, admin UI (Grappelli), and URL catch-all. **Add all custom URL patterns above** the `path("", include("mezzanine.urls"))` line in `urls.py` — Mezzanine's catch-all will shadow anything placed after it.

---

## Test-Driven Development

For bug fixes and new features, **start by writing a failing test** before touching production code. This keeps changes focused and verifiable.

### Workflow

1. **Write a failing test** that reproduces the bug or exercises the new behaviour.
2. Confirm the test fails for the right reason.
3. Implement the minimal code change to make the test pass.
4. Verify no existing tests regressed.

### Running tests

Run `pytest` from the repository root after `source caper/config.sh`. Markers
(`integration`, `slow`, `functional`, `browser`, `performance`), the commands for
each tier, and the test-data link are in [docs/testing.md](docs/testing.md). Use
the narrowest marker that matches the behavior; add `slow` only for real
aggregation, `functional` only for the shared `loaded_datasets` flow or a local
server, `browser` for Playwright.

### Test addition policy

Do not add a new test automatically for every code edit. First search for the
nearest existing test that owns the behavior.

Add or strengthen a test when the change reproduces a bug, closes a numbered
issue, protects an outage, security, or data-integrity invariant, or introduces
a materially new branch that existing assertions do not exercise. For a
refactor, text/style-only change, or behavior already covered at the same
boundary, run the existing test and add nothing.

Prefer extending the nearest existing test and file. Create a new test file
only for a distinct subsystem or when it requires a different marker, fixture,
or execution environment. A one-test file needs a short reason why it does not
belong in an existing subject file.

Test at the lowest layer that proves the regression. Do not repeat the same
outcome with a mock, a direct view call, and a browser test unless each test
names the different failure boundary it protects.

`skip` and `xfail` may guard an unmet precondition checked before the system
under test runs. Never skip or xfail because the tested action failed. Assert
that fixtures created by the suite remain available.

Before adding a test file, state in the change summary which existing file was
considered and why it was not the right home.

---

## PR Checklist

- Never include `caper.sqlite3` in commits or PRs.
- Minimum manual smoke-test: home page, CCLE project page, any CCLE sample page.
- Versioned releases use tag pattern `v<major>.<minor>.<patch>_<MMDDYY>` (e.g., `v1.0.1_072523`).
