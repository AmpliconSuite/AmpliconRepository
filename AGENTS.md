# AGENTS.md — AmpliconRepository

A genomics data repository (ampliconrepository.org) for storing, browsing, and analysing DNA amplicon results produced by [AmpliconSuiteAggregator](https://github.com/AmpliconSuite/AmpliconSuiteAggregator). Built with Django + Mezzanine CMS.

---

## Critical: Environment Setup Before Any Django Command

**Always** source `caper/config.sh` before running any Django management command. It sets all required env vars (MongoDB URI, OAuth secrets, S3, Neo4j, email).

```bash
# Required pattern
source caper/config.sh && cd caper && python manage.py <command>

# Or use the helper script from project root
./run_django_command.sh <command>
```

Never commit `caper/config.sh` or `caper/.env` to version control.

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

### AC 2.0 / no-amp conventions

Search now treats no-amplification samples explicitly. The search UI has four real amplicon class filters (`ecDNA`, `linear amplification`, `BFB`, `complex non-cyclic`) plus the `no-amp` pseudo-filter. Backend no-amp matching uses `search._zero_feature_mask()` and includes:
- empty `runs[sample]` lists, converted to placeholder rows with `Classification='NA'`
- blank `Feature_ID`
- `Classification == 'No FSCNA'`
- `Classification == 'NA'`, the current AmpliconSuiteAggregator convention for samples with no amplification result

When editing search/classification logic, keep `views.search_results()` and `search.perform_search()` semantics aligned: no boxes checked and all five boxes checked both mean no filter; all four amp types checked without `no-amp` excludes no-amp rows.

### Deleted Version Tombstones

Deleting an old project version now purges its heavy GridFS payload but keeps a lightweight MongoDB tombstone with:
- `version_deleted_from_history: True`
- `payload_purged: True`
- `redirect_to_project: <current_project_id>`

`utils.get_one_project()` resolves these tombstones to the surviving project. Cleanup code must preserve them. Use `caper/caper/project_version_cleanup.py` helpers when changing version deletion behavior, especially `iter_gridfs_file_ids()`, `delete_gridfs_payload_for_project()`, and `build_deleted_version_tombstone()`.

Files (tarballs from AmpliconSuiteAggregator) are stored in **GridFS** and referenced by ObjectId within the project document. Use `tar_utils.extract_from_project_tarfile()` to stream-extract specific paths without writing the full tar to disk.

---

## Developer Workflows

### Local dev server
```bash
source caper/config.sh && cd caper && python manage.py runserver
# visit http://localhost:8000
```

### Docker dev (simplest for new setup)
```bash
mkdir -p logs tmp .aws .git
docker compose -f docker-compose-dev.yml build --no-cache
docker compose -f docker-compose-dev.yml up -d
# visit http://localhost:8000
docker compose -f docker-compose-dev.yml down
```

### Create a project from CLI
```bash
source caper/config.sh && cd caper && \
  python manage.py create_project <project_name> <username> <path_or_url.tar.gz> \
    --visibility public --description "My project"
```
Accepts local paths, HTTP URLs, or `s3://` URIs.

### Clean up local MongoDB / GridFS data

Prefer the safe two-stage cleanup before any full local purge:

```bash
source caper/config.sh

# 1. Remove unreachable project docs and their attached GridFS/S3/tmp files.
(cd caper && python ../cleanup_orphaned_projects.py --dry-run)
(cd caper && python ../cleanup_orphaned_projects.py)

# 2. Remove remaining GridFS blobs not referenced by reachable projects.
python purge-local-db.py --smart-gridfs
python purge-local-db.py --smart-gridfs --execute
```

Useful read-only reports:

```bash
source caper/config.sh && python purge-local-db.py --gridfs-usage-by-type --limit 25
source caper/config.sh && python purge-local-db.py --gridfs-usage-by-project --limit 25
source caper/config.sh && python purge-local-db.py --tarfile-report --limit 25
```

`cleanup_orphaned_projects.py` protects all reachable projects, previous versions, soft-deleted current projects, and redirect tombstones before deleting orphan project documents plus their GridFS/S3/tmp payloads. `purge-local-db.py` is dry-run by default; destructive operations require `--execute`. Its default `--reference-scope reachable` is conservative, and `--reference-strategy recursive` protects any ObjectId-like value in reachable project documents. Use `--reference-strategy app-fields` only when you intentionally want a more aggressive GridFS cleanup based on known GridFS fields.

The legacy full wipe is now explicit and local-dev-only: `python purge-local-db.py --all-project-data --execute`.

For project flag audits, run:
```bash
source caper/config.sh
(cd caper && python manage.py shell < ../check_project_flags_django.py)
```

To recover a deleted version tombstone into a project's `previous_versions`, use `recover_deleted_version.py`; it is dry-run by default and writes only with `--apply`.

### Do NOT commit
- `caper/caper.sqlite3`
- `caper/config.sh` / `.env`

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

Tests live in `tests/` and are run with `pytest` from the repository root, where `pytest.ini` defines markers:
- `integration`: live MongoDB, normally fast
- `slow`: real AmpliconSuiteAggregator project creation/reaggregation
- `functional`: depends on `loaded_datasets` and usually a local dev server
- `browser`: Playwright tests against `http://localhost:8000`
- `performance`: benchmarking only

```bash
# Fast integration tests only — MongoDB required, no aggregation/server.
source caper/config.sh && pytest -m "integration and not slow and not functional and not browser" -v

# All integration + functional tests — requires local server and AmpliconSuiteAggregator.
source caper/config.sh && pytest -m "integration and not browser" -v

# Full slow aggregation tests.
source caper/config.sh && pytest -m "slow and integration" -v

# Browser tests; start the server from caper/ in a separate shell first.
source caper/config.sh && pytest -m browser --base-url http://localhost:8000 -v

# Full suite
source caper/config.sh && pytest -v
```

### Test datasets

Additional test data files (tarballs, sample inputs, etc.) are available on Google Drive:
https://drive.google.com/drive/folders/1lp6NUPWg1C-72CQQeywucwX0swnBFDvu?usp=drive_link

New tests should use the narrowest marker that matches behavior. Use `@pytest.mark.integration` for live-Mongo unit/view behavior, add `@pytest.mark.slow` only when the test runs real aggregation, add `@pytest.mark.functional` when it depends on the shared `loaded_datasets` flow or local server, and add `@pytest.mark.browser` for Playwright.

```python
import pytest

@pytest.mark.integration
def test_my_feature(client, live_mongo):
    # arrange → act → assert
    ...
```

---

## PR Checklist

- Never include `caper.sqlite3` in commits or PRs.
- Minimum manual smoke-test: home page, CCLE project page, any CCLE sample page.
- Versioned releases use tag pattern `v<major>.<minor>.<patch>_<MMDDYY>` (e.g., `v1.0.1_072523`).
