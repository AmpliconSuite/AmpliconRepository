# Testing Guide

- [Test structure](#test-structure)
- [Running tests locally](#running-tests-locally)
- [Running tests in GitHub Actions](#running-tests-in-github-actions)
- [Secrets and configuration](#secrets-and-configuration)
- [AWS and S3](#aws-and-s3)
- [Adding tests to CI](#adding-tests-to-ci)

---

## Test structure

All tests live in `tests/` and are run with `pytest` from the repository root (where `pytest.ini` lives).

Tests are tagged with markers that control which tier they belong to:

| Marker | What it covers | Prerequisites |
|--------|----------------|---------------|
| `integration` | Unit-style tests against a live MongoDB connection; each completes in under a few seconds | MongoDB running locally |
| `slow` | Full end-to-end project creation with real aggregation; each test takes several minutes | MongoDB + AmpliconSuiteAggregator installed |
| `functional` | View-layer tests that reuse projects created by the `loaded_datasets` session fixture; depend on `slow` tests completing first | Everything for `slow` |
| `browser` | Playwright browser tests against a running dev server | Everything above + `playwright install chromium` + dev server on port 8000 |

Test files:

| File | Markers |
|------|---------|
| `tests/test_create_edit_project.py` | `slow`, `integration` |
| `tests/test_project_lifecycle.py` | `slow`, `integration` |
| `tests/test_search.py` | `slow`, `integration`, `functional` |
| `tests/test_downloads.py` | `integration`, `functional` |
| `tests/test_error_handling.py` | `integration`, `functional` |
| `tests/test_api.py` | `slow`, `integration` |
| `tests/test_browser.py` | `browser` |

---

## Running tests locally

### 1. Activate your environment

```bash
source ampliconenv/bin/activate   # python venv
# or
conda activate ampliconenv        # conda
```

### 2. Install the test runner (one-time)

```bash
pip install pytest
# For browser tests only:
pip install pytest-playwright && playwright install chromium
```

### 3. Start MongoDB

```bash
mongod --dbpath ~/data/db
```

### 4. Load environment variables

Source `config.sh` from the `caper/` directory:

```bash
source caper/config.sh
```

This exports all required secrets (OAuth keys, database URI, etc.) into your shell.
`conftest.py` also reads `caper/config.env` automatically at test startup as a fallback,
but shell exports from `config.sh` take precedence. You need `config.sh` sourced once per
shell session before running tests.

### 5. Test datasets

The small dataset is tracked in git (`test_data/one_amprepo_sample.tar.gz` and
`test_data/one_amprepo_sample.xlsx`).  The larger datasets used by `slow` and `functional`
tests are available from the
[shared Google Drive folder](https://drive.google.com/drive/folders/1lp6NUPWg1C-72CQQeywucwX0swnBFDvu?usp=share_link)
and should be placed in `test_data/`:

| File | Samples | Genome | Used by |
|------|---------|--------|---------|
| `one_amprepo_sample.tar.gz` | 1 | hg19 | all tiers (tracked in git) |
| `one_amprepo_sample.xlsx` | — | — | metadata for above (tracked in git) |
| `Contino_unagg_040423.tar.gz` | 9 | hg38 | `slow`, `functional` |
| `two_hg38_samples_no_ecdna.tar.gz` | 2 | hg38 | `slow` add-samples tests |

### 6. Run the tests

**Fast integration tests only** — no aggregation, safe to run any time (< 1 min total):
```bash
pytest -m "integration and not slow and not functional and not browser" -v
```

**All integration + functional tests** — requires AmpliconSuiteAggregator (~10 min):
```bash
pytest -m "integration and not browser" -v
```

**Full slow tests** — creates and aggregates real projects end-to-end:
```bash
pytest -m "slow and integration" -v
```

**Browser tests** — requires a running dev server:
```bash
# Terminal 1
cd caper && python manage.py runserver

# Terminal 2
pytest -m browser --base-url http://localhost:8000 -v
```

**Run everything** (takes 20+ minutes):
```bash
pytest -v
```

### Cleanup

Each test cleans up after itself in a `finally` block: MongoDB documents, `tmp/`
directories, and S3 objects are all removed even when a test fails.

---

## Running tests in GitHub Actions

The workflow at `.github/workflows/tests.yml` runs automatically on every push and
pull request to `main`. It spins up a MongoDB 6 service container and runs only the
fast integration tests:

```
pytest -m "integration and not slow and not functional and not browser"
```

Slow, functional, and browser tests are excluded from CI because they require
AmpliconSuiteAggregator, large test datasets not in the repository, and a running
dev server.

---

## Secrets and configuration

### How configuration works

Locally you source `config.sh`, which runs `export VAR=value` in your shell.
At test startup, `conftest.py` also reads `caper/config.env` using
`os.environ.setdefault()` — which means values already in the environment (from
`config.sh`) are never overwritten by `config.env`.

In GitHub Actions, the workflow's `env:` block is processed by the runner before any
Python code runs, so workflow env vars arrive in `os.environ` before `conftest.py`
reads `config.env`. Because `conftest.py` uses `setdefault()`, the workflow values
win over `config.env` values. The workflow `env:` block is therefore the CI equivalent
of sourcing `config.sh` — sensitive values are supplied via GitHub Secrets instead of
a local file.

### Which secrets to store in GitHub

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Where to get the value |
|-------------|------------------------|
| `GOOGLE_SECRET_KEY` | `config.sh` — `export GOOGLE_SECRET_KEY=...` |
| `GLOBUS_SECRET_KEY` | `config.sh` — `export GLOBUS_SECRET_KEY=...` |
| `RECAPTCHA_PRIVATE_KEY` | `config.sh` — `export RECAPTCHA_PRIVATE_KEY=...` |
| `RECAPTCHA_PUBLIC_KEY` | `config.sh` — `export RECAPTCHA_PUBLIC_KEY=...` |

The workflow references these as `${{ secrets.SECRET_NAME }}` with a `ci-placeholder`
fallback so that CI does not fail if the secrets haven't been configured yet:

```yaml
GOOGLE_SECRET_KEY: ${{ secrets.GOOGLE_SECRET_KEY || 'ci-placeholder' }}
```

The fast integration tests never exercise OAuth or ReCaptcha, so placeholder values
are sufficient for those tests. If you later add browser tests to CI that log in via
the email/password form, the real keys are needed.

### Values that never need to be in GitHub Secrets

These are either safe to hard-code in the workflow or overridden to CI-appropriate
values:

| Variable | CI value | Reason |
|----------|----------|--------|
| `DB_URI_SECRET` | `mongodb://localhost:27017` | Points to the MongoDB service container |
| `DB_NAME` | `caper-ci-test` | Isolated test database |
| `DJANGO_SECRET_KEY` | `ci-test-secret-key-not-for-production` | Only needs to be a non-empty string for Django startup |
| `S3_FILE_DOWNLOADS` | `FALSE` | Disables all S3 boto3 calls (see AWS section below) |
| `S3_STATIC_FILES` | `FALSE` | Tests call views directly, not a running server |
| `SECURE_SSL_REDIRECT` | `FALSE` | No TLS in CI |

Email, Mailjet, and Neo4j variables are not set in the workflow because no currently-enabled
test exercises those code paths.

---

## AWS and S3

The application has two distinct S3 use cases, controlled by separate environment variables:

**Static files** (`S3_STATIC_FILES`) — CSS, JS, images served from S3 in production.
Tests call view functions directly via `RequestFactory`; no browser fetches static files,
so this is irrelevant to the test suite. Set to `FALSE` in CI.

**Project download files** (`S3_FILE_DOWNLOADS`) — when `TRUE`, `project_download` uploads
the generated tar to S3 and returns a presigned redirect URL; when `FALSE` it streams the
file from `tmp/` on disk. Setting `S3_FILE_DOWNLOADS=FALSE` in the workflow means boto3 is
never called during CI, so **no AWS credentials are needed for the current test configuration**.

### Adding AWS access for future slow/functional tests in CI

If slow or functional tests are ever added to the CI workflow, they create real projects and
test downloads, which requires live S3 access. The recommended approach is **GitHub OIDC
federation** — no AWS access keys are stored anywhere:

1. **Register GitHub as an OIDC provider** in your AWS account (one-time, in the IAM console).

2. **Create an IAM role** (`amprepo-ci-role`) with:
   - A trust policy that accepts tokens from `token.actions.githubusercontent.com` scoped to
     your repository:
     ```json
     {
       "Effect": "Allow",
       "Principal": { "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com" },
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringLike": { "token.actions.githubusercontent.com:sub": "repo:AmpliconSuite/AmpliconRepository:*" }
       }
     }
     ```
   - A permission policy that allows only what CI tests need:
     ```json
     {
       "Effect": "Allow",
       "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
       "Resource": "arn:aws:s3:::amprepo-private/ci-test/*"
     }
     ```

3. **Add the OIDC step to the workflow**, before the test step:
   ```yaml
   permissions:
     id-token: write
     contents: read

   steps:
     - uses: actions/checkout@v4

     - name: Configure AWS credentials via OIDC
       uses: aws-actions/configure-aws-credentials@v4
       with:
         role-to-assume: arn:aws:iam::YOUR_ACCOUNT_ID:role/amprepo-ci-role
         aws-region: us-east-1
   ```

With OIDC the runner receives a short-lived session token at runtime; no AWS credentials
appear in GitHub repository secrets or in any configuration file.

---

## Adding tests to CI

To promote a test tier from local-only to CI-enabled, edit `.github/workflows/tests.yml`:

- **Remove a marker exclusion** from the `pytest -m` expression.
- **Add any new prerequisites** as workflow steps before the test step (e.g. install
  AmpliconSuiteAggregator, configure AWS credentials, start the dev server).
- **Add large test datasets** either by committing them (small files only), downloading
  them from S3/Google Drive in a workflow step, or using
  [GitHub Actions cache](https://docs.github.com/en/actions/using-workflows/caching-dependencies-to-speed-up-workflows)
  to avoid re-downloading on every run.
