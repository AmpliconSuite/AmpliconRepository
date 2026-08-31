"""
Root conftest.py — initialises Django before any test is collected.

pytest-django is not used because this project stores all data in MongoDB
rather than Django's ORM, so its transaction management adds no value.
Django is instead set up here in the same way as the existing standalone
test scripts, which is the established pattern for this project.
"""

import os
import sys


def _load_config_env():
    """Load config.env so settings.py finds all required environment variables."""
    repo_root = os.path.dirname(__file__)
    config_env = os.path.join(repo_root, 'caper', 'config.env')
    if not os.path.exists(config_env):
        return
    with open(config_env) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)


def _pin_reads_to_the_primary():
    """Make the test session read what it just wrote.

    The deployed connection string ends
    ``replicaSet=rs0&readPreference=secondaryPreferred``, which is right for the
    web app and fatal for a test suite: nearly every test writes a fixture and
    then queries for it through the application's own handles, and that query
    goes to a replica.

    This is not a lag that a retry would ride out. Measured against `caper-dev`
    on 2026-08-31, 40 write-then-immediately-read trials: **40 of 40 missed** on
    the default read preference and **0 of 40** missed pinned to the primary.
    Read-your-writes is never satisfied through that URI, which is why 12 of the
    60 failures on the dev server were this and nothing else.

    Rewriting the URI here rather than patching handles is deliberate: `caper.utils`
    builds ``db_handle``, ``fs_handle`` and the rest at import time and other
    modules bind those names directly, so there is no single object left to
    patch afterwards. Changing the environment before ``django.setup()`` catches
    every one of them at the source.

    This only works because ``get_db_handle`` was changed to let the connection
    string decide. It used to pass ``SECONDARY_PREFERRED`` to ``MongoClient``
    as an explicit keyword, which overrides the URI -- so this rewrite was
    silently a no-op, and the first version of this fix cleared 17 of the 60
    failures rather than the 12 it was aimed at plus the rest. If a future
    change reintroduces an explicit default there, this function goes quiet
    again; ``test_the_read_preference_kwarg_does_not_override_the_uri`` is the
    tripwire for that.

    The trade, stated so it stays a decision: a pinned suite no longer exercises
    the read path the site actually uses. That is the right trade -- these tests
    are for application logic, not for replication timing, and a test that fails
    on when a replica catches up is testing the cluster. It does mean a
    genuinely stale-read-dependent bug in the app would not be caught here.

    A single-node mongo, which is what runs locally, ignores this entirely.
    """
    uri = os.environ.get('DB_URI_SECRET')
    if not uri or 'readPreference=' not in uri:
        return
    import re
    os.environ['DB_URI_SECRET'] = re.sub(
        r'readPreference=[^&]*', 'readPreference=primary', uri)


def pytest_configure(config):
    """Called by pytest during startup — runs before any tests or fixtures."""
    repo_root = os.path.dirname(__file__)
    caper_dir = os.path.join(repo_root, 'caper')
    if caper_dir not in sys.path:
        sys.path.insert(0, caper_dir)

    _load_config_env()
    _pin_reads_to_the_primary()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
    # The test suite calls synchronous Django views/ORM code directly, while
    # some local pytest/plugin combinations leave an event loop active in the
    # main thread.  This keeps those sync tests deterministic across machines.
    os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

    import django
    django.setup()

    # Ensure Django ORM tables exist (idempotent; required by FileUploadView
    # which saves to the UploadTarFile model in SQLite).
    from django.core.management import call_command
    call_command('migrate', '--run-syncdb', verbosity=0)

    # filebrowser_safe (a Mezzanine dependency) accesses settings.DEFAULT_FILE_STORAGE
    # at module import time.  This setting was removed in Django 5.x.  Patching it here
    # keeps the test environment working without modifying the application's settings.py.
    from django.conf import settings as dj_settings
    if not hasattr(dj_settings, 'DEFAULT_FILE_STORAGE'):
        dj_settings.DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
