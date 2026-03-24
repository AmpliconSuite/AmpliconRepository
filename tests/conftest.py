"""
Shared pytest fixtures for the AmpliconRepository test suite.

Django is initialised by the root conftest.py's pytest_configure hook before
any fixtures run.  Django imports are kept inside fixture functions (lazy) so
they don't execute at module import time, which would precede Django setup.
"""

import os
import pytest

# ---------------------------------------------------------------------------
# Paths used across multiple test modules
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(__file__))   # …/caper/
TEST_DATA_DIR = os.path.join(REPO_ROOT, 'test_data')
TAR_FILE = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.tar.gz')
XLSX_FILE = os.path.join(TEST_DATA_DIR, 'one_amprepo_sample.xlsx')


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
        email = 'pytest_test_user@example.com'
        is_staff = True
        is_active = True
        is_authenticated = True

        def __str__(self):
            return self.username

    return _MockUser()


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
    """Absolute path to the sample tar.gz test file."""
    assert os.path.exists(TAR_FILE), f"Test data not found: {TAR_FILE}"
    return TAR_FILE


@pytest.fixture
def xlsx_file():
    """Absolute path to the sample xlsx metadata test file."""
    assert os.path.exists(XLSX_FILE), f"Test data not found: {XLSX_FILE}"
    return XLSX_FILE
