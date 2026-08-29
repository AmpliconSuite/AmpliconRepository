"""Removing every sample from a project is allowed, and produces an empty one.

Reported from dev, 2026-08-28: "edit then delete last sample fails
aggregation".  Reproduced locally the same day, and the cause is not the site
mishandling an edge case -- the aggregator refuses, on its own terms correctly:

    asa_stages.py  _abort("Every sample in the input was excluded --
                          there is nothing left to aggregate.")  -> sys.exit(1)

It has no empty result table to emit.  Jens decided on 2026-08-29 that removing
every sample must be possible, and that the site should not run the aggregator
for it at all.  So the site writes the empty version itself, with no payload.

This matters beyond the edit form: the version history's delete control is
hidden unless a project has more than one active version, so removing samples is
the only route an ordinary user has to an empty project.
"""

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
)


def _edit_removing(request_factory, user, project_id, project_name, samples):
    """An edit request that asks for *samples* to be removed."""
    data = {
        'project_name': project_name,
        'description': 'removing every sample',
        'private': 'private',
        'publication_link': '',
        'project_members': '',
        'alias': '',
        'remap_sample_names': 'false',
        'accept_license': 'on',
        'samples_to_remove': samples,
    }
    request = request_factory.post(f'/project/{project_id}/edit', data=data)
    request.user = user
    return request


@pytest.mark.slow
@pytest.mark.integration
def test_removing_every_sample_produces_an_empty_live_version(
        request_factory, test_user, mongo_collection):
    """The whole transition, end to end.

    Asserts the outcome rather than recording it: the product question ("should
    this be possible?") was answered yes, so there is now a right answer to
    pin.
    """
    from caper.views import create_project, edit_project_page
    from caper.project_status import LIVE, SUPERSEDED, classify

    created = []
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'RemoveEverySample',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        original = _project_id_from_redirect(response)
        created.append(original)
        doc = _poll_until_finished(mongo_collection, original)
        assert doc and not doc.get('aggregation_failed'), \
            f"create failed: {doc.get('error_message') if doc else 'timed out'}"

        # The identifier the edit form posts is the feature rows' 'Sample_name',
        # NOT the runs dict's key. They differ -- the key is positional
        # ('sample_1'), the name is the sample's own ('GBM39'). Sending the key
        # instead removes nothing, silently. Measured on dev, 2026-08-28, by
        # making exactly that mistake here.
        samples = sorted({
            features[0]['Sample_name']
            for features in (doc.get('runs') or {}).values()
            if features and features[0].get('Sample_name')
        })
        assert samples, 'the fixture project has no named samples to remove'

        edit_response = edit_project_page(
            _edit_removing(request_factory, test_user, original,
                           'RemoveEverySample', samples),
            project_name=original)

        new_id = _project_id_from_redirect(edit_response)
        assert new_id and new_id != original, \
            'removing every sample must still make a new version, not edit in place'
        created.append(new_id)

        result = _poll_until_finished(mongo_collection, new_id)
        assert result is not None, 'the new version never finished'
        assert not result.get('aggregation_failed'), (
            f'removing every sample still fails: '
            f'{result.get("error_message")!r}')

        # The new version: empty, live, and the head of the chain.
        assert not result.get('runs'), 'the new version still has samples'
        assert result.get('sample_count') == 0
        assert result.get('EMPTY?') is True, \
            "EMPTY? must describe what the version actually holds"
        assert classify(result) == LIVE, \
            f'the empty version is {classify(result)}, not the live head'
        assert result.get('is_latest') is True
        assert result.get('FINISHED?') is True, \
            'nothing extracts afterwards, so it is finished when written'

        # No payload: an empty version names no archive and no GridFS files.
        assert result.get('tarfile') is None, \
            'an empty version must not claim a payload it does not have'

        # The version that had the samples still has them, and steps down.
        survivor = mongo_collection.find_one({'_id': ObjectId(original)})
        assert survivor is not None, \
            'removing every sample destroyed the version that had them'
        assert survivor.get('runs'), \
            'the removal landed in place; it must land in a new version'
        assert classify(survivor) == SUPERSEDED
        assert survivor.get('is_latest') is not True, 'two heads in one chain'

        # Lineage: one chain, contiguous ordinals, pointers both ways.
        assert result.get('version_chain_id') == survivor.get('version_chain_id')
        assert survivor.get('next_version_id') == ObjectId(new_id)
        assert result.get('previous_version_id') == ObjectId(original)
        assert result.get('version_ordinal') == survivor.get('version_ordinal') + 1

        # Project-level fields survive the emptying -- they belong to the
        # project, not to the samples that happened to be in it.
        assert result.get('project_name') == 'RemoveEverySample'
        assert result.get('description') == 'removing every sample'

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)


@pytest.mark.slow
@pytest.mark.integration
def test_the_emptied_project_still_serves_its_pages(
        request_factory, test_user, mongo_collection):
    """An empty project is live and reachable, not a 404 and not a 500.

    The page-render matrix covers each *status*; this covers the shape, which
    is the thing that broke before -- validate_project() ran ahead of the
    is_empty_project branch and subscripted runs.
    """
    from caper.views import (
        create_project, edit_project_page, project_page, project_download,
    )
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware

    created = []
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'EmptiedProjectPages',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        original = _project_id_from_redirect(response)
        created.append(original)
        doc = _poll_until_finished(mongo_collection, original)
        assert doc and not doc.get('aggregation_failed')

        samples = sorted({
            features[0]['Sample_name']
            for features in (doc.get('runs') or {}).values()
            if features and features[0].get('Sample_name')
        })
        edit_response = edit_project_page(
            _edit_removing(request_factory, test_user, original,
                           'EmptiedProjectPages', samples),
            project_name=original)
        new_id = _project_id_from_redirect(edit_response)
        created.append(new_id)
        assert _poll_until_finished(mongo_collection, new_id) is not None

        def _get(view, project_id):
            req = request_factory.get(f'/project/{project_id}',
                                      HTTP_HOST='localhost')
            req.user = test_user
            SessionMiddleware(lambda r: None).process_request(req)
            req.session.save()
            req._messages = FallbackStorage(req)
            try:
                rendered = view(req, project_name=project_id)
            except Exception as exc:                    # noqa: BLE001
                name = type(exc).__name__
                if 'Http404' in name:
                    return 'Http404'
                if 'TokenRetrieval' in name or 'NoCredentials' in name:
                    return 'no-aws'
                raise
            if hasattr(rendered, 'render'):
                rendered.render()
            return f'HTTP {rendered.status_code}'

        assert _get(project_page, new_id) == 'HTTP 200', \
            'an empty project must render its page, not 404 and not raise'

        # It has no payload, so there is nothing to download. 404 is the right
        # answer; raising is not.
        assert _get(project_download, new_id) in ('HTTP 404', 'Http404', 'no-aws')

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)


def test_removes_every_sample_predicate():
    """The decision itself, without the 40 seconds of aggregation.

    Cheap enough to enumerate, and it guards the one distinction that already
    caused a silent wrong answer once: names vs runs keys.
    """
    from caper.views import removes_every_sample

    one = {'runs': {'sample_1': [{'Sample_name': 'GBM39'}]}}
    two = {'runs': {'sample_1': [{'Sample_name': 'GBM39'}],
                    'sample_2': [{'Sample_name': 'HK359'}]}}

    assert removes_every_sample(one, ['GBM39'], []) is True
    assert removes_every_sample(two, ['GBM39', 'HK359'], []) is True

    # Removing some is an ordinary re-aggregation.
    assert removes_every_sample(two, ['GBM39'], []) is False
    # Nothing asked for.
    assert removes_every_sample(one, [], []) is False
    # Samples arriving in the same edit means the result is not empty.
    assert removes_every_sample(one, ['GBM39'], ['/tmp/new.tar.gz']) is False
    # The runs key, not the sample name -- matches nothing, so not this case.
    assert removes_every_sample(one, ['sample_1'], []) is False
    # Already empty: there is nothing to remove, so this is not the transition.
    assert removes_every_sample({'runs': {}}, ['GBM39'], []) is False
    assert removes_every_sample({}, ['GBM39'], []) is False
