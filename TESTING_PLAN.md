# Caper Test Suite Expansion Plan

## Context

The AmpliconRepository (Caper) application is a Django + MongoDB bioinformatics portal
for amplicon analysis projects. This plan expands the existing pytest suite with
functional integration tests, browser-level E2E tests, and CI/CD automation.
The suite loads real test datasets, runs all tests, then tears everything down.

---

## Codebase at a Glance

- **Framework:** Django 4.0.6 + MongoDB (MongoEngine/pymongo) + optional Neo4j
- **Main app:** `caper/caper/caper/` — `views.py`, `views_apis.py`, `models.py`, `urls.py`
- **Existing pytest setup:** `pytest.ini` at repo root; test discovery path = `tests/`
- **Existing tests:** `tests/test_create_edit_project.py` (4 integration tests)
- **Existing fixtures:** `tests/conftest.py` + root `conftest.py` (Django setup)

### Test datasets (already present in `test_datasets/`)

| File | Samples | Genome | Metadata |
|---|---|---|---|
| `one_amprepo_sample.tar.gz` | 1 | hg19 | `one_amprepo_sample.xlsx` included |
| `Contino_unagg_040423.tar.gz` | 9 | hg38 | None |
| `two_hg38_samples_no_ecdna.tar.gz` | 2 | hg38 | None — no ecDNA samples |

---

## Known Bug: Wrong Test Data Path in `tests/conftest.py`

`TEST_DATA_DIR` currently points to `test_data/` but files live in `test_datasets/`.
Fix this before running any tests:

```python
# tests/conftest.py  — change line 15
TEST_DATA_DIR = os.path.join(REPO_ROOT, 'test_datasets')   # was 'test_data'
```

Also update the path constants below it:

```python
TAR_FILE  = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.tar.gz')
XLSX_FILE = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.xlsx')
```

---

## Note on `performance_test.py`

`performance_test.py` (repo root) is a **standalone HTTP load-testing tool**, not a
pytest test. It fires concurrent `requests.get()` calls against a running server and
reports latency percentiles (p95, p99). It requires a live server, has no pass/fail
threshold, and is specifically designed to compare dev server vs Gunicorn throughput.
Converting it to pytest would be a misuse of both tools.

**Recommendation:** Move it to `tools/performance_test.py` and document it in the
README. Do not convert it to pytest.

```bash
# Usage (server must be running):
python tools/performance_test.py --url http://localhost:8000/ --requests 100 --concurrency 10
```

---

## Phase 1 — Fix `tests/conftest.py`

### 1.1  Move shared helpers out of `test_create_edit_project.py`

`_build_create_request`, `_poll_until_finished`, `_project_id_from_redirect`, and
`_cleanup_project` are currently defined in `tests/test_create_edit_project.py` and
would be imported from there by the new `loaded_datasets` fixture. Importing from one
test file into another is a pytest anti-pattern — if that file is renamed or refactored
the import silently breaks.

Move all four helpers into `tests/conftest.py` as module-level functions (not fixtures).
Update `tests/test_create_edit_project.py` to import them from `conftest` instead.

### 1.2  Add dataset path constants

```python
# tests/conftest.py
REPO_ROOT     = os.path.dirname(os.path.dirname(__file__))
TEST_DATA_DIR = os.path.join(REPO_ROOT, 'test_datasets')   # fix: was 'test_data'

DATASET_SMALL_TAR  = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.tar.gz')
DATASET_SMALL_XLSX = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.xlsx')
DATASET_MEDIUM_TAR = os.path.join(TEST_DATA_DIR, 'Contino_unagg_040423.tar.gz')
DATASET_ADDL_TAR   = os.path.join(TEST_DATA_DIR, 'two_hg38_samples_no_ecdna.tar.gz')
```

### 1.3  Add `admin_user` and `non_member_user` fixtures

```python
@pytest.fixture(scope='session')
def admin_user():
    """Mock superuser for admin-only tests (featured projects, etc.)."""
    class _AdminUser:
        username = 'pytest_admin_user'
        email    = 'pytest_admin@example.com'
        is_staff = True
        is_active = True
        is_authenticated = True
        is_superuser = True
        def __str__(self): return self.username
    return _AdminUser()

@pytest.fixture(scope='session')
def non_member_user():
    """A second non-owner user for access-control tests."""
    class _NonMember:
        username = 'pytest_non_member'
        email    = 'pytest_nonmember@example.com'
        is_staff = False
        is_active = True
        is_authenticated = True
        is_superuser = False
        def __str__(self): return self.username
    return _NonMember()
```

### 1.4  Add `loaded_datasets` session-scoped fixture

This fixture creates the two persistent test projects once per session.
All Phase 2 tests depend on it.

```python
@pytest.fixture(scope='session')
def loaded_datasets(request_factory, test_user, mongo_collection):
    """
    Creates two projects from real test data, waits for aggregation,
    yields project IDs and known metadata values, then cleans up.

    Uses the smallest datasets to keep session setup fast.
    Tests that need the 9-sample Contino dataset create their own projects.
    """
    assert os.path.exists(DATASET_SMALL_TAR),  f"Missing: {DATASET_SMALL_TAR}"
    assert os.path.exists(DATASET_MEDIUM_TAR), f"Missing: {DATASET_MEDIUM_TAR}"

    created_ids = []

    req_a, handles_a = _build_create_request(
        request_factory, test_user, 'FuncTest_Small',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
    try:
        from caper.views import create_project
        resp_a = create_project(req_a)
    finally:
        for h in handles_a: h.close()
    id_a = _project_id_from_redirect(resp_a)
    created_ids.append(id_a)

    req_b, handles_b = _build_create_request(
        request_factory, test_user, 'FuncTest_Medium',
        tar_path=DATASET_MEDIUM_TAR)
    try:
        resp_b = create_project(req_b)
    finally:
        for h in handles_b: h.close()
    id_b = _project_id_from_redirect(resp_b)
    created_ids.append(id_b)

    doc_a = _poll_until_finished(mongo_collection, id_a)
    doc_b = _poll_until_finished(mongo_collection, id_b)
    assert doc_a and not doc_a.get('aggregation_failed'), "Small dataset aggregation failed"
    assert doc_b and not doc_b.get('aggregation_failed'), "Medium dataset aggregation failed"

    yield {
        'project_small':  id_a,   # 1 sample, hg19, has xlsx metadata
        'project_medium': id_b,   # 9 samples, hg38, no metadata
        # Override these env vars if your local datasets have different values:
        'gene_in_small':   os.environ.get('DATASET_SMALL_GENE',   'MYC'),
        'tissue_in_small': os.environ.get('DATASET_SMALL_TISSUE', 'GBM'),
        'gene_in_medium':  os.environ.get('DATASET_MEDIUM_GENE',  'EGFR'),
        'tissue_in_medium':os.environ.get('DATASET_MEDIUM_TISSUE','Lung'),
    }

    for pid in created_ids:
        _cleanup_project(mongo_collection, pid)
```

**Environment variable overrides** — set these to match what is actually in your datasets:

```bash
export DATASET_SMALL_GENE=MYC
export DATASET_SMALL_TISSUE=GBM
export DATASET_MEDIUM_GENE=EGFR
export DATASET_MEDIUM_TISSUE=Lung
```

### 1.5  Update `pytest.ini`

```ini
[pytest]
testpaths = tests

markers =
    slow: full aggregation pipeline (several minutes)
    integration: requires live MongoDB
    performance: benchmarking (skip by default; use tools/performance_test.py instead)
    functional: end-to-end tests that depend on the loaded_datasets fixture
    browser: Playwright browser tests (requires running dev server)

addopts = -p no:logfire -p no:django -q --tb=short -r a -W ignore
```

---

## Phase 2 — New Integration Test Files

### Isolation rule for mutation tests

Tests that modify project state (privacy, featured flag, membership) **must not share
projects with other tests.** Each such test creates its own short-lived project in a
`try/finally` block and cleans it up before returning. Never modify `loaded_datasets`
projects in-place — if an intermediate assertion fails, the fixture state is corrupted
for all subsequent tests.

---

### `tests/test_project_lifecycle.py`

*Covers: project visibility, featured flag, membership, version history.*

```
test_project_starts_private
    - Assert loaded_datasets.project_small has private='private' in MongoDB
    - Fixtures: loaded_datasets, mongo_collection

test_change_visibility_cycle
    - Creates a dedicated short-lived project (DATASET_SMALL_TAR)
    - Sets private='public'; verifies unauthenticated GET /project/<id> returns 200
    - Sets featured=True (as admin_user); verifies GET / includes project name
    - Sets private='private'; verifies unauthenticated access returns 302/403
    - Cleans up in finally block regardless of outcome
    - Fixtures: request_factory, test_user, admin_user, mongo_collection

test_add_and_remove_project_member
    - Creates a dedicated short-lived project (DATASET_SMALL_TAR)
    - Adds non_member_user to project_members; verifies non_member_user gets 200
    - Removes non_member_user; verifies access is now denied
    - Cleans up in finally block
    - Fixtures: request_factory, test_user, non_member_user, mongo_collection

test_replace_project_file_version_history
    - Creates a dedicated short-lived project from DATASET_SMALL_TAR
    - Waits for aggregation; re-uploads with project_mode='reaggregate'
    - Waits for new version; asserts MongoDB has a previous_versions entry or new doc
    - Asserts GET /project/<id>/download returns the archive
    - xfail if aggregator does not support remap (follow existing xfail pattern)
    - Cleans up both project IDs (original + reaggregated) in finally block
    - Fixtures: request_factory, test_user, mongo_collection
```

Key views: `views.create_project`, `views.edit_project_page`, `views.project_page`, `views.index`

---

### `tests/test_search.py`

*Covers: gene search, full-text search, tissue filtering, access-control on search results.*

```
test_gene_search_returns_results
    - GET /gene-search/?genequery=<gene_in_small>
    - Assert ≥1 sample row, project_small in results
    - Fixtures: loaded_datasets, request_factory, test_user

test_gene_search_no_results
    - GET /gene-search/?genequery=NONEXISTENTXYZ
    - Assert result list is empty
    - Fixtures: loaded_datasets, request_factory, test_user

test_gene_search_with_classification_filter
    - GET /gene-search/?genequery=<gene_in_small>&classquery=ECDNA
    - Assert all returned samples have 'ecDNA' in their Classifications
    - Fixtures: loaded_datasets, request_factory, test_user

test_gene_search_tissue_filter
    - GET /gene-search/?metadata_cancer_tissue=<tissue_in_small>
    - Assert small-dataset samples appear; medium-dataset samples (different tissue) do not
    - Fixtures: loaded_datasets, request_factory, test_user

test_fulltext_search_by_gene
    - POST /search_results/ with genequery=<gene_in_small> on a public project
    - Assert 200 and public_sample_data non-empty
    - Fixtures: loaded_datasets, request_factory, test_user

test_fulltext_search_by_classification
    - POST /search_results/ with classquery=ecDNA
    - Assert results contain ecDNA samples
    - Fixtures: loaded_datasets, request_factory, test_user

test_fulltext_search_no_match
    - POST /search_results/ with project_name=ZZZNOMATCH
    - Assert both public/private sample lists are empty
    - Fixtures: loaded_datasets, request_factory, test_user

test_search_private_visible_to_member
    - project_small is private; search as test_user (owner)
    - Assert project_small samples appear in private_sample_data
    - Fixtures: loaded_datasets, request_factory, test_user, mongo_collection

test_search_private_hidden_to_nonmember
    - project_small is private; search as non_member_user
    - Assert project_small samples do NOT appear
    - Fixtures: loaded_datasets, request_factory, non_member_user
```

Key views: `views.gene_search_page`, `views.search_results`

---

### `tests/test_downloads.py`

*Covers: tar download, metadata CSV, summary HTML, per-sample download, GridFS PNG.*

```
test_project_tar_download
    - GET /project/<id>/download as authenticated user
    - Assert status 200 (or 302 to S3); Content-Type contains 'gzip' or 'octet-stream'
    - Fixtures: loaded_datasets, request_factory, test_user

test_project_metadata_csv_download
    - GET /project/<id>/download_metadata
    - Assert 200, Content-Type text/csv, non-empty body
    - Fixtures: loaded_datasets, request_factory, test_user

test_project_summary_html_download
    - GET /project/<id>/download_summary
    - Assert 200, Content-Type text/html
    - Fixtures: loaded_datasets, request_factory, test_user

test_sample_download
    - Retrieve first sample name from MongoDB doc for project_small
    - GET /project/<id>/sample/<name>/download
    - Assert status 200, content non-empty
    - Fixtures: loaded_datasets, request_factory, test_user, mongo_collection

test_sample_png_exists_in_gridfs
    - Assert MongoDB project_small doc references GridFS PNG object IDs for ≥1 sample
    - Fixtures: loaded_datasets, mongo_collection

test_pdf_download
    - Retrieve first sample + feature from project_small doc
    - GET /project/<id>/sample/<name>/feature/<fname>/download/pdf/<fid>
    - Assert 200, Content-Type application/pdf
    - pytest.skip if project_small has no PDF features
    - Fixtures: loaded_datasets, request_factory, test_user, mongo_collection
```

Key views: `views.project_download`, `views.project_metadata_download`,
`views.project_summary_download`, `views.sample_download`, `views.pdf_download`

---

### `tests/test_error_handling.py`

*Covers: missing file upload, bad project IDs, auth redirects, missing samples.*

```
test_create_project_without_file
    - POST /create-project/ with no 'document' file attached
    - Assert no new MongoDB document created (response is not a redirect to a new project)
    - Fixtures: request_factory, test_user, mongo_collection

test_project_page_nonexistent_id
    - GET /project/000000000000000000000000 (valid ObjectId format, non-existent doc)
    - Assert response 404 or redirect
    - Fixtures: request_factory, test_user

test_download_nonexistent_project
    - GET /project/000000000000000000000000/download
    - Assert response 404 or redirect
    - Fixtures: request_factory, test_user

test_private_project_unauthenticated_redirect
    - GET /project/<project_small> with AnonymousUser
    - Assert response 302 (redirect to login) or 403
    - Fixtures: loaded_datasets, request_factory, mongo_collection

test_sample_page_nonexistent_sample
    - GET /project/<id>/sample/NOSUCHSAMPLE
    - Assert 404 or graceful error page (not 500)
    - Fixtures: loaded_datasets, request_factory, test_user
```

---

### `tests/test_api.py`

*Covers: CLI upload API, add-samples API, background task status.*

The `two_hg38_samples_no_ecdna.tar.gz` dataset is used here to add samples to an
existing Contino project, exercising the add-samples path without needing ecDNA data.

```
test_upload_api_creates_project
    - POST /upload_api/ with DATASET_MEDIUM_TAR and API key header
    - Assert 200 JSON response with project_id
    - Assert new document appears in MongoDB
    - Poll until FINISHED?=True; clean up in finally block
    - pytest.skip if API_SECRET_KEY not set in environment
    - Fixtures: request_factory, test_user, mongo_collection

test_add_samples_to_project_api
    - Create a project from DATASET_MEDIUM_TAR (dedicated, not loaded_datasets)
    - Wait for aggregation; record initial sample_count
    - POST /add_samples_to_project_api/ with DATASET_ADDL_TAR
      (two_hg38_samples_no_ecdna.tar.gz)
    - Assert 200 and sample_count increased in MongoDB
    - Clean up in finally block
    - pytest.skip if API_SECRET_KEY not set in environment
    - Fixtures: request_factory, test_user, mongo_collection

test_background_task_status_api
    - Trigger a background task (create a project)
    - GET /api/background-task-status/?task_id=<id>
    - Assert 200 and JSON contains a 'status' key
    - Clean up created project in finally block
    - Fixtures: request_factory, test_user, mongo_collection
```

Key views: `views_apis.FileUploadView`, `views_apis.ProjectFileAddView`,
`views_apis.BackgroundTaskStatusView`

---

## Phase 3 — Browser (Playwright) Tests

The integration tests in Phase 2 call Django view functions directly. They exercise
business logic but do not test URL routing, template rendering, or JavaScript. The
user-journey rows in the test matrix require a real browser.

Install:
```bash
pip install pytest-playwright
playwright install chromium
```

### `tests/test_browser.py`

All browser tests are marked `@pytest.mark.browser` and require a running dev server.
Run them with:
```bash
pytest -m browser --base-url http://localhost:8000 -v
```

```
test_homepage_loads
    - Navigate to /
    - Assert page title visible
    - Assert at least one project card or "no featured projects" message renders

test_gene_search_form_submits
    - Navigate to /gene-search/
    - Fill in the gene input; submit
    - Assert results table appears (or empty-state message — not a 500 page)

test_search_results_render
    - Navigate to /search_results/ via form POST
    - Assert the results section is visible

test_project_page_renders
    - Authenticate via session cookie (force_login helper) or visit a public project
    - Assert sample table rows are present
    - Assert download buttons are visible

test_private_project_redirects_to_login
    - Visit /project/<private_id> without a session
    - Assert redirected to /accounts/login/ or similar

test_create_project_form_validation
    - Navigate to /create-project/
    - Submit form without a file
    - Assert error message visible (not a 500)
```

**Note on OAuth:** Automated Google/Globus login cannot be driven through the browser
tests without a real account or a mock OAuth provider (e.g., `pytest-oauth2-proxy`).
Browser tests that require login should use Django's `force_login()` to set a session
cookie before Playwright navigates, or restrict themselves to publicly accessible pages.

---

## Phase 4 — GitHub Actions CI/CD

Create `.github/workflows/tests.yml`.

### Design decisions

- Only `integration` tests run in CI; `slow` (full aggregation pipeline) tests are
  skipped to keep runs under ~10 minutes and well within the 2,000 free minutes/month
  on private repos (unlimited on public repos).
- `functional` and `browser` tests also require the `slow` aggregation pipeline so they
  are skipped in CI. They are local-only until a CI artifact strategy is in place for
  the test dataset files (which cannot be committed to the repo).
- MongoDB is provided as a Docker service container — no paid add-ons required.

```yaml
# .github/workflows/tests.yml
name: Tests

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      mongo:
        image: mongo:6
        ports:
          - 27017:27017
        options: >-
          --health-cmd "mongosh --eval 'db.runCommand(\"ping\").ok'"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-django

      - name: Run integration tests (exclude slow/functional/browser)
        env:
          DJANGO_SETTINGS_MODULE: caper.settings
          MONGO_HOST: localhost
          MONGO_PORT: 27017
        run: |
          pytest -m "integration and not slow" -v
```

---

## Phase 5 — README Update

Update the "Running Tests" section to document all test categories:

```bash
# Prerequisites
source ampliconenv/bin/activate
pip install pytest pytest-django pytest-playwright
playwright install chromium   # only needed for browser tests
mongod --dbpath ~/data/db
source caper/config.sh

# Set env vars to match your actual datasets
export DATASET_SMALL_GENE=MYC
export DATASET_SMALL_TISSUE=GBM
export DATASET_MEDIUM_GENE=EGFR
export DATASET_MEDIUM_TISSUE=Lung

# Fast integration tests (no aggregation pipeline; runs in CI)
pytest -m "integration and not slow" -v

# Full integration tests including aggregation (several minutes per test)
pytest -m "integration" -v

# Functional tests (requires loaded_datasets; ~10-20 min)
pytest -m "functional and integration" -v

# Browser tests (requires running dev server on :8000)
pytest -m browser --base-url http://localhost:8000 -v

# Run everything
pytest -v

# Load/performance benchmarking (standalone tool, not pytest)
python tools/performance_test.py --url http://localhost:8000/ --requests 100 --concurrency 10
```

---

## Complete File Change Summary

| File | Action |
|---|---|
| `tests/conftest.py` | **Fix path bug** (`test_data` → `test_datasets`); **move helpers** from `test_create_edit_project.py`; **add** `loaded_datasets`, `admin_user`, `non_member_user` fixtures |
| `tests/test_create_edit_project.py` | **Update imports** — helpers now come from `conftest` |
| `tests/test_project_lifecycle.py` | **Create** (Phase 2) |
| `tests/test_search.py` | **Create** (Phase 2) |
| `tests/test_downloads.py` | **Create** (Phase 2) |
| `tests/test_error_handling.py` | **Create** (Phase 2) |
| `tests/test_api.py` | **Create** (Phase 2) |
| `tests/test_browser.py` | **Create** (Phase 3) |
| `pytest.ini` | **Extend** — add `functional`, `browser`, `performance` markers |
| `.github/workflows/tests.yml` | **Create** (Phase 4) |
| `tools/performance_test.py` | **Move** from `performance_test.py` (repo root) — no conversion |
| `README.md` | **Update** — testing instructions |

---

## Important Implementation Notes

1. **Mutation tests must use dedicated projects** — Never modify `loaded_datasets`
   projects (privacy, members, featured flag). Create a short-lived project in each
   mutation test, perform the mutations, and clean up in a `try/finally` block.

2. **Helpers belong in `conftest.py`** — `_build_create_request`, `_poll_until_finished`,
   `_project_id_from_redirect`, and `_cleanup_project` must be module-level functions
   in `tests/conftest.py`, not imported from `test_create_edit_project.py`.

3. **`xfail` + cleanup** — After calling `pytest.xfail()`, the rest of the test
   function does NOT execute. Cleanup must happen in a `finally` block *before* the
   `xfail` call, or use a fixture with its own teardown section.

4. **`test_add_samples_to_project_api` is independent** — It must create its own project
   rather than relying on `loaded_datasets.project_medium`. Test ordering within a file
   is not guaranteed.

5. **OAuth login stays manual** — Google/Globus auth flows are not covered by the
   automated suite. The `admin_user`, `test_user`, and `non_member_user` fixtures bypass
   auth middleware entirely. Browser tests use `force_login()` session cookies.

6. **MongoDB connection** — All integration tests use the live `collection_handle` from
   `caper.views`. There is no mock. Tests require MongoDB at `localhost:27017`.

7. **`performance_test.py` stays standalone** — It measures latency percentiles against
   a live HTTP server and has no pass/fail semantics. Do not convert it to pytest;
   move it to `tools/` and call it directly from the command line.
