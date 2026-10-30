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

import os
from urllib.parse import urlparse

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


@pytest.fixture(autouse=True)
def _through_the_dev_gate(page, base_url):
    """Carry a landing-gate pass when the server under test has the gate on.

    Read from this process's settings, which is the same config.sh the server
    loaded when both run in the container.  Without it every test that starts
    at `/` sees the landing page instead of the site: three did, on dev,
    2026-09-16.  A laptop or production has the gate off and this is a no-op.
    """
    from caper.dev_gate import COOKIE_NAME, gate_enabled, issue_pass
    if gate_enabled():
        page.context.add_cookies([{
            'name': COOKIE_NAME, 'value': issue_pass(), 'url': base_url,
        }])


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def public_project_id():
    """
    Return the linkid of the first non-deleted, finished public project
    in MongoDB.  Skips if none exists.
    """
    from caper.utils import collection_handle

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
    from caper.utils import collection_handle

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
def authenticated_page(page, base_url):
    """
    Playwright page pre-authenticated as a test user via Django session injection.

    Creates a real Django User, writes a server-side session directly, and
    injects the session cookie into the browser context.  This bypasses the
    login form entirely, so the fixture is immune to allauth's
    ACCOUNT_AUTHENTICATION_METHOD setting (email vs username) and avoids
    timing races where wait_for_load_state resolves on an intermediate redirect
    before the session cookie is committed.

    Cleans up the user and session after the test.
    """
    from django.conf import settings as dj_settings
    from django.contrib.auth.models import User
    from django.contrib.sessions.backends.db import SessionStore

    username = 'playwright_auth_user'
    email    = 'playwright_auth@test.local'
    password = 'Playwright!Test123'

    os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

    User.objects.filter(username=username).delete()
    user = User.objects.create_user(
        username=username, email=email, password=password)

    # Write a real Django session and inject it as a browser cookie.
    # Using ModelBackend explicitly so the session hash is valid regardless of
    # which allauth backend is configured as the primary.
    session = SessionStore()
    session['_auth_user_id']      = str(user.pk)
    session['_auth_user_backend'] = 'allauth.account.auth_backends.AuthenticationBackend'
    session['_auth_user_hash']    = user.get_session_auth_hash()
    session.set_expiry(3600)
    session.save()

    server_url  = base_url or 'http://127.0.0.1:8000'
    cookie_name = getattr(dj_settings, 'SESSION_COOKIE_NAME', 'sessionid')

    # Navigate to the homepage first so the browser has a first-party context
    # for 127.0.0.1 before the session cookie is injected.  Playwright requires
    # the origin to be established before cookies can be reliably set for IP
    # addresses; using `url` (rather than `domain`+`path`) is also more
    # reliable for numeric IPs across Chromium versions.
    page.goto(server_url)
    page.wait_for_load_state('domcontentloaded')

    page.context.add_cookies([{
        'name':  cookie_name,
        'value': session.session_key,
        'url':   server_url,
    }])

    try:
        # Validate that the dev server recognised our injected session. If the
        # server uses a different SQLite database, this requested browser run
        # is misconfigured and must fail rather than report a skip.
        page.goto(f'{server_url}/create-project/')
        page.wait_for_load_state('domcontentloaded')
        caper_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'caper')
        assert page.locator('#upload-form').count() > 0, (
            "Session injection failed — the dev server appears to be using a "
            "different database from the pytest process (possibly a different "
            "repository clone). "
            f"Start the server from: cd {caper_root} && python manage.py runserver"
        )

        yield page
    finally:
        try:
            session.delete()
        except Exception:
            pass
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
def test_homepage_projects_are_ordered_newest_first(page):
    """
    The Updated column must actually sort, within a visibility group.

    This is worth a browser test because it cannot fail server-side: the view
    sends rows in Mongo's natural order and the ordering is entirely the
    client's doing.  It regressed once already, when the date cell went from
    plain text to a span holding a compact date -- DataTables reads a DOM
    cell's innerHTML as its sort data, so the date parser scored every row 0,
    every row tied, and the table silently fell back to the order it was
    served in.  The dates on screen looked fine throughout.
    """
    page.goto('/')
    page.wait_for_selector('#unifiedProjectTable tbody tr', timeout=15_000)

    # Read through the DataTables API rather than the DOM: the table paginates,
    # and the rank column is hidden, so neither is present in tbody.  data-order
    # is the sort key the page sets on each date cell, and reading it rather
    # than the visible text is what pins the defect -- the text was never wrong.
    rows = page.evaluate(
        """() => {
            const dt = jQuery('#unifiedProjectTable').DataTable();
            const ranks = dt.column(0, {order: 'applied'}).data().toArray();
            const cells = dt.column(3, {order: 'applied'}).nodes().toArray();
            return cells.map((cell, i) => ({
                rank: String(ranks[i]).trim(),
                order: cell.getAttribute('data-order'),
            }));
        }""")

    # Skip on too little data, never on a missing sort key -- a key that is not
    # there is the regression, so it has to fail rather than quietly skip.
    if len(rows) < 2:
        pytest.skip('Need at least two projects on the home page')

    missing = [row for row in rows if row['order'] in (None, '')]
    assert not missing, \
        (f'{len(missing)} of {len(rows)} date cells carry no data-order, '
         f'so the column has no sort key to order on')

    dated = rows
    keys = [int(r['order']) for r in dated]
    assert len(set(keys)) > 1, \
        'Every row has the same sort key -- the date column is not sorting'

    for previous, current in zip(dated, dated[1:]):
        if previous['rank'] != current['rank']:
            continue        # a visibility-group boundary restarts the dates
        assert int(previous['order']) >= int(current['order']), \
            (f"Rows out of order within rank {current['rank']}: "
             f"{previous['order']} came before {current['order']}")


@pytest.mark.browser
def test_gene_search_redirects_to_the_home_page(page):
    """
    /gene-search/ was retired; the URL must 301 rather than 404.

    It was the pre-2025 search, superseded by /search_results/ and the home page
    box that posts to it, and it served 34 requests in three days of production
    logs -- almost all crawler and measurement traffic. bingbot has the URL
    indexed and ten internal redirects still name the route, so it stays as a
    permanent redirect rather than being deleted outright.
    """
    response = page.goto('/gene-search/')
    assert response is not None, 'no response for /gene-search/'
    assert response.status == 200, (
        f'expected the redirect to land on a 200, got {response.status}')
    from urllib.parse import urlparse
    assert urlparse(page.url).path == '/', \
        f'expected to land on the home page, got {page.url}'
    assert page.locator('#home-query').count() == 1, \
        'the home page it redirects to should carry the search box'


@pytest.mark.browser
def test_choosing_a_gene_suggestion_searches_for_it(page):
    """
    The home box suggests genes by every name AmpliconSuite used; choosing one
    writes the NCBI symbol and submits. C17orf37 is MIEN1's name in hg19 results
    only, and the entry says so.
    """
    page.goto('/')
    page.locator('#home-query').type('C17orf3', delay=20)
    first = page.locator('.gene-ac-list:not([hidden]) .gene-ac-item').first
    first.wait_for(timeout=10_000)
    assert 'MIEN1' in first.inner_text() and 'C17orf37 (hg19)' in first.inner_text()
    with page.expect_navigation(timeout=120_000):
        first.dispatch_event('mousedown')
    assert '/search_results/' in page.url
    assert page.locator('.search-box input[name="gene"]').input_value() == 'MIEN1'


@pytest.mark.browser
def test_the_advanced_search_modal_suggests_genes(page):
    """The header's search modal carries the same autocomplete, drawn above it."""
    page.goto('/')
    page.click('#home-advanced')
    page.locator('#searchModal input[name="genequery"]').type('PDK', delay=20)
    first = page.locator('.gene-ac-list:not([hidden]) .gene-ac-item').first
    first.wait_for(timeout=10_000)
    assert first.inner_text().startswith('PDK1')
    on_top = page.evaluate("""() => {
        const r = document.querySelector('.gene-ac-list:not([hidden]) .gene-ac-item').getBoundingClientRect();
        return Boolean(document.elementFromPoint(r.left + 5, r.top + 5).closest('.gene-ac-item'));
    }""")
    assert on_top, 'the suggestion list is hidden behind the modal'


@pytest.mark.browser
def test_the_advanced_search_takes_genes_as_chips(page):
    """
    Each suggestion picked in the Advanced search becomes a chip; with two, the
    any/all controls appear, and "all" offers same amplicon or same sample.
    The results page carries the same choices back.
    """
    page.goto('/')
    page.click('#home-advanced')
    modal = page.locator('#searchModal')
    box = modal.locator('.gene-chip-input')
    for name in ('MYC', 'CDK4'):
        box.type(name, delay=20)
        entry = page.locator('.gene-ac-list:not([hidden]) .gene-ac-item').first
        entry.wait_for(timeout=10_000)
        entry.dispatch_event('mousedown')
    assert [c.get_attribute('data-term') for c in modal.locator('.gene-chip').all()] == ['MYC', 'CDK4']
    assert modal.locator('.gene-match').is_visible()
    assert not modal.locator('.gene-scope').is_visible()
    modal.locator('input[name="gene_match"][value="all"]').check()
    modal.locator('input[name="gene_scope"][value="sample"]').check()
    # Text left in the box joins the chips on submit.
    box.type('mdm2')
    with page.expect_navigation(timeout=120_000):
        modal.locator('button[type="submit"]').click()
    panel = page.locator('.search-box')
    assert [i.input_value() for i in panel.locator('input[name="gene"]').all()] == ['MYC', 'CDK4', 'mdm2']
    assert panel.locator('input[name="gene_match"][value="all"]').is_checked()
    assert panel.locator('input[name="gene_scope"][value="sample"]').is_checked()
    # Removing a chip down to one hides the controls again.
    panel.locator('.gene-chip-remove').first.click()
    panel.locator('.gene-chip-remove').first.click()
    assert not panel.locator('.gene-match').is_visible()


@pytest.mark.browser
def test_the_project_filter_finds_a_gene_under_another_name(page):
    """
    A name the project's rows don't hold is resolved: the hg19 rows say
    C17orf37, and a filter for MIEN1 (its hg38 name) finds the same samples.
    """
    from caper.utils import collection_handle
    doc = collection_handle.find_one({
        'private': {'$in': [False, 'public']}, 'delete': False, 'current': True,
        'reference_genome': {'$in': ['hg19', 'GRCh37']}, 'FINISHED?': True,
    })
    if not doc:
        pytest.skip('No public hg19 project in the database')
    linkid = str(doc.get('linkid') or doc['_id'])
    page.goto(f'/project/{linkid}')
    page.wait_for_selector('#samples-table-wrap.is-ready', timeout=60_000)
    box = page.locator('#sample-filter-input')
    box.fill('C17orf37')
    page.wait_for_timeout(500)
    by_own_name = page.locator('#myTable1_info').inner_text()
    if 'No samples' in by_own_name:
        pytest.skip('This project has no C17orf37 sample')
    box.fill('MIEN1')
    note = page.locator('#sample-gene-note')
    note.wait_for(state='visible', timeout=10_000)
    assert 'called C17orf37 here' in note.inner_text()
    assert page.locator('#myTable1_info').inner_text() == by_own_name


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
    # OAuth provider buttons — template renders provider.name ("Google", "Globus"),
    # not the full "Login via Google" phrase.  Providers require database SocialApp
    # entries so we do a soft check: skip (not fail) if none are configured.
    has_oauth = (
        page.get_by_text('Google').count() > 0 or
        page.get_by_text('Globus').count() > 0
    )
    if not has_oauth:
        pytest.skip(
            "No OAuth providers (Google/Globus) found on login page — "
            "SocialApp entries may not be configured in this environment"
        )


@pytest.mark.browser
def test_email_password_login_form_works(page, base_url):
    """
    Submitting the login form with valid credentials must redirect away from
    the login page, confirming the form actually authenticates the user.

    allauth 0.51 defaults ACCOUNT_AUTHENTICATION_METHOD to 'username', so this
    test fills #id_login with the username (not the email).  If the project ever
    switches to email-only auth, change the fill value to ``email``.
    """
    from django.contrib.auth.models import User

    os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'

    username = 'playwright_login_form_user'
    email    = 'playwright_login_form@test.local'
    password = 'LoginForm!789'

    User.objects.filter(username=username).delete()
    User.objects.create_user(username=username, email=email, password=password)

    try:
        server_url = base_url.rstrip('/')

        # ?next=/ makes allauth redirect to the homepage on success instead of
        # /accounts/profile/, which in turn redirects anonymous users back to
        # the login page (the profile view guards against unauthenticated access).
        page.goto(f'{server_url}/accounts/login/?next=/')
        page.wait_for_load_state('domcontentloaded')

        # allauth 0.51 default: username-based login
        page.locator('#id_login').fill(username)
        page.locator('#id_password').fill(password)
        page.get_by_role('button', name='Sign In').click()

        # Successful login redirects to /  (via the ?next=/ parameter).
        # Use a predicate so we wait for the exact homepage URL, not the login
        # page itself (which also ends with '/' and would fool '**/' immediately).
        homepage = server_url.rstrip('/')
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
        except ImportError:
            PlaywrightTimeout = Exception  # fallback — will still catch correctly
        try:
            page.wait_for_url(
                lambda url: url.rstrip('/') == homepage,
                timeout=10_000,
            )
        except PlaywrightTimeout:
            pytest.fail(
                "Login form did not redirect to homepage within 10 s — the dev "
                "server may be using a different database from the pytest process "
                "(user created by pytest not found on the server).  "
                "Ensure --base-url points to the server started from this repo."
            )

        assert '500' not in page.title(), "Login caused a 500 error"
        assert 'login' not in page.url.lower(), \
            f"Login form redirected back to login — still at {page.url!r}. " \
            "Check ACCOUNT_AUTHENTICATION_METHOD in settings.py."
    finally:
        try:
            User.objects.filter(username=username).delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Download buttons: click each one as a user would, check what arrives, and
# time it.  Asked for after a sample zip froze Chrome on prod 2026-10-16 while
# the server had sent all 33 MB of it -- a server-side test could not have
# told those apart, a browser can.
#
# Writes download counters on the server under test, so point it at a laptop
# or dev, not production.  Target a particular project with
# BROWSER_DOWNLOAD_PROJECT=<id>; the per-download budget is
# BROWSER_DOWNLOAD_BUDGET_S (default 120).  Run with -s to see the timings.
# ---------------------------------------------------------------------------

def _download_target(public_project_id):
    return os.environ.get('BROWSER_DOWNLOAD_PROJECT') or public_project_id


def _check_payload(name, data):
    """What arrived must be the file, not an error or a challenge page."""
    import io
    import json
    import zipfile
    assert data, f'{name}: empty download'
    head = data[:200].lstrip().lower()
    assert not head.startswith((b'<!doctype', b'<html')), \
        f'{name}: an HTML page arrived instead of the file'
    if name.endswith('.zip') or data[:2] == b'PK':
        bad = zipfile.ZipFile(io.BytesIO(data)).testzip()
        assert bad is None, f'{name}: corrupt member {bad}'
    elif name.endswith('.tar.gz') or data[:2] == b'\x1f\x8b':
        import gzip
        gzip.decompress(data)
    elif name.endswith('.json'):
        json.loads(data)


def _timed_download(page, click, label, timings, budget):
    import time
    seen = []
    record = lambda r: seen.append(f'{r.status} {r.url}')
    page.on('response', record)
    t0 = time.perf_counter()
    try:
        with page.expect_download(timeout=budget * 1000) as info:
            click()
        download = info.value
    except Exception as exc:
        pytest.fail(f'{label}: no download within {budget}s ({type(exc).__name__}); '
                    f'page is at {page.url}; last responses: {seen[-5:]}')
    finally:
        page.remove_listener('response', record)
    path = download.path()            # blocks until the file is complete
    elapsed = time.perf_counter() - t0
    assert download.failure() is None, f'{label}: {download.failure()}'
    with open(path, 'rb') as handle:
        data = handle.read()
    _check_payload(download.suggested_filename, data)
    timings.append((label, download.suggested_filename, len(data), elapsed))
    assert elapsed < budget, f'{label}: {elapsed:.1f}s is over the {budget}s budget'


def _timed_inline(page, base_url, link, label, timings, budget):
    """PNG and PDF links are served inline, not as attachments.

    Fetched through the page's own session instead of clicked: headless
    Chromium has no PDF viewer, so a clicked PDF neither stays in the tab nor
    arrives as a download, and its body cannot be read afterwards.  The URL is
    still the one the page links to.
    """
    import time
    href = link.get_attribute('href')
    t0 = time.perf_counter()
    response = page.request.get(base_url.rstrip('/') + href, timeout=budget * 1000)
    body = response.body()
    elapsed = time.perf_counter() - t0
    assert response.status == 200, f'{label}: HTTP {response.status} for {href}'
    ctype = response.headers.get('content-type', '')
    assert ctype.startswith(('image/', 'application/pdf')), f'{label}: {ctype}'
    assert body, f'{label}: empty body'
    timings.append((label, href.rsplit('/', 4)[-4], len(body), elapsed))


def _report(title, timings):
    print(f'\n{title}')
    for label, name, size, elapsed in timings:
        print(f'  {label:<22} {elapsed:7.2f}s {size / 2**20:9.2f} MiB  {name}')


@pytest.mark.browser
@pytest.mark.slow
def test_sample_page_download_buttons(authenticated_page, base_url, public_project_id):
    page = authenticated_page
    budget = float(os.environ.get('BROWSER_DOWNLOAD_BUDGET_S', '120'))
    project = _download_target(public_project_id)
    sample = os.environ.get('BROWSER_DOWNLOAD_SAMPLE')
    if not sample:
        # The first sample with amplicon features, so the per-feature BED, PDF
        # and PNG links are on its page; column 2 of the table is Features.
        page.goto(f'{base_url}/project/{project}')
        page.wait_for_load_state('domcontentloaded')
        sample = page.evaluate("""() => {
            for (const tr of document.querySelectorAll('tbody tr')) {
                const cells = tr.querySelectorAll('td');
                const link = cells[0] && cells[0].querySelector('a[href*="/sample/"]');
                if (link && parseInt(cells[1].textContent, 10) > 0)
                    return link.textContent.trim();
            }
            return null;
        }""")
        assert sample, 'no sample with features on the project page'
    page.goto(f'{base_url}/project/{project}/sample/{sample}')
    page.wait_for_load_state('domcontentloaded')

    timings = []
    _timed_download(page, lambda: page.get_by_role('link', name='Download Sample Data').click(),
                    'sample zip', timings, budget)
    _timed_download(page, lambda: page.get_by_role('link', name='Download Metadata').click(),
                    'sample metadata', timings, budget)
    bed = page.get_by_role('link', name='BED', exact=True).first
    if bed.count():
        _timed_download(page, bed.click, 'feature BED', timings, budget)
    for kind in ('PDF', 'PNG'):
        link = page.get_by_role('link', name=kind, exact=True).first
        if link.count():
            _timed_inline(page, base_url, link, f'feature {kind}', timings, budget)
    _report(f'sample downloads, {page.url}', timings)
    assert len(timings) >= 2


@pytest.mark.browser
@pytest.mark.slow
def test_project_page_download_buttons(authenticated_page, base_url, public_project_id):
    page = authenticated_page
    budget = float(os.environ.get('BROWSER_DOWNLOAD_BUDGET_S', '120'))
    project = _download_target(public_project_id)
    page.goto(f'{base_url}/project/{project}')
    page.wait_for_load_state('domcontentloaded')

    timings = []
    for handler, label in (('downloadSummaryTable', 'project summary'),
                           ('downloadMetadata', 'project metadata'),
                           ('downloadAndReload', 'project archive')):
        button = page.locator(f'[onclick^="{handler}("]').first
        assert button.count(), f'no {label} button'
        _timed_download(page, button.click, label, timings, budget)
    _report(f'project downloads, {page.url}', timings)
