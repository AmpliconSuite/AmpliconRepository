"""
Shared pytest fixtures and test helpers for the AmpliconRepository test suite.

Django is initialised by the root conftest.py's pytest_configure hook before
any fixtures run.  Django imports are kept inside fixture functions (lazy) so
they don't execute at module import time, which would precede Django setup.

Helper functions (_build_create_request, etc.) are defined here as plain
module-level functions so all test modules can import them without importing
from another test file (which is a pytest anti-pattern).
"""

import logging
import os
import shutil
import time

import pytest
from bson.objectid import ObjectId

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT     = os.path.dirname(os.path.dirname(__file__))
TEST_DATA_DIR = os.path.join(REPO_ROOT, 'test_data')
TMP_DIR       = os.path.join(REPO_ROOT, 'tmp')

DATASET_SMALL_TAR  = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.tar.gz')
DATASET_SMALL_XLSX = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.xlsx')
DATASET_MEDIUM_TAR = os.path.join(TEST_DATA_DIR, 'Contino_unagg_040423.tar.gz')
DATASET_ADDL_TAR   = os.path.join(TEST_DATA_DIR, 'two_hg38_samples_no_ecdna.tar.gz')
# CoRAL reconstructions plus the AmpliconClassifier run over them. See
# test_data/README.md for what is in it and how to rebuild it.
DATASET_CORAL_TAR  = os.path.join(TEST_DATA_DIR, 'coral_four_samples.tar.gz')

# AmpliconArchitect runs carrying an AmpliconClassifier 2.0 classification. Three
# of them, because the three things worth covering do not occur together in one
# cohort: plain AC 2.0 ingestion, FAN feature rows, and a GRCh38 project with
# every classification AC emits. test_data/README.md has the rebuild recipes.
DATASET_AC2_TAR      = os.path.join(TEST_DATA_DIR, 'ac2_nine_samples.tar.gz')
DATASET_AC2_FAN_TAR  = os.path.join(TEST_DATA_DIR, 'ac2_five_fan_samples.tar.gz')
DATASET_AC2_HG38_TAR = os.path.join(TEST_DATA_DIR, 'ac2_four_samples_hg38.tar.gz')

# Legacy aliases so existing tests that reference TAR_FILE / XLSX_FILE still work
TAR_FILE  = DATASET_SMALL_TAR
XLSX_FILE = DATASET_SMALL_XLSX

# ---------------------------------------------------------------------------
# Aggregation polling
# ---------------------------------------------------------------------------
POLL_TIMEOUT  = 300  # seconds to wait for background aggregation
POLL_INTERVAL = 5    # polling frequency in seconds


# ---------------------------------------------------------------------------
# Shared test helpers — import these directly in test modules
# ---------------------------------------------------------------------------

def _build_create_request(request_factory, user, project_name, *,
                           tar_path, xlsx_path=None, remap=False):
    """Return a POST request that mimics the create-project form."""
    data = {
        'project_name': project_name,
        'description': f'Automated pytest — {project_name}',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'true' if remap else 'false',
        'accept_license': 'on',
    }
    files = {}
    handles = []

    fh = open(tar_path, 'rb')
    handles.append(fh)
    files['document'] = fh

    if xlsx_path:
        fh2 = open(xlsx_path, 'rb')
        handles.append(fh2)
        files['metadataFile'] = fh2

    request = request_factory.post('/create-project/',
                                   data={**data, **files},
                                   format='multipart')
    request.user = user
    return request, handles


def _build_edit_request(request_factory, user, project_id, *,
                         project_name='Test_EditProject', xlsx_path=None, remap=False):
    """Return a POST request that mimics the edit-project form with reaggregate."""
    data = {
        'project_name': project_name,
        'description': f'Automated pytest — edit {project_name}',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'true' if remap else 'false',
        'project_mode': 'reaggregate',
        'accept_license': 'on',
    }
    files = {}
    handles = []

    if xlsx_path:
        fh = open(xlsx_path, 'rb')
        handles.append(fh)
        files['metadataFile'] = fh

    request = request_factory.post(f'/project/{project_id}/edit',
                                   data={**data, **files},
                                   format='multipart')
    request.user = user
    return request, handles


def _project_id_from_redirect(response):
    """Parse the project ID from a redirect Location header (/project/<id>)."""
    location = response.get('Location', '')
    parts = [p for p in location.split('/') if p]
    return parts[-1] if parts else None


def _poll_until_finished(collection, project_id,
                          timeout=POLL_TIMEOUT, interval=POLL_INTERVAL):
    """
    Poll MongoDB until the project is fully done: FINISHED?=True or
    aggregation_failed=True.  Waiting for FINISHED? (rather than just for
    aggregation_in_progress to clear) ensures that extract_project_files —
    which runs in a second background thread after _create_project — has
    also completed.  Returns the final document, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = collection.find_one({'_id': ObjectId(project_id)})
        if doc is None:
            return None
        if doc.get('FINISHED?', False) or doc.get('aggregation_failed', False):
            return doc
        time.sleep(interval)
    return None


def _cleanup_project(collection, project_id):
    """
    Fully remove all artifacts created for a test project:
      1. Project tarball and extracted feature files in GridFS
      2. MongoDB document
      3. tmp/{project_id}/ directory on disk
      4. S3 object (when USE_S3_DOWNLOADS is True)
    Errors in any step are logged but do not raise so all steps always run.
    """
    # Through the application's own walker and deleter, not a list of file keys
    # kept here.  The list that used to live here was the fifth hand-maintained
    # copy of that list in this repo, and it named 16 of the 35 spellings the
    # application recognises: only the underscore forms, though the upload path
    # writes the space-separated ones and a later step rewrites them.  Nothing
    # is known to be stored under the missing spellings today, which is exactly
    # how these copies go wrong -- the two others that fell behind were 8 keys
    # short each, and one of them classified 80,170 live files as garbage.
    # Deleting through delete_gridfs_file() also batches, which the project
    # tarfile needs and fs_handle.delete() does not do.
    try:
        from caper.project_version_cleanup import delete_gridfs_payload_for_project
        from caper.utils import delete_gridfs_file

        project = collection.find_one({'_id': ObjectId(project_id)}) or {}
        deleted = delete_gridfs_payload_for_project(delete_gridfs_file, project)
        logging.info(f"[cleanup] Deleted {deleted} GridFS file(s) for {project_id}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not inspect GridFS artifacts for {project_id}: {e}")

    try:
        collection.delete_one({'_id': ObjectId(project_id)})
        logging.info(f"[cleanup] Deleted MongoDB document {project_id}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not delete MongoDB document {project_id}: {e}")

    tmp_path = os.path.join(TMP_DIR, project_id)
    try:
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)
            logging.info(f"[cleanup] Removed tmp dir {tmp_path}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not remove tmp dir {tmp_path}: {e}")

    try:
        from django.conf import settings
        if getattr(settings, 'USE_S3_DOWNLOADS', False):
            import boto3
            bucket_path = getattr(settings, 'S3_DOWNLOADS_BUCKET_PATH', '')
            s3_key = f'{bucket_path}{project_id}/{project_id}.tar.gz'
            session = boto3.Session(profile_name=getattr(settings, 'AWS_PROFILE_NAME', None))
            s3_client = session.client('s3')
            s3_client.delete_object(Bucket=settings.S3_DOWNLOADS_BUCKET, Key=s3_key)
            logging.info(f"[cleanup] Deleted S3 object s3://{settings.S3_DOWNLOADS_BUCKET}/{s3_key}")
    except Exception as e:
        logging.warning(f"[cleanup] Could not delete S3 object for {project_id}: {e}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_api_throttling(request):
    """
    Disable /api/v1/ rate limiting for every test, and clear its counters.

    Tests fire dozens of requests from the same 127.0.0.1 in a few seconds,
    which would trip the production limits (api_batch is 10/min) and make
    unrelated tests fail intermittently with 429s.

    tests/test_api_throttling.py re-enables throttling explicitly via
    override_settings; it is marked with `throttled` so this fixture stands
    aside for it.  Counters are still cleared either way, so no test inherits
    a partially-consumed window from the one before it.

    Note: do not reach into rest_framework.settings.api_settings.__dict__ to
    drop cached values here.  APISettings.reload() iterates its own
    _cached_attrs and calls delattr, so clearing entries behind its back makes
    the next override_settings raise AttributeError for every test in the
    suite.  override_settings already fires the signal that reloads it.
    """
    from django.core.cache import caches
    from django.test import override_settings

    def _clear():
        caches['throttle'].clear()

    _clear()
    if request.node.get_closest_marker('throttled'):
        yield
        _clear()
        return

    with override_settings(REST_FRAMEWORK={'NUM_PROXIES': 1,
                                           'DEFAULT_THROTTLE_CLASSES': [],
                                           'DEFAULT_THROTTLE_RATES': {}}):
        _clear()
        yield
    _clear()


@pytest.fixture(scope='session')
def test_user():
    """
    A lightweight mock user for all tests.

    The tests only call view functions directly and never go through
    authentication middleware, so a simple object with the right attributes
    is sufficient.  Using a real Django User would trigger the ORM, which
    requires Django 4.x-compatible settings (e.g. DEFAULT_FILE_STORAGE)
    that are absent in the Django 5.x installed in this environment.
    """
    class _MockUser:
        username = 'pytest_test_user'
        email    = 'pytest_test_user@example.com'
        is_staff = True
        is_active = True
        is_authenticated = True

        def __str__(self):
            return self.username

    return _MockUser()


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

        def __str__(self):
            return self.username

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

        def __str__(self):
            return self.username

    return _NonMember()


@pytest.fixture
def request_factory():
    """A Django RequestFactory instance."""
    from django.test import RequestFactory
    return RequestFactory()


@pytest.fixture
def mongo_collection():
    """Direct handle to the MongoDB projects collection."""
    from caper.views import collection_handle
    return collection_handle


@pytest.fixture
def tar_file():
    """Absolute path to the small (1-sample, hg19) tar.gz test file."""
    assert os.path.exists(DATASET_SMALL_TAR), f"Test data not found: {DATASET_SMALL_TAR}"
    return DATASET_SMALL_TAR


@pytest.fixture
def xlsx_file():
    """Absolute path to the small dataset xlsx metadata test file."""
    assert os.path.exists(DATASET_SMALL_XLSX), f"Test data not found: {DATASET_SMALL_XLSX}"
    return DATASET_SMALL_XLSX


@pytest.fixture(scope='session')
def loaded_datasets(test_user):
    """
    Creates two projects from real test data, waits for aggregation to finish,
    yields project IDs and known metadata values, then cleans up both projects.

    project_small:  one_amprepo_sample.tar.gz   (1 sample, hg19, has xlsx metadata)
    project_medium: Contino_unagg_040423.tar.gz  (9 samples, hg38, no metadata)

    Session-scoped: set up once per test session, shared across all functional tests.
    Instantiates RequestFactory and collection_handle directly to avoid requesting
    function-scoped fixtures from a session-scoped fixture.

    Override gene/tissue env vars if your datasets differ from the defaults:
        DATASET_SMALL_GENE, DATASET_SMALL_TISSUE,
        DATASET_MEDIUM_GENE, DATASET_MEDIUM_TISSUE
    """
    assert os.path.exists(DATASET_SMALL_TAR),  f"Missing test dataset: {DATASET_SMALL_TAR}"
    assert os.path.exists(DATASET_MEDIUM_TAR), f"Missing test dataset: {DATASET_MEDIUM_TAR}"

    from django.test import RequestFactory
    from caper.views import collection_handle, create_project

    rf         = RequestFactory()
    collection = collection_handle
    created_ids = []

    req_a, handles_a = _build_create_request(
        rf, test_user, 'FuncTest_Small',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
    try:
        resp_a = create_project(req_a)
    finally:
        for h in handles_a:
            h.close()
    id_a = _project_id_from_redirect(resp_a)
    assert id_a, "Could not parse project_id from FuncTest_Small redirect"
    created_ids.append(id_a)

    req_b, handles_b = _build_create_request(
        rf, test_user, 'FuncTest_Medium',
        tar_path=DATASET_MEDIUM_TAR)
    try:
        resp_b = create_project(req_b)
    finally:
        for h in handles_b:
            h.close()
    id_b = _project_id_from_redirect(resp_b)
    assert id_b, "Could not parse project_id from FuncTest_Medium redirect"
    created_ids.append(id_b)

    doc_a = _poll_until_finished(collection, id_a)
    doc_b = _poll_until_finished(collection, id_b)
    assert doc_a and not doc_a.get('aggregation_failed'), \
        f"Small dataset aggregation failed: {doc_a.get('error_message') if doc_a else 'timeout'}"
    assert doc_b and not doc_b.get('aggregation_failed'), \
        f"Medium dataset aggregation failed: {doc_b.get('error_message') if doc_b else 'timeout'}"

    yield {
        'project_small':    id_a,
        'project_medium':   id_b,
        'gene_in_small':    os.environ.get('DATASET_SMALL_GENE',    'MYC'),
        'tissue_in_small':  os.environ.get('DATASET_SMALL_TISSUE',  'GBM'),
        'gene_in_medium':   os.environ.get('DATASET_MEDIUM_GENE',   'EGFR'),
        'tissue_in_medium': os.environ.get('DATASET_MEDIUM_TISSUE', 'Lung'),
    }

    for pid in created_ids:
        _cleanup_project(collection, pid)


# ---------------------------------------------------------------------------
# Source-scanning guards: what counts as "the codebase"
# ---------------------------------------------------------------------------

def tracked_python_files(repo_root):
    """Every ``.py`` file git knows about, relative to *repo_root*.

    Several guards read the source tree looking for patterns that must not
    appear -- raw reads of the visibility field, unlisted status literals, a
    retired OAuth scope. Walking the directory finds whatever happens to be
    sitting there, and what is sitting there differs per machine: on the dev
    server on 2026-08-31 those guards were failing on `holding/settings.py` and
    `caper/caper/view_old.py`, neither of which has ever been committed, plus a
    measurement script somebody had copied in that morning. Three real guards
    reporting three false faults, on files that are not part of the codebase.

    Tracked files are the codebase. An untracked file is somebody's scratch
    copy and is nobody's business but theirs -- and if it ever does become part
    of the codebase, committing it is exactly the moment these guards should
    start reading it.

    ``-c safe.directory=*`` is needed and is safe here. The container runs as
    root while the checkout is owned by uid 1000, so git refuses with "detected
    dubious ownership" -- that protection exists to stop git executing config
    and hooks out of somebody else's repository, and ``ls-files`` does neither.
    Setting it inline keeps it to this one read-only command; the alternative,
    which ``views_admin`` uses, is to mutate global git config at runtime, and
    that lands in the container's writable layer where the next image rebuild
    silently removes it. That is exactly how this surfaced: the guards passed
    for weeks on a container where an admin page had once set it, and failed
    the moment the image was rebuilt on 2026-08-31.

    Falls back to walking the tree **only when there is no repository at all**,
    which is the source-tarball case -- no git means no untracked files to be
    confused by either. When a repository is present and git cannot read it,
    this skips rather than walks. The first version fell back to walking
    unconditionally and that was a bad failure mode: it fails open into exactly
    the behaviour the helper exists to prevent, so a broken git turns three
    guards into false accusations instead of an honest error.
    """
    import subprocess

    repo_root = str(repo_root)
    if os.path.isdir(os.path.join(repo_root, '.git')):
        try:
            listed = subprocess.run(
                ['git', '-c', 'safe.directory=*', '-C', repo_root,
                 'ls-files', '-z', '*.py'],
                capture_output=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, 'stderr', b'') or b''
            pytest.skip('cannot enumerate tracked files, so this guard would '
                        'report on whatever is lying in the checkout: '
                        f'{detail.decode(errors="replace").strip()[:200] or exc}')
        return sorted(name for name in listed.decode().split('\0') if name)

    walked = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames
                       if d not in {'__pycache__', 'node_modules'}]
        for name in filenames:
            if name.endswith('.py'):
                walked.append(os.path.relpath(
                    os.path.join(dirpath, name), repo_root))
    return sorted(walked)


# ---------------------------------------------------------------------------
# Browser tests: skip before anything tries to launch a browser
# ---------------------------------------------------------------------------

def _playwright_browser_available():
    """Whether a chromium build is actually on disk.

    Deliberately cheap and fail-open: it looks for the download rather than
    starting playwright's driver to ask. If the layout is ever unfamiliar this
    returns True and the test fails honestly, which is the right way round --
    silently skipping a test that could have run is the worse error.
    """
    root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.join(
        os.path.expanduser('~'), '.cache', 'ms-playwright')
    if not os.path.isdir(root):
        return False
    return any(name.startswith('chromium') for name in os.listdir(root))


def pytest_collection_modifyitems(config, items):
    """Skip ``browser``-marked tests when this machine cannot run them.

    ``test_browser.py`` already had a module-scoped guard that skips when
    ``--base-url`` is absent, but pytest-playwright's ``page`` fixture launches
    chromium during setup, and fixture setup can run before that guard does. On
    a machine with the browsers installed the launch succeeds and the guard then
    skips, so nothing shows; on the dev server, where they are not installed,
    eleven tests came back as ERROR rather than SKIPPED.

    That distinction matters more than it looks. A suite reporting errors reads
    as broken and gets ignored; a suite reporting skips reads as a suite that
    knows what it is not doing. Deciding at collection time means no fixture
    runs at all, so the answer no longer depends on fixture ordering.

Chromium is installed by the Dockerfile as of 2026-08-31, in the production
    image as well as dev, so the browsers should normally be there and this
    hook's second branch should normally not fire. It stays because the first
    branch always applies -- an ordinary run passes no ``--base-url`` and these
    tests need a server -- and because a container predating that image change,
    or a developer running outside one, is still a case the suite has to
    describe rather than fall over on. Verified on dev the same day: all eleven
    pass against the container's own server on 127.0.0.1:8000.
    """
    try:
        base_url = config.getoption('--base-url')
    except (ValueError, AttributeError):
        base_url = None

    if not base_url:
        reason = ('browser tests need a running server: '
                  'pytest -m browser --base-url http://localhost:8000')
    elif not _playwright_browser_available():
        reason = ('playwright browsers are not installed here: '
                  'python -m playwright install --with-deps chromium '
                  '(the browser alone is not enough; it needs system libs too)')
    else:
        return

    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if 'browser' in item.keywords:
            item.add_marker(skip)
