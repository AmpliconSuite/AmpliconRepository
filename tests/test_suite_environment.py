"""Guards on the things that made this suite pass locally and fail on dev.

Issue #582. The suite has no isolated test database -- it uses whatever
``DB_NAME`` and ``DB_URI_SECRET`` the environment hands it -- so it inherits
both the data and the connection semantics of wherever it runs. Sixty failures
on the dev server on 2026-08-31 came from four environment faults and not one
from the code under test. These tests pin down the two that were fixed in code;
the other two were a stale document set and a stray file, which no test can
hold in place.
"""

import importlib.util
import os
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parents[1]


def _module(path, name):
    """Load a module by file path.

    Neither of the two modules under test here can be reached by an ordinary
    import: bare ``conftest`` resolves to ``tests/conftest.py`` rather than the
    root one, and ``tests`` is shadowed by an unrelated package of that name in
    site-packages. This is the same loader the audit-backfill tests use to
    reach the standalone migration scripts.
    """
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- reads must see this process's own writes -----------------------------

def test_the_deployed_read_preference_is_rewritten_for_tests():
    """``secondaryPreferred`` is correct for the site and fatal here.

    Measured against `caper-dev` on 2026-08-31: of 40 write-then-immediately-
    read trials, **40 missed** on the default read preference and **0 missed**
    pinned to the primary. Every miss is a test that wrote a fixture and then
    could not find it. A retry or a sleep does not fix a 100% miss rate.
    """
    pin = _module('conftest.py', 'root_conftest')._pin_reads_to_the_primary

    deployed = ('mongodb://user:pw@host:27017/?tls=true&replicaSet=rs0'
                '&readPreference=secondaryPreferred&retryWrites=false')
    os.environ['DB_URI_SECRET'] = deployed
    try:
        pin()
        rewritten = os.environ['DB_URI_SECRET']
    finally:
        os.environ['DB_URI_SECRET'] = deployed

    assert 'readPreference=primary' in rewritten
    assert 'secondaryPreferred' not in rewritten
    # Everything else about the URI has to survive, credentials included --
    # rebuilding it from parts would be a good way to drop the CA file or the
    # replica set name and spend an afternoon on it.
    assert 'replicaSet=rs0' in rewritten
    assert 'retryWrites=false' in rewritten
    assert rewritten.startswith('mongodb://user:pw@host:27017/')


def test_a_uri_naming_no_read_preference_is_left_alone():
    """The local single-node mongo has no readPreference and needs none.

    Appending one would work, but silently editing a URI this function was not
    asked to change is how a local run starts differing from what the developer
    configured.
    """
    pin = _module('conftest.py', 'root_conftest')._pin_reads_to_the_primary

    plain = 'mongodb://localhost:27017/'
    os.environ['DB_URI_SECRET'] = plain
    try:
        pin()
        assert os.environ['DB_URI_SECRET'] == plain
    finally:
        os.environ['DB_URI_SECRET'] = plain


# --- a staged slot needs an owner this namespace can see -------------------

def test_a_staged_slot_is_owned_by_a_pid_that_exists_here():
    """``os.getppid()`` is 0 under ``docker exec``, which means "nobody".

    The load-shed tests stage in-flight requests owned by another live worker.
    Inside a container the parent process lives outside the PID namespace, so
    the parent pid is 0, the dead-worker reaper reclaims every staged slot
    before the shedder can refuse anything, and eleven tests that assert 503
    get a cheerful 200 instead. None of it reproduces on a laptop.
    """
    shedding = _module('tests/test_load_shedding.py', 'shedding_under_test')

    pid = shedding._live_foreign_pid()

    assert pid != 0, 'pid 0 is not a process; the reaper will free the slot'
    assert pid != os.getpid(), 'the slot must belong to some *other* worker'
    assert os.path.exists(f'/proc/{pid}'), (
        f'pid {pid} is not visible in this namespace, so it reads as dead')


def test_the_owner_pid_is_not_taken_from_the_parent():
    """The specific regression, named at the call rather than the symptom.

    Written as a source check because the failure only appears in a container:
    a laptop run would pass with `getppid()` restored and the eleven dev
    failures would come straight back.
    """
    import inspect

    shedding = _module('tests/test_load_shedding.py', 'shedding_under_test')

    source = inspect.getsource(shedding._occupy)
    assert 'getppid' not in source, (
        '_occupy must not own slots with the parent pid; it is 0 in a container'
    )


@pytest.mark.parametrize('name', ['DB_URI_SECRET', 'DB_NAME'])
def test_the_suite_documents_that_it_shares_the_environments_database(name):
    """No isolated test database exists, and that is worth stating.

    These tests run against whatever the environment points at -- a local mongo
    on a laptop, `caper-dev` on the dev server. That is why leftover fixture
    documents in `caper-dev` could break 35 search tests without anything in
    the repository changing.
    """
    assert os.environ.get(name), (
        f'{name} is unset; this suite has no database of its own to fall back '
        'on and every integration test would fail at connection time'
    )


# --- the fixture seeder's guard, and the half of it that must not move -----

def test_seeding_still_refuses_a_non_local_database():
    """The guard that was relaxed for purging must still hold for seeding.

    `_assert_local` exists because dev and production are two databases on one
    cluster separated by one environment variable, and a seeder aimed at the
    wrong one puts documents named `Awkward_NameCollision` on a real site.
    Purging was deliberately exempted on 2026-08-31 -- it selects on the
    fixture marker alone and the need to clean a shared cluster arises exactly
    where the guard was once missing. Writing is a different act and keeps the
    guard. This test is the difference, held in place.
    """
    seeder = _module('tests/awkward_states.py', 'awkward_under_test')

    remote = ('mongodb://user:pw@docdb.cluster-abc.us-east-1.docdb.amazonaws.com'
              ':27017/?tls=true&replicaSet=rs0')
    with pytest.raises(SystemExit) as raised:
        seeder._assert_local(remote, 'caper')
    assert 'refusing to write' in str(raised.value)

    assert seeder._assert_local('mongodb://localhost:27017/', 'caper-local')


def test_the_purge_selects_on_the_marker_and_nothing_else():
    """What makes purging safe to run anywhere, stated as a test.

    If this ever selects on a project name, a date, or a missing field, it can
    reach a document it did not write -- and it now runs against shared
    clusters, where such a document belongs to somebody.
    """
    import inspect

    seeder = _module('tests/awkward_states.py', 'awkward_under_test')

    source = inspect.getsource(seeder.purge)
    assert f"{{MARKER: True}}" in source or "MARKER: True" in source
    for reckless in ('project_name', "'date'", '$exists', '$ne', 'delete_many({})'):
        assert reckless not in source, (
            f'purge selects on {reckless!r}; it can now reach documents it did '
            'not create')


def test_the_read_preference_kwarg_does_not_override_the_uri():
    """The tripwire for the reason the first version of this fix did nothing.

    ``get_db_handle`` used to default *read_preference* to SECONDARY_PREFERRED
    and hand it to ``MongoClient`` as a keyword. A keyword beats the connection
    string, so rewriting the URI in the root conftest changed nothing at all --
    ``db_handle.client.read_preference`` still came back SecondaryPreferred
    inside a test run on dev on 2026-08-31, measured directly.

    Deployed behaviour is unaffected either way, because the prod and dev URIs
    already name ``secondaryPreferred``. What matters is that the value stays
    overridable from outside, which is the only reason the test session can ask
    for read-your-writes.
    """
    import inspect

    from caper.utils import get_db_handle

    signature = inspect.signature(get_db_handle)
    default = signature.parameters['read_preference'].default
    assert default is None, (
        'get_db_handle must default to None so the connection string governs; '
        f'it defaults to {default!r}, which silently overrides the URI and '
        'makes the conftest read-preference pin a no-op'
    )


# --- fixtures must not need privileges the application lacks ---------------

def test_no_fixture_creates_its_own_database():
    """A scratch *database* cannot exist on a least-privilege deployment.

    Dev connects as `caper_app_dev`, whose role is scoped to `caper-dev`.
    Measured 2026-08-31: that credential reads and writes `caper-dev` fine,
    pings `admin` fine, and gets `Authorization failure` the moment it touches
    a database of another name. Two fixtures created one per test and took 34
    tests down with them.

    This matters beyond the test suite: prod is still on the shared `fkim`
    credential, and the cutover to `caper_app_prod` would hit exactly this.
    A scratch collection inside the configured database needs no privilege the
    application does not already have.
    """
    from pathlib import Path

    offenders = []
    for path in sorted((REPO_ROOT / 'tests').glob('test_*.py')):
        if path.name == 'test_suite_environment.py':
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split('#', 1)[0]
            if 'drop_database' in code or 'client.drop_database' in code:
                offenders.append(f'{path.name}:{lineno}: {line.strip()[:70]}')

    assert not offenders, (
        'these fixtures create and drop a whole database, which a '
        'least-privilege user cannot do; use a uniquely-named collection '
        'inside DB_NAME instead:\n  ' + '\n  '.join(offenders))


def test_the_source_guards_read_tracked_files_only():
    """An untracked file is somebody's scratch copy, not the codebase.

    Three guards were failing on the dev server against `holding/settings.py`,
    `caper/caper/view_old.py` and a measurement script left in the checkout --
    none of them ever committed. A guard that reads whatever is lying around
    reports faults that belong to no one and differ per machine.
    """
    from conftest import tracked_python_files

    tracked = tracked_python_files(REPO_ROOT)

    assert tracked, 'the helper found no files at all; the guards check nothing'
    assert 'conftest.py' in tracked
    assert any(name.startswith('caper/caper/') for name in tracked)
    # The two files that actually caused this, named so the intent survives.
    for stray in ('holding/settings.py', 'caper/caper/view_old.py'):
        assert stray not in tracked, (
            f'{stray} is tracked now; if that is deliberate the guards will '
            'read it, which is the correct behaviour -- update this test')
