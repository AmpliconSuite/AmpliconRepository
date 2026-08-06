"""
Regression tests for the sample-page read amplification that caused the
repeated production outages during the week of 2026-07-29.

Rendering ONE sample page called ``get_one_sample()``, which fetched the entire
project document — including the whole ``runs`` dict holding every sample and
every feature row in the project. On large projects that pulled ~7-19 MB from
DocumentDB per page view while sending ~150 KB to the client (~125x read
amplification). Under scraper load this saturated the instance's inbound
bandwidth allowance, stalled DocumentDB reads, and wedged every gunicorn worker.

The contract these tests pin down:
  * the requested sample's rows are returned in full (unchanged behaviour)
  * prev/next navigation still resolves to the right sample names
  * the returned project does NOT carry the other samples' feature rows
"""

import pytest
from bson.objectid import ObjectId


def _feature_row(sample_name, feature_id, bulk_size=0):
    """A feature row shaped like the real ones, optionally padded."""
    row = {
        'Sample_name': sample_name,
        'Feature_ID': feature_id,
        'AA_amplicon_number': 1,
        'Classification': 'ecDNA',
        'Location': "['chr1:1000-2000']",
        'Reference_version': 'hg38',
        'AA_PNG_file': f'{feature_id}.png',
        'Feature_BED_file': f'{feature_id}.bed',
    }
    if bulk_size:
        # Stand-in for the real per-feature payload, so a full-document fetch
        # is measurably more expensive than fetching one sample's rows.
        row['_bulk_payload'] = 'x' * bulk_size
    return row


def _project_doc(name, runs, creator):
    return {
        'project_name': name,
        'creator': creator,
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': runs,
        'sample_count': len(runs),
    }


@pytest.mark.integration
def test_get_one_sample_does_not_fetch_all_runs(mongo_collection, test_user):
    """The whole point: one sample page must not drag every sample over the wire."""
    from caper.utils import get_one_sample

    # 3 samples; the two we are NOT asking for carry a large payload.
    runs = {
        'SAMPLE_A': [_feature_row('SAMPLE_A', 'A_amplicon1', bulk_size=200_000)],
        'SAMPLE_B': [_feature_row('SAMPLE_B', 'B_amplicon1')],
        'SAMPLE_C': [_feature_row('SAMPLE_C', 'C_amplicon1', bulk_size=200_000)],
    }
    result = mongo_collection.insert_one(
        _project_doc('ReadAmplificationTest', runs, test_user.username)
    )
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})

    try:
        project, sample_data, prev_sample, next_sample = get_one_sample(
            project_id, 'SAMPLE_B')

        # --- unchanged behaviour: the requested sample comes back in full ---
        assert sample_data is not None, "requested sample was not found"
        assert sample_data[0]['Sample_name'] == 'SAMPLE_B'
        assert sample_data[0]['Feature_ID'] == 'B_amplicon1'

        # --- unchanged behaviour: prev/next navigation still resolves ---
        assert prev_sample and prev_sample[0].get('Sample_name') == 'SAMPLE_A'
        assert next_sample and next_sample[0].get('Sample_name') == 'SAMPLE_C'

        # --- the fix: the other samples' payloads must not be dragged along ---
        import bson
        project_bytes = len(bson.BSON.encode(
            {k: v for k, v in project.items() if k != '_id'}))
        assert project_bytes < 100_000, (
            f"get_one_sample returned a {project_bytes} byte project document; "
            "the non-requested samples' feature rows are still being fetched, "
            "which is the read amplification that took production down."
        )
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


@pytest.mark.integration
def test_get_one_sample_when_run_key_differs_from_sample_name(mongo_collection,
                                                              test_user):
    """Run keys are not guaranteed to equal Sample_name — the lookup must still work.

    The original implementation scanned every run comparing runs[key][0]['Sample_name'],
    so it tolerated a mismatch. Any optimisation must preserve that.
    """
    from caper.utils import get_one_sample

    runs = {
        'run_01': [_feature_row('tumor_alpha', 'alpha_amplicon1')],
        'run_02': [_feature_row('tumor_beta', 'beta_amplicon1')],
        'run_03': [_feature_row('tumor_gamma', 'gamma_amplicon1')],
    }
    result = mongo_collection.insert_one(
        _project_doc('RunKeyMismatchTest', runs, test_user.username)
    )
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})

    try:
        _, sample_data, prev_sample, next_sample = get_one_sample(
            project_id, 'tumor_beta')

        assert sample_data is not None, \
            "sample not found when the run key differs from Sample_name"
        assert sample_data[0]['Feature_ID'] == 'beta_amplicon1'
        assert prev_sample and prev_sample[0].get('Sample_name') == 'tumor_alpha'
        assert next_sample and next_sample[0].get('Sample_name') == 'tumor_gamma'
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


@pytest.mark.integration
def test_get_one_sample_follows_deleted_version_tombstone(mongo_collection, test_user):
    """A tombstoned old version must still resolve to the surviving project.

    get_one_sample() historically routed its lookup through get_one_project(),
    which resolves deleted-version tombstones. Any optimisation must keep doing
    so, or sample links to superseded versions 404 instead of redirecting.
    """
    from caper.utils import get_one_sample

    runs = {'SAMPLE_A': [_feature_row('SAMPLE_A', 'A_amplicon1')]}
    live = mongo_collection.insert_one(
        _project_doc('TombstoneSurvivor', runs, test_user.username)
    )
    live_id = live.inserted_id
    mongo_collection.update_one({'_id': live_id},
                                {'$set': {'linkid': str(live_id)}})

    tomb = mongo_collection.insert_one({
        'project_name': 'TombstoneOldVersion',
        'creator': test_user.username,
        'private': 'public',
        'delete': True,
        'current': False,
        'version_deleted_from_history': True,
        'payload_purged': True,
        'redirect_to_project': str(live_id),
    })
    tomb_id = tomb.inserted_id

    try:
        project, sample_data, _, _ = get_one_sample(str(tomb_id), 'SAMPLE_A')
        assert project is not None
        assert str(project['_id']) == str(live_id), \
            "tombstone did not resolve to the surviving project"
        assert sample_data is not None, \
            "sample not found after tombstone redirect"
        assert sample_data[0]['Feature_ID'] == 'A_amplicon1'
    finally:
        mongo_collection.delete_one({'_id': live_id})
        mongo_collection.delete_one({'_id': tomb_id})


@pytest.mark.integration
def test_get_one_sample_missing_sample_returns_none(mongo_collection, test_user):
    """A sample that isn't in the project must still return None, not raise."""
    from caper.utils import get_one_sample

    runs = {'SAMPLE_A': [_feature_row('SAMPLE_A', 'A_amplicon1')]}
    result = mongo_collection.insert_one(
        _project_doc('MissingSampleTest', runs, test_user.username)
    )
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})

    try:
        _, sample_data, prev_sample, next_sample = get_one_sample(
            project_id, 'NOT_A_REAL_SAMPLE')
        assert sample_data is None
        assert prev_sample is None
        assert next_sample is None
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})
