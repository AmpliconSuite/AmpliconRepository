import copy

import pytest
from bson.objectid import ObjectId


def _stat_subset(stats):
    return {
        'public_proj_count': stats.get('public_proj_count', 0),
        'public_sample_count': stats.get('public_sample_count', 0),
        'all_private_proj_count': stats.get('all_private_proj_count', 0),
        'all_private_sample_count': stats.get('all_private_sample_count', 0),
        'public_amplicon_classifications_count': copy.deepcopy(
            stats.get('public_amplicon_classifications_count', {})
        ),
        'all_private_amplicon_classifications_count': copy.deepcopy(
            stats.get('all_private_amplicon_classifications_count', {})
        ),
        'public_tissue_of_origin_count': copy.deepcopy(
            stats.get('public_tissue_of_origin_count', {})
        ),
        'all_private_tissue_of_origin_count': copy.deepcopy(
            stats.get('all_private_tissue_of_origin_count', {})
        ),
    }


def _cleanup_stats_since(start_id):
    from caper.site_stats import site_statistics_handle

    site_statistics_handle.delete_many({'_id': {'$gt': start_id}})


def _latest_subset():
    from caper.site_stats import get_latest_site_statistics

    return _stat_subset(get_latest_site_statistics())


def _project(sample_specs, visibility='private'):
    runs = {}
    for idx, (classification, tissue) in enumerate(sample_specs, start=1):
        sample_name = f'StatsSample_{idx}'
        runs[sample_name] = [{
            'Sample_name': sample_name,
            'Classification': classification,
            'Tissue_of_origin': tissue,
        }]
    return {
        '_id': ObjectId(),
        'project_name': 'StatsUnitProject',
        'private': visibility,
        'delete': False,
        'current': True,
        'runs': runs,
    }


def _count(stats, bucket, key):
    return stats[f'{bucket}_count'].get(key, 0)


def _bucket_count(stats, bucket, key):
    return stats[bucket].get(key, 0)


@pytest.mark.slow
@pytest.mark.integration
def test_private_project_add_and_delete_updates_site_stats():
    from caper.site_stats import (
        add_project_to_site_statistics,
        delete_project_from_site_statistics,
        get_latest_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    before = _latest_subset()
    project = _project([
        ('ecDNA', 'brain'),
        ('Linear amplification', 'lung'),
    ], visibility='private')

    try:
        add_project_to_site_statistics(project, is_private=True)
        after_add = _latest_subset()

        assert after_add['all_private_proj_count'] == before['all_private_proj_count'] + 1
        assert after_add['all_private_sample_count'] == before['all_private_sample_count'] + 2
        assert after_add['public_proj_count'] == before['public_proj_count']
        assert after_add['public_sample_count'] == before['public_sample_count']
        assert _count(after_add, 'all_private_amplicon_classifications', 'ecDNA') == (
            _count(before, 'all_private_amplicon_classifications', 'ecDNA') + 1
        )
        assert _count(after_add, 'all_private_tissue_of_origin', 'brain') == (
            _count(before, 'all_private_tissue_of_origin', 'brain') + 1
        )

        delete_project_from_site_statistics(project, is_private=True)
        assert _latest_subset() == before
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_public_project_add_and_delete_updates_site_stats():
    from caper.site_stats import (
        add_project_to_site_statistics,
        delete_project_from_site_statistics,
        get_latest_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    before = _latest_subset()
    project = _project([
        ('BFB', 'breast'),
        ('Virus', 'cervix'),
    ], visibility='public')

    try:
        add_project_to_site_statistics(project, is_private=False)
        after_add = _latest_subset()

        assert after_add['public_proj_count'] == before['public_proj_count'] + 1
        assert after_add['public_sample_count'] == before['public_sample_count'] + 2
        assert after_add['all_private_proj_count'] == before['all_private_proj_count']
        assert after_add['all_private_sample_count'] == before['all_private_sample_count']
        assert _count(after_add, 'public_amplicon_classifications', 'BFB') == (
            _count(before, 'public_amplicon_classifications', 'BFB') + 1
        )
        assert _count(after_add, 'public_tissue_of_origin', 'breast') == (
            _count(before, 'public_tissue_of_origin', 'breast') + 1
        )

        delete_project_from_site_statistics(project, is_private=False)
        assert _latest_subset() == before
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_visibility_transitions_move_stats_between_public_and_private_buckets():
    from caper.site_stats import (
        add_project_to_site_statistics,
        edit_proj_privacy,
        get_latest_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    before = _latest_subset()
    project = _project([
        ('ecDNA', 'brain'),
        ('BFB', 'brain'),
    ], visibility='private')

    try:
        add_project_to_site_statistics(project, is_private=True)
        after_private_create = _latest_subset()

        edit_proj_privacy(project, 'private', 'public')
        after_public = _latest_subset()
        assert after_public['all_private_proj_count'] == before['all_private_proj_count']
        assert after_public['all_private_sample_count'] == before['all_private_sample_count']
        assert after_public['public_proj_count'] == before['public_proj_count'] + 1
        assert after_public['public_sample_count'] == before['public_sample_count'] + 2

        edit_proj_privacy(project, 'public', 'hidden_public')
        after_hidden_public = _latest_subset()
        assert after_hidden_public['public_proj_count'] == before['public_proj_count']
        assert after_hidden_public['public_sample_count'] == before['public_sample_count']
        assert after_hidden_public['all_private_proj_count'] == before['all_private_proj_count'] + 1
        assert after_hidden_public['all_private_sample_count'] == before['all_private_sample_count'] + 2

        edit_proj_privacy(project, 'hidden_public', 'private')
        assert _latest_subset() == after_hidden_public

        edit_proj_privacy(project, 'private', 'public')
        edit_proj_privacy(project, 'public', 'private')
        after_roundtrip = _latest_subset()
        assert after_roundtrip['all_private_proj_count'] == after_private_create['all_private_proj_count']
        assert after_roundtrip['all_private_sample_count'] == after_private_create['all_private_sample_count']
        assert after_roundtrip['public_proj_count'] == after_private_create['public_proj_count']
        assert after_roundtrip['public_sample_count'] == after_private_create['public_sample_count']
        assert _bucket_count(after_roundtrip, 'all_private_tissue_of_origin_count', 'brain') == (
            _bucket_count(before, 'all_private_tissue_of_origin_count', 'brain') + 2
        )
        assert _bucket_count(after_roundtrip, 'public_tissue_of_origin_count', 'brain') == (
            _bucket_count(before, 'public_tissue_of_origin_count', 'brain')
        )
        assert _count(after_roundtrip, 'all_private_amplicon_classifications', 'ecDNA') == (
            _count(before, 'all_private_amplicon_classifications', 'ecDNA') + 1
        )
        assert _count(after_roundtrip, 'public_amplicon_classifications', 'ecDNA') == (
            _count(before, 'public_amplicon_classifications', 'ecDNA')
        )
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_replacement_updates_sample_counts_without_double_counting_project():
    from caper.site_stats import (
        add_project_to_site_statistics,
        delete_project_from_site_statistics,
        get_latest_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    before = _latest_subset()
    old_project = _project([
        ('ecDNA', 'brain'),
    ], visibility='private')
    new_project = dict(old_project)
    new_project['runs'] = _project([
        ('ecDNA', 'brain'),
        ('BFB', 'brain'),
        ('Virus', 'lung'),
    ], visibility='private')['runs']

    try:
        add_project_to_site_statistics(old_project, is_private=True)
        after_create = _latest_subset()
        assert after_create['all_private_proj_count'] == before['all_private_proj_count'] + 1
        assert after_create['all_private_sample_count'] == before['all_private_sample_count'] + 1

        delete_project_from_site_statistics(old_project, is_private=True)
        add_project_to_site_statistics(new_project, is_private=True)
        after_replace = _latest_subset()

        assert after_replace['all_private_proj_count'] == before['all_private_proj_count'] + 1
        assert after_replace['all_private_sample_count'] == before['all_private_sample_count'] + 3
        assert _count(after_replace, 'all_private_amplicon_classifications', 'BFB') == (
            _count(before, 'all_private_amplicon_classifications', 'BFB') + 1
        )
        assert _count(after_replace, 'all_private_tissue_of_origin', 'lung') == (
            _count(before, 'all_private_tissue_of_origin', 'lung') + 1
        )
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_failed_replacement_rollback_restores_original_site_stats():
    from caper.site_stats import (
        add_project_to_site_statistics,
        delete_project_from_site_statistics,
        get_latest_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    before = _latest_subset()
    project = _project([
        ('ecDNA', 'brain'),
        ('Linear amplification', 'lung'),
    ], visibility='public')

    try:
        add_project_to_site_statistics(project, is_private=False)
        after_create = _latest_subset()

        delete_project_from_site_statistics(project, is_private=False)
        add_project_to_site_statistics(project, is_private=False)

        assert _latest_subset() == after_create
    finally:
        _cleanup_stats_since(start_id)
