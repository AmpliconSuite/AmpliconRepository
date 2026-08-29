"""What happens when an edit removes every sample a project has.

Reported from dev, 2026-08-28: "edit then delete last sample fails
aggregation".  That matters more than a failed edit usually would, because it
is the one route an ordinary user has to an empty project -- the version
history's delete control is hidden unless a project has more than one active
version, so deleting versions cannot empty a project either.

The spec's model says an empty project is a legitimate state (T6): the chain is
the project, EMPTY is derived from its members, and T9 re-populates it.  So
either this should produce an empty project or it should be refused with
something a user can act on.  Failing aggregation is the third thing, and it is
the one that leaves a placeholder marked aggregation_failed behind.

This file measures which of those happens rather than assuming.
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
def test_removing_every_sample_says_what_it_does(
        request_factory, test_user, mongo_collection):
    """Records the current behaviour, whatever it is.

    Written as a measurement rather than an assertion of the desired end state,
    because which end state is wanted is a product decision that has not been
    made. What it does pin down is that the outcome is one of the three
    possibilities and not something worse -- in particular that the original
    version survives, since a failed edit must never take the project with it.
    """
    from caper.views import create_project, edit_project_page
    from caper.project_status import classify

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

        # The identifier the edit form posts, which is the feature rows'
        # 'Sample_name' and NOT the runs dict's key. They differ -- the key is
        # positional ('sample_1'), the name is the sample's own ('GBM39') --
        # and remove_samples_from_runs() matches on the name. Sending the key
        # instead produces a new version identical to the old one, silently:
        # the aggregator finds no sample by that name, excludes nothing, and
        # nothing reports that the removal did not happen. Measured on dev,
        # 2026-08-28, by making exactly that mistake here.
        samples = sorted({
            features[0]['Sample_name']
            for features in (doc.get('runs') or {}).values()
            if features and features[0].get('Sample_name')
        })
        assert samples, 'the fixture project has no named samples to remove'
        print(f'\nremoving {samples} '
              f'(runs keys are {sorted(doc.get("runs") or {})})')

        edit_response = edit_project_page(
            _edit_removing(request_factory, test_user, original,
                           'RemoveEverySample', samples),
            project_name=original)

        new_id = _project_id_from_redirect(edit_response)
        if new_id and new_id != original:
            created.append(new_id)
            result = _poll_until_finished(mongo_collection, new_id)
        else:
            result = mongo_collection.find_one({'_id': ObjectId(original)})

        # Whatever happened, the version that had the samples is still there.
        survivor = mongo_collection.find_one({'_id': ObjectId(original)})
        assert survivor is not None, \
            'removing every sample destroyed the version that had them'
        assert survivor.get('runs'), \
            'the original version lost its samples; a new version is where a ' \
            'removal lands, never in place'

        outcome = (
            'refused' if not new_id or new_id == original
            else 'aggregation_failed' if result and result.get('aggregation_failed')
            else 'empty_project' if result and not result.get('runs')
            else 'still_has_samples')
        print(f'\nremoving every sample -> {outcome}')
        if result is not None:
            print(f'  status: {classify(result)}')
            print(f'  samples: {len(result.get("runs") or {})}')
            print(f'  error:  {str(result.get("error_message"))[:120]}')

        assert outcome != 'still_has_samples', \
            'the samples were not removed at all, which is the one outcome ' \
            'that silently does nothing'

        # Whichever of the three outcomes this is, the user has to be told
        # something they can act on. The aggregator refuses this edit with a
        # sentence saying why; until 2026-08-28 the site replaced that sentence
        # with str(SystemExit(1)), so the project's error_message was the single
        # character '1'.
        if outcome == 'aggregation_failed':
            stored = str(result.get('error_message') or '')
            assert stored.rstrip('. ') != 'An error occurred during aggregation: 1', \
                'the failure message is the exit status again'
            assert 'excluded' in stored, (
                f'the aggregator explained why it refused and the site did not '
                f'pass it on; stored message was {stored!r}')

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)
