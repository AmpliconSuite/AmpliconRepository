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
      1. MongoDB document
      2. tmp/{project_id}/ directory on disk
      3. S3 object (when USE_S3_DOWNLOADS is True)
    Errors in any step are logged but do not raise so all steps always run.
    """
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
