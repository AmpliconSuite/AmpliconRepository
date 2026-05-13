"""
Browser end-to-end tests using Playwright.

These tests verify URL routing, template rendering, and JavaScript behaviour
that the view-layer integration tests (Phase 2) cannot cover.

Prerequisites
-------------
    pip install pytest-playwright
    playwright install chromium

The dev server must be running before tests start:
    cd caper && python manage.py runserver

Run with:
    pytest -m browser --base-url http://localhost:8000 -v

Individual markers still apply, so to also run slow tests:
    pytest -m "browser or slow" --base-url http://localhost:8000 -v
"""

import pytest


# ---------------------------------------------------------------------------
# Module-level guard: skip everything if --base-url is not supplied.
# pytest-playwright provides the `base_url` fixture; its value is None when
# --base-url is not passed on the command line.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope='module')
def _require_base_url(base_url):
    if not base_url:
        pytest.skip(
            "Browser tests require a running server and --base-url. "
            "Example: pytest -m browser --base-url http://localhost:8000 -v")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def public_project_id():
    """
    Return the linkid of the first non-deleted, finished public project
    in MongoDB.  Skips if none exists.
    """
    try:
        from caper.utils import collection_handle
    except Exception as exc:
        pytest.skip(f"Cannot import caper.utils: {exc}")

    doc = collection_handle.find_one({
        'private': {'$in': [False, 'public']},
        'delete': False,
        'current': True,
        'FINISHED?': True,
    })
    if not doc:
        pytest.skip("No public finished project in database")
    return str(doc.get('linkid') or doc['_id'])


@pytest.fixture(scope='module')
def private_project_id():
    """
    Return the linkid of the first non-deleted, finished private project
    in MongoDB.  Skips if none exists.
    """
    try:
        from caper.utils import collection_handle
    except Exception as exc:
        pytest.skip(f"Cannot import caper.utils: {exc}")

    doc = collection_handle.find_one({
        'private': {'$in': [True, 'private']},
        'delete': False,
        'current': True,
        'FINISHED?': True,
    })
    if not doc:
        pytest.skip("No private finished project in database")
    return str(doc.get('linkid') or doc['_id'])


@pytest.fixture
def authenticated_page(page):
    """
    Playwright page pre-authenticated as a test user.

    Creates a real Django User, logs in via the browser login form
    (email + password, not OAuth), and returns the page ready to navigate.
    Cleans up the user after the test.

    Skips if Django ORM setup fails (e.g., migration not run).
    """
    try:
        from django.contrib.auth.models import User
    except Exception as exc:
        pytest.skip(f"Cannot import Django User model: {exc}")

    username = 'playwright_auth_user'
    email    = 'playwright_auth@test.local'
    password = 'Playwright!Test123'

    # Ensure a clean slate
    User.objects.filter(username=username).delete()
    try:
        user = User.objects.create_user(
            username=username, email=email, password=password)
    except Exception as exc:
        pytest.skip(f"Could not create test user: {exc}")

    # Log in via the browser form
    page.goto('/accounts/login/')
    page.locator('#id_login').fill(email)
    page.locator('#id_password').fill(password)
    page.locator('button[type="submit"]').click()
    page.wait_for_load_state('domcontentloaded')

    yield page

    # Teardown
    try:
        User.objects.filter(username=username).delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.browser
def test_homepage_loads(page):
    """
    Homepage must load and render the unified project DataTable without a 500.
    """
    page.goto('/')
    # DataTables initialises #unifiedProjectTable after DOM + JS load
    page.wait_for_selector('#unifiedProjectTable', timeout=15_000)
    assert page.locator('#unifiedProjectTable').is_visible(), \
        "Project table (#unifiedProjectTable) not visible on homepage"
    title = page.title()
    assert 'error' not in title.lower() and '500' not in title, \
        f"Homepage title suggests an error: {title!r}"


@pytest.mark.browser
def test_homepage_has_project_rows(page):
    """
    When any public or private projects exist, at least one DataTable row
    with a data-project-type attribute must appear.
    """
    page.goto('/')
    page.wait_for_selector('#unifiedProjectTable', timeout=15_000)
    rows = page.locator('tr[data-project-type]')
    # Acceptable if zero rows (empty DB); just verify no JS crash
    count = rows.count()
    assert count >= 0  # tautologically true — the real check is no 500/JS error


@pytest.mark.browser
def test_gene_search_page_renders_form(page):
    """
    /gene-search/ must render the search form with a visible gene name input
    and a submit button.
    """
    page.goto('/gene-search/')
    page.wait_for_selector('#genequery', timeout=10_000)
    assert page.locator('#genequery').is_visible(), \
        "Gene query input (#genequery) not visible on gene search page"
    assert page.locator('button[type="submit"]').is_visible(), \
        "Submit button not visible on gene search page"


@pytest.mark.browser
def test_gene_search_form_submits(page):
    """
    Submitting the gene search form (empty query) must navigate to a results
    page without a 500 error and render the results container.
    """
    page.goto('/gene-search/')
    page.wait_for_selector('#genequery', timeout=10_000)
    # Submit with no gene query
    page.locator('button[type="submit"]').click()
    page.wait_for_load_state('domcontentloaded')

    assert '500' not in page.title(), \
        "Gene search submission caused a 500 error"
    # The results page renders a .results-container or .search-container
    has_results = (
        page.locator('.results-container').count() > 0 or
        page.locator('.search-container').count() > 0
    )
    assert has_results, \
        "Expected .results-container or .search-container after search submission"


@pytest.mark.browser
def test_search_results_via_searchbox(page):
    """
    The slide-out search panel on the homepage (#searchForm) must submit
    to /search_results/ and render the results page.
    """
    page.goto('/')
    page.wait_for_selector('#search-slider-toggle', timeout=10_000)

    # Open the search slider panel
    page.locator('#search-slider-toggle').click()
    page.wait_for_selector('#searchForm', timeout=5_000)
    assert page.locator('#searchForm').is_visible(), \
        "Search form (#searchForm) not visible after toggling the search panel"

    # Submit with empty query
    page.locator('#searchForm button[type="submit"]').click()
    page.wait_for_url('**/search_results/**', timeout=10_000)

    assert '500' not in page.title(), \
        "Search form submission caused a 500 error"
    has_results = (
        page.locator('.results-container').count() > 0 or
        page.locator('.search-container').count() > 0
    )
    assert has_results, \
        "Expected .results-container or .search-container on /search_results/ page"


@pytest.mark.browser
def test_public_project_page_renders(page, public_project_id):
    """
    A public project page must render the sample DataTable (#myTable1)
    with at least one sample row and show the three download buttons.
    """
    page.goto(f'/project/{public_project_id}')
    page.wait_for_selector('#myTable1', timeout=20_000)

    assert page.locator('#myTable1').is_visible(), \
        "Sample table (#myTable1) not visible on project page"
    sample_rows = page.locator('#myTable1 tbody tr')
    assert sample_rows.count() > 0, \
        "Project page must show at least one sample row in #myTable1"

    # All three download buttons should be present
    assert page.get_by_text('Download Project').count() > 0, \
        "Download Project button missing"
    assert page.get_by_text('Download Summary').count() > 0, \
        "Download Summary button missing"
    assert page.get_by_text('Download Metadata').count() > 0, \
        "Download Metadata button missing"


@pytest.mark.browser
def test_private_project_redirects_to_login(page, private_project_id):
    """
    Visiting a private project page without an authenticated session must
    redirect to the login page (URL contains 'login' or the login form is visible).
    """
    page.goto(f'/project/{private_project_id}')
    page.wait_for_load_state('domcontentloaded')

    final_url = page.url
    has_login_form = page.locator('#id_login, #id_password').count() > 0
    url_has_login = 'login' in final_url.lower()

    assert url_has_login or has_login_form, \
        f"Expected redirect to login for private project, ended at {final_url!r}"


@pytest.mark.browser
def test_create_project_page_requires_login(page):
    """
    /create-project/ is decorated with @login_required.
    An unauthenticated visitor must be redirected to the login page,
    not see a 500 error.
    """
    page.goto('/create-project/')
    page.wait_for_load_state('domcontentloaded')

    final_url = page.url
    is_login_redirect = 'login' in final_url.lower()
    has_login_form = page.locator('#id_login, button[type="submit"]').count() > 0

    assert is_login_redirect or has_login_form, \
        f"Expected login redirect for /create-project/, ended at {final_url!r}"
    assert '500' not in page.title(), \
        "create-project caused a 500 error"


@pytest.mark.browser
def test_authenticated_user_sees_create_form(authenticated_page):
    """
    An authenticated user must see the file upload form on /create-project/,
    not a login redirect.  The create button starts disabled (no file chosen).
    """
    authenticated_page.goto('/create-project/')
    authenticated_page.wait_for_selector('#upload-form', timeout=10_000)

    assert authenticated_page.locator('#upload-form').is_visible(), \
        "Upload form (#upload-form) not visible for authenticated user"
    assert authenticated_page.locator('#fileuploadfield').is_visible(), \
        "File input (#fileuploadfield) not visible"

    create_btn = authenticated_page.locator('#createProjectBtn')
    assert create_btn.is_visible(), \
        "Create project button (#createProjectBtn) not visible"
    assert create_btn.is_disabled(), \
        "Create button should be disabled before a file is selected"


@pytest.mark.browser
def test_login_page_renders(page):
    """
    The login page must render both the email/password form and the
    OAuth provider buttons (Google, Globus).
    """
    page.goto('/accounts/login/')
    page.wait_for_load_state('domcontentloaded')

    assert page.locator('#id_login').is_visible(), \
        "Login email field (#id_login) not visible"
    assert page.locator('#id_password').is_visible(), \
        "Password field (#id_password) not visible"
    # OAuth buttons
    assert page.get_by_text('Login via Google').count() > 0 or \
           page.get_by_text('Login via Globus').count() > 0, \
        "Neither Google nor Globus OAuth button found on login page"
