"""
Regression tests for tar path traversal on project tarball extraction.

Uploaded tarballs are attacker-controlled: ``FileUploadView`` (``POST
/upload_api/``) has ``permission_classes = []``, so anyone can hand the server
a ``.tar.gz``.  Until this fix, every extraction site called ``extractall()`` /
``extract()`` with no ``filter=`` argument, and on every Python version this
project runs on (3.10 in the Docker image, 3.12 locally) the default filter is
``fully_trusted``.  A member named ``results/../../../../x`` was therefore
written wherever the ``..`` sequence resolved to — in practice several levels
above the extraction target, i.e. into the application source tree next to
imported ``.py`` files.  The safe default does not arrive until Python 3.14.

The contract these tests pin down:
  * no member of an uploaded tar can write outside its destination directory,
    whether it escapes with ``..``, with an absolute path, or through a symlink
  * ``results/``-prefixed names get no free pass — the prefix check in
    ``tar_utils._extract_matching_members`` is satisfied by ``results/../..``
  * one hostile member does not abort extraction of the legitimate ones; it is
    skipped and logged, never silently swallowed
  * legitimate AmpliconSuite tarballs still extract their complete file set
  * the protection holds on interpreters predating the ``tarfile.data_filter``
    backport, via the fallback checker
"""

import io
import json
import os
import tarfile

import pytest

from caper import tar_safety
from caper.tar_safety import safe_extract_member, safe_extractall
from caper.tar_utils import _extract_matching_members
from caper.views import extract_project_files

from conftest import DATASET_SMALL_TAR

pytestmark = pytest.mark.integration


SENTINEL = 'TARSLIP_SENTINEL.txt'
SENTINEL_BODY = b'escaped\n'

# The destination sits this many directories below the sandbox root, mirroring
# the deployment layout (…/caper/media/<project id> under the source tree).
# Every escape vector below is tuned to land inside the sandbox but outside the
# destination, so a successful escape is detectable rather than scattered
# somewhere in /tmp.
DEST_DEPTH = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_file(tar, name, body=SENTINEL_BODY):
    info = tarfile.TarInfo(name)
    info.size = len(body)
    tar.addfile(info, io.BytesIO(body))


def _add_symlink(tar, name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)


def _build_hostile_tar(path):
    """A structurally valid tar that tries to write outside the destination.

    ``results/run.json`` is present and parseable because that is all
    ``create_project_helper`` validates before starting the extraction thread —
    a hostile upload reaches the sink looking exactly like this.
    """
    up = '../' * 3           # dest/<x> -> two levels above dest
    deep = '../' * 4         # dest/results/<x> -> two levels above dest
    with tarfile.open(path, 'w:gz') as tar:
        _add_file(tar, 'results/run.json', json.dumps({'runs': {}}).encode())
        _add_file(tar, 'results/legit.txt', b'legitimate payload\n')
        # plain relative escape
        _add_file(tar, f'{up}{SENTINEL}')
        # escape that still satisfies a startswith('results') prefix check
        _add_file(tar, f'results/{deep}{SENTINEL}')
        # symlink out of the destination, then a write through it
        _add_symlink(tar, 'results/escape_link', deep.rstrip('/'))
        _add_file(tar, f'results/escape_link/{SENTINEL}')
    return path


def _files_under(root):
    """Every regular file and symlink at or below *root*, as relative paths."""
    return {
        os.path.relpath(os.path.join(dirpath, name), root)
        for dirpath, _dirnames, filenames in os.walk(root)
        for name in filenames
    }


def _escapes(sandbox, dest):
    """Paths written anywhere under *sandbox* but outside *dest*."""
    inside = os.path.abspath(dest) + os.sep
    return sorted(
        os.path.join(dirpath, name)
        for dirpath, _dirnames, filenames in os.walk(sandbox)
        for name in filenames
        if name == SENTINEL
        and not os.path.abspath(os.path.join(dirpath, name)).startswith(inside)
    )


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway tree shaped like the deployment: destination nested deep.

    The intermediate directories stand in for the application source root that
    the original exploit reached.  Returns ``(sandbox_root, destination)``.
    """
    dest = tmp_path.joinpath('srv', 'AmpliconRepository', 'caper', 'media',
                             'PROJECT_ID')
    assert len(dest.relative_to(tmp_path).parts) - 1 == DEST_DEPTH
    dest.mkdir(parents=True)
    return tmp_path, dest


# ---------------------------------------------------------------------------
# The real sink: extract_project_files()
# ---------------------------------------------------------------------------

def test_extract_project_files_contains_traversal_members(sandbox):
    """The primary sink must not write outside its project directory."""
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    # Fails on the invalid project id long after extraction, and swallows that
    # exception itself; the escape, if any, has already happened by then.
    extract_project_files(tarfile, tar_path, str(dest),
                          'NOT_A_REAL_OBJECT_ID', None, None, [])

    assert _escapes(root, dest) == []


def test_extract_project_files_still_extracts_legitimate_members(sandbox):
    """A hostile member is skipped, not fatal to the rest of the archive."""
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    extract_project_files(tarfile, tar_path, str(dest),
                          'NOT_A_REAL_OBJECT_ID', None, None, [])

    assert (dest / 'results' / 'run.json').exists()
    assert (dest / 'results' / 'legit.txt').read_text() == 'legitimate payload\n'


# ---------------------------------------------------------------------------
# safe_extractall() / safe_extract_member()
# ---------------------------------------------------------------------------

def test_safe_extractall_blocks_every_escape_vector(sandbox):
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with tarfile.open(tar_path) as tar:
        rejected = safe_extractall(tar, str(dest))

    assert _escapes(root, dest) == []
    # Both ``..`` members and the outward-pointing symlink are refused.  The
    # write *through* that symlink is not refused and does not need to be: with
    # the link gone it creates an ordinary directory inside the destination.
    assert rejected == ['../../../' + SENTINEL,
                        'results/../../../../' + SENTINEL,
                        'results/escape_link']
    assert _files_under(dest) == {
        os.path.join('results', 'run.json'),
        os.path.join('results', 'legit.txt'),
        os.path.join('results', 'escape_link', SENTINEL),
    }


@pytest.mark.parametrize('has_data_filter', [True, False])
def test_safe_extractall_contains_absolute_member(sandbox, monkeypatch,
                                                  has_data_filter):
    """Absolute member names are not neutralised by tarfile on their own.

    ``tar.add()`` strips leading slashes when *writing* an archive, but a
    handcrafted member keeps its absolute name on the way back out, and an
    unfiltered ``extractall`` honours it.  The stdlib filter strips the leading
    slash so the member lands inside the destination; the fallback checker
    refuses it outright.  Either way nothing is written outside.
    """
    monkeypatch.setattr(tar_safety, '_HAS_DATA_FILTER', has_data_filter)
    root, dest = sandbox
    outside = root / 'outside'
    outside.mkdir()
    tar_path = root / 'absolute.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        _add_file(tar, str(outside / SENTINEL))
        _add_file(tar, 'results/legit.txt', b'ok\n')

    with tarfile.open(tar_path) as tar:
        safe_extractall(tar, str(dest))

    assert not (outside / SENTINEL).exists()
    assert _escapes(root, dest) == []
    assert (dest / 'results' / 'legit.txt').exists()


def test_safe_extractall_logs_rejected_members(sandbox, caplog):
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with caplog.at_level('WARNING'):
        with tarfile.open(tar_path) as tar:
            rejected = safe_extractall(tar, str(dest))

    # Silently dropping members would hide a broken legitimate upload just as
    # effectively as it hides an attack.
    assert rejected
    for name in rejected:
        assert name in caplog.text


def test_safe_extract_member_rejects_traversal(sandbox):
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with tarfile.open(tar_path) as tar:
        hostile = next(m for m in tar.getmembers() if m.name.startswith('../'))
        assert safe_extract_member(tar, hostile, str(dest)) is False
        assert safe_extract_member(tar, 'results/run.json', str(dest)) is True

    assert (dest / 'results' / 'run.json').exists()
    assert _escapes(root, dest) == []


def test_safe_extract_member_still_raises_keyerror_for_missing_name(sandbox):
    """Callers branch on KeyError to fall back to an alternative member name."""
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with tarfile.open(tar_path) as tar:
        with pytest.raises(KeyError):
            safe_extract_member(tar, 'results/not_here.json', str(dest))


def test_fallback_checker_blocks_traversal_without_data_filter(sandbox, monkeypatch):
    """Interpreters predating the data_filter backport must be protected too."""
    monkeypatch.setattr(tar_safety, '_HAS_DATA_FILTER', False)
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with tarfile.open(tar_path) as tar:
        rejected = safe_extractall(tar, str(dest))

    assert _escapes(root, dest) == []
    assert rejected == ['../../../' + SENTINEL,
                        'results/../../../../' + SENTINEL,
                        'results/escape_link']
    assert _files_under(dest) == {
        os.path.join('results', 'run.json'),
        os.path.join('results', 'legit.txt'),
        os.path.join('results', 'escape_link', SENTINEL),
    }


# ---------------------------------------------------------------------------
# tar_utils streaming extraction
# ---------------------------------------------------------------------------

def test_extract_matching_members_prefix_check_is_not_enough(sandbox):
    """``results/../..`` satisfies startswith('results') — it must still fail."""
    root, dest = sandbox
    tar_path = _build_hostile_tar(str(root / 'hostile.tar.gz'))

    with tarfile.open(tar_path) as tar:
        extracted = _extract_matching_members(tar, 'results', str(dest))

    assert _escapes(root, dest) == []
    assert (dest / 'results' / 'legit.txt').exists()
    # Five members match the prefix; the ``..`` escape and the outward symlink
    # are refused and are not counted as extracted.
    assert extracted == 3


# ---------------------------------------------------------------------------
# Legitimate uploads are unaffected
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(DATASET_SMALL_TAR),
                    reason='test fixture tarball not present')
def test_real_fixture_extracts_completely(tmp_path):
    """A genuine AmpliconSuite tarball loses nothing to the safety filter."""
    dest = tmp_path / 'dest'
    dest.mkdir()

    with tarfile.open(DATASET_SMALL_TAR) as tar:
        expected = {m.name.lstrip('./') for m in tar.getmembers() if m.isfile()}

    with tarfile.open(DATASET_SMALL_TAR) as tar:
        rejected = safe_extractall(tar, str(dest))

    assert rejected == []
    assert {p.replace(os.sep, '/') for p in _files_under(dest)} == expected
