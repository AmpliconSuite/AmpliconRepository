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


def pytest_configure(config):
    """Called by pytest during startup — runs before any tests or fixtures."""
    repo_root = os.path.dirname(__file__)
    caper_dir = os.path.join(repo_root, 'caper')
    if caper_dir not in sys.path:
        sys.path.insert(0, caper_dir)

    _load_config_env()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')

    import django
    django.setup()

    # filebrowser_safe (a Mezzanine dependency) accesses settings.DEFAULT_FILE_STORAGE
    # at module import time.  This setting was removed in Django 5.x.  Patching it here
    # keeps the test environment working without modifying the application's settings.py.
    from django.conf import settings as dj_settings
    if not hasattr(dj_settings, 'DEFAULT_FILE_STORAGE'):
        dj_settings.DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
