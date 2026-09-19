"""The co-amplification graph is built off the request worker, once per graph,
at most MAX_CONCURRENT at a time.  See caper/coamp_build.py.

Every test here runs against a scratch collection swapped in for
``builds_handle``, and captures process launches instead of performing them, so the queueing logic is exercised on the database (the slot cap is an
atomic array update whose operator support is the thing to prove) without
building a graph or touching neo4j.
"""

import datetime

import pytest
from django.test import RequestFactory


@pytest.fixture
def builds(monkeypatch):
    from caper import coamp_build
    from caper.utils import db_handle_primary, get_collection_handle
    scratch = get_collection_handle(db_handle_primary, 'coamp_builds_pytest')
    scratch.delete_many({})
    monkeypatch.setattr(coamp_build, 'builds_handle', scratch)
    try:
        yield scratch
    finally:
        scratch.drop()


@pytest.fixture
def submitted(monkeypatch):
    """Capture the builds that would have been launched as processes."""
    from caper import coamp_build
    calls = []

    def fake_launch(cache_key):
        calls.append(cache_key)
        return 4242
    monkeypatch.setattr(coamp_build, '_launch', fake_launch)
    return calls


@pytest.fixture
def cap(monkeypatch):
    from caper import coamp_build
    monkeypatch.setattr(coamp_build, 'MAX_CONCURRENT', 2)
    return 2


def _running(builds):
    from caper.coamp_build import SLOTS_ID
    doc = builds.find_one({'_id': SLOTS_ID}) or {}
    return [e['key'] for e in doc.get('running', [])]


@pytest.mark.integration
def test_a_request_is_recorded_and_started(builds, submitted, cap):
    from caper.coamp_build import request_build, build_status, RUNNING

    doc = request_build(['p1'], cache_key='k1')
    assert doc['state'] == RUNNING and doc['project_ids'] == ['p1']
    assert _running(builds) == ['k1']
    assert submitted == ['k1']
    assert builds.find_one({'_id': 'k1'})['build_pid'] == 4242
    status = build_status('k1')
    assert status['state'] == RUNNING and status['queued_ahead'] == 0 and status['running'] == ['k1']


@pytest.mark.integration
def test_the_same_graph_is_built_once(builds, submitted, cap):
    from caper.coamp_build import request_build

    first = request_build(['p1', 'p2'], cache_key='same')
    second = request_build(['p2', 'p1'], cache_key='same')
    assert first['_id'] == second['_id'] == 'same'
    assert builds.count_documents({'_id': 'same'}) == 1
    assert len(submitted) == 1, 'the second request started a second build of the same graph'


@pytest.mark.integration
def test_builds_beyond_the_cap_queue_and_are_promoted_on_release(builds, submitted, cap):
    from caper.coamp_build import (request_build, build_status, _release_slot,
                                   start_pending, QUEUED, RUNNING)

    for key in ('a', 'b', 'c'):
        request_build([key], cache_key=key)
    assert _running(builds) == ['a', 'b']
    assert build_status('c')['state'] == QUEUED
    assert build_status('c')['queued_ahead'] == 0
    assert submitted == ['a', 'b']

    # A fourth queues behind c.
    request_build(['d'], cache_key='d')
    assert build_status('d')['queued_ahead'] == 1

    # a finishes: its slot goes, and the oldest queued build takes it.
    _release_slot('a')
    builds.update_one({'_id': 'a'}, {'$set': {'state': 'done'}})
    assert start_pending() == ['c']
    assert _running(builds) == ['b', 'c']
    assert build_status('c')['state'] == RUNNING
    assert build_status('d')['state'] == QUEUED and build_status('d')['queued_ahead'] == 0


@pytest.mark.integration
def test_taking_a_slot_is_atomic_at_the_cap(builds, cap):
    """The cap is enforced by one update whose filter and push cannot be
    separated -- so it holds across workers, not just across threads.  This
    also proves the operator shape on whichever database the tests run on."""
    from caper.coamp_build import _take_slot, _slots, _running_keys

    now = datetime.datetime.utcnow()
    _slots()
    assert _take_slot('x', now) and _take_slot('y', now)
    assert not _take_slot('z', now), 'a third slot was handed out with the cap at 2'
    assert _running_keys(now) == ['x', 'y']


@pytest.mark.integration
def test_a_build_that_outlived_its_lease_reads_as_failed_and_can_be_retried(builds, submitted, cap):
    from caper.coamp_build import (request_build, build_status, FAILED, RUNNING,
                                   INTERRUPTED_MESSAGE, _take_slot)

    request_build(['p'], cache_key='stale')
    long_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=2)
    builds.update_one({'_id': 'stale'}, {'$set': {'lease_until': long_ago}})
    builds.update_one({'_id': '#slots', 'running.key': 'stale'},
                      {'$set': {'running.$.until': long_ago}})

    status = build_status('stale')
    assert status['state'] == FAILED and status['error'] == INTERRUPTED_MESSAGE
    assert status['running'] == [], 'an expired slot still counts as running'

    # Asking again replaces the dead record and starts afresh, in the slot it
    # had been holding.
    doc = request_build(['p'], cache_key='stale')
    assert doc['state'] == RUNNING and doc['error'] is None
    assert len(submitted) == 2
    assert _running(builds) == ['stale']


@pytest.mark.integration
def test_run_build_marks_done_releases_the_slot_and_saves_the_csv(builds, submitted, cap, monkeypatch):
    import pandas as pd
    from caper import views, neo4j_utils, coamp_build
    from caper.coamp_build import request_build, run_build, build_status, DONE

    request_build(['p'], cache_key='ok')
    saved = []
    monkeypatch.setattr(views, 'concat_projects', lambda ids: (pd.DataFrame({'x': [1]}), {}))
    monkeypatch.setattr(neo4j_utils, 'load_graph', lambda df, project_ids=None: type('G', (), {'get_edges_dataframe': None})())
    monkeypatch.setattr(views, '_save_coamp_edges', lambda key, graph: saved.append(key))

    assert run_build('ok', ['p']) is True
    assert saved == ['ok']
    assert build_status('ok')['state'] == DONE
    assert _running(builds) == []


@pytest.mark.integration
def test_run_build_records_the_failure_and_still_frees_the_slot(builds, submitted, cap, monkeypatch):
    from caper import views
    from caper.coamp_build import request_build, run_build, build_status, FAILED

    request_build(['p'], cache_key='boom')
    request_build(['q'], cache_key='next')   # takes the second slot
    request_build(['r'], cache_key='waiting')

    def explode(ids):
        raise RuntimeError('neo4j went away')
    monkeypatch.setattr(views, 'concat_projects', explode)

    assert run_build('boom', ['p']) is False
    status = build_status('boom')
    assert status['state'] == FAILED and 'neo4j went away' in status['error']
    # The freed slot went to the queued build.
    assert _running(builds) == ['next', 'waiting']


# ---------------------------------------------------------------------------
# The views around it
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_visualizer_renders_the_waiting_page_on_a_cache_miss(builds, submitted, cap, test_user, monkeypatch):
    from caper import views, neo4j_utils

    monkeypatch.setattr(neo4j_utils, 'check_cached_graph', lambda ids: False)
    monkeypatch.setattr(views, 'get_projects_metadata', lambda ids: {pid: [5, 2] for pid in ids})
    captured = {}
    monkeypatch.setattr(views, 'render',
                        lambda request, template, context: captured.update(template=template, **context) or 'rendered')

    request = RequestFactory().get('/coamplification-graph/visualizer/')
    request.user = test_user
    request.session = {'selected_projects': ['p1', 'p2']}
    assert views.visualizer(request) == 'rendered'

    assert captured['template'] == 'pages/visualizer_building.html'
    assert captured['project_count'] == 2 and captured['sample_count'] == 10 and captured['ecdna_sample_count'] == 4
    assert request.session['graph_available'] is False
    key = request.session['active_cache_key']
    assert builds.find_one({'_id': key})['project_ids'] == ['p1', 'p2']
    assert submitted == [key]


@pytest.mark.integration
def test_build_status_endpoint_reports_the_sessions_build(builds, submitted, cap, test_user):
    import json
    from caper.coamp_build import request_build
    from caper.views import coamp_build_status

    request_build(['p1'], cache_key='k')
    request = RequestFactory().get('/coamplification-graph/build-status/')
    request.user = test_user
    request.session = {'active_cache_key': 'k'}
    body = json.loads(coamp_build_status(request).content)
    assert body['state'] == 'running' and body['cache_key'] == 'k'

    request.session = {}
    assert coamp_build_status(request).status_code == 400
    request.session = {'active_cache_key': 'never-requested'}
    assert coamp_build_status(request).status_code == 404


@pytest.mark.integration
def test_a_launch_failure_fails_the_build_and_frees_the_slot(builds, cap, monkeypatch):
    from caper import coamp_build
    from caper.coamp_build import request_build, build_status, FAILED

    def broken(cache_key):
        raise OSError('no such file: manage.py')
    monkeypatch.setattr(coamp_build, '_launch', broken)

    request_build(['p'], cache_key='unlaunchable')
    status = build_status('unlaunchable')
    assert status['state'] == FAILED and 'could not be started' in status['error']
    assert _running(builds) == []


def test_the_build_command_exists():
    """The launcher runs `manage.py coamp_build`; the command must resolve."""
    from django.core.management import get_commands
    assert get_commands().get('coamp_build') == 'caper'
