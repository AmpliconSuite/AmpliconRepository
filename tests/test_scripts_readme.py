"""scripts/README.md has one row per file under scripts/, and no rows for files that are gone.

The cleanup that created scripts/ started from ~55 files at the repository root that nobody
could classify: what each was for, who ran it, whether it was finished. The page exists so
that question has an answer written down when a script is added, not rediscovered later.
"""
import os
import re
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, 'scripts', 'README.md')
ROW = re.compile(r'^\| `([\w\-]+\.(?:py|sh|conf))` \|', re.MULTILINE)


def _tracked_script_files():
    """Tracked files under scripts/, not whatever is lying in the checkout.

    An untracked scratch copy on a server is nobody's script, and a guard that read it would
    fail on a machine-specific file (the reason tests/conftest.py's helper exists).
    """
    try:
        listed = subprocess.run(
            ['git', '-c', 'safe.directory=*', '-C', REPO_ROOT, 'ls-files', '-z', 'scripts'],
            capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f'cannot enumerate tracked files: {exc}')
    names = [n for n in listed.decode().split('\0') if n]
    return sorted(n for n in names if os.path.basename(n) != 'README.md')


def test_every_script_has_a_row_and_every_row_has_a_script():
    tracked = _tracked_script_files()
    # A guard that scans nothing passes forever; the directory has dozens of files.
    assert len(tracked) >= 25, f'found only {len(tracked)} files under scripts/'

    with open(README, encoding='utf-8') as handle:
        rows = ROW.findall(handle.read())
    assert len(rows) == len(set(rows)), 'a script is listed twice in scripts/README.md'

    on_disk = {os.path.basename(n) for n in tracked}
    assert len(on_disk) == len(tracked), 'two scripts share a basename in different directories'
    assert on_disk - set(rows) == set(), 'no row in scripts/README.md for these scripts'
    assert set(rows) - on_disk == set(), 'scripts/README.md lists scripts that do not exist'


def test_each_row_sits_under_its_directorys_heading():
    """A row listed under `maintenance/` must be a file in scripts/maintenance/."""
    tracked = _tracked_script_files()
    location = {os.path.basename(n): os.path.dirname(n) for n in tracked}

    directory = None
    misfiled = []
    with open(README, encoding='utf-8') as handle:
        for line in handle:
            heading = re.match(r'^## (\w+)/\s*$', line)
            if heading:
                directory = 'scripts/' + heading.group(1)
            elif line.startswith('## '):
                directory = None
            row = ROW.match(line)
            if row and location.get(row.group(1)) != directory:
                misfiled.append(f'{row.group(1)} is in {location.get(row.group(1))}, listed under {directory}')
    assert misfiled == []
