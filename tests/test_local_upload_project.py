"""local_upload_project.py writes only to a local site."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'local_upload_project.py'
spec = importlib.util.spec_from_file_location('local_upload_project', SCRIPT)
local_upload_project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_upload_project)


@pytest.mark.parametrize('url', ['https://ampliconrepository.org', 'https://dev.ampliconrepository.org',
                                 'http://10.0.0.5:8000'])
def test_refuses_anything_but_localhost(url):
    with pytest.raises(SystemExit):
        local_upload_project.require_local(url)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8000', 'http://localhost:8011'])
def test_accepts_localhost(url):
    local_upload_project.require_local(url)
