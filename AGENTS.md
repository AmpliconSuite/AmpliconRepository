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
  management/commands/create_project.py  # CLI to create a project from local/HTTP/S3 file
caper/templates/       # Django templates (Mezzanine host-themes loader)
caper/schema/          # schema.json for validating MongoDB project documents
```

---

## Data Model (MongoDB)

Projects live in the `projects` collection. Notable fields:
- `private`: `"private"` | `"public"` | `"hidden_public"` (use `utils.normalize_visibility_field()` when reading legacy boolean values)
- `current: True` — only the latest version of a renamed/updated project
- `previous_versions` — list of prior project `_id`s (version chain)
- `delete: False` — soft-delete flag
- `runs` — dict of run-name → list of sample dicts
- `project_members` — comma-separated usernames/emails controlling access

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

### Purge local MongoDB data
```bash
python purge-local-db.py
```

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

Tests live in `tests/`. There are two suites:

```bash
# Fast suite (mocked DB — no live MongoDB required)
source caper/config.sh && cd caper && python -m pytest ../tests/ -m "not slow" -v

# Slow suite (requires live MongoDB — the default for new tests)
source caper/config.sh && cd caper && python -m pytest ../tests/ -m slow -v

# Full suite
source caper/config.sh && cd caper && python -m pytest ../tests/ -v
```

New tests go in the **slow suite** by default (mark with `@pytest.mark.slow`). Only move a test to the fast suite if it genuinely requires no database access and can be fully covered by mocks.

```python
import pytest

@pytest.mark.slow
def test_my_feature(client, live_mongo):
    # arrange → act → assert
    ...
```

---

## PR Checklist

- Never include `caper.sqlite3` in commits or PRs.
- Minimum manual smoke-test: home page, CCLE project page, any CCLE sample page.
- Versioned releases use tag pattern `v<major>.<minor>.<patch>_<MMDDYY>` (e.g., `v1.0.1_072523`).

