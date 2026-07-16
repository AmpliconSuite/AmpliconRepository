import copy

import pytest
from bson.objectid import ObjectId


def _stat_subset(stats):
    subset = {}
    for prefix in ('public', 'all_private', 'hidden_public'):
        subset[f'{prefix}_proj_count'] = stats.get(f'{prefix}_proj_count', 0)
        subset[f'{prefix}_sample_count'] = stats.get(f'{prefix}_sample_count', 0)
        subset[f'{prefix}_amplicon_classifications_count'] = copy.deepcopy(
            stats.get(f'{prefix}_amplicon_classifications_count', {})
        )
        subset[f'{prefix}_tissue_of_origin_count'] = copy.deepcopy(
            stats.get(f'{prefix}_tissue_of_origin_count', {})
        )
    return subset


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
        add_project_to_site_statistics(project, 'private')
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

        delete_project_from_site_statistics(project, 'private')
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
        add_project_to_site_statistics(project, 'public')
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

        delete_project_from_site_statistics(project, 'public')
        assert _latest_subset() == before
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_visibility_transitions_move_stats_between_buckets():
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
        add_project_to_site_statistics(project, 'private')
        after_private_create = _latest_subset()

        edit_proj_privacy(project, 'private', 'public')
        after_public = _latest_subset()
        assert after_public['all_private_proj_count'] == before['all_private_proj_count']
        assert after_public['all_private_sample_count'] == before['all_private_sample_count']
        assert after_public['public_proj_count'] == before['public_proj_count'] + 1
        assert after_public['public_sample_count'] == before['public_sample_count'] + 2

        # Unlisted projects are counted on their own, not with the private ones.
        edit_proj_privacy(project, 'public', 'hidden_public')
        after_hidden_public = _latest_subset()
        assert after_hidden_public['public_proj_count'] == before['public_proj_count']
        assert after_hidden_public['public_sample_count'] == before['public_sample_count']
        assert after_hidden_public['all_private_proj_count'] == before['all_private_proj_count']
        assert after_hidden_public['all_private_sample_count'] == before['all_private_sample_count']
        assert after_hidden_public['hidden_public_proj_count'] == before['hidden_public_proj_count'] + 1
        assert after_hidden_public['hidden_public_sample_count'] == before['hidden_public_sample_count'] + 2

        edit_proj_privacy(project, 'hidden_public', 'private')
        assert _latest_subset() == after_private_create

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
def test_regenerate_counts_unlisted_projects_in_their_own_bucket():
    """Unlisted (hidden_public) projects get their own mutually exclusive bucket: they must not
    land in the public or private counts, which is what the admin stats table columns rely on."""
    from caper.utils import collection_handle
    from caper.site_stats import get_latest_site_statistics, regenerate_site_statistics

    start_id = get_latest_site_statistics()['_id']
    unlisted = _project([('ecDNA', 'brain'), ('BFB', 'lung')], visibility='hidden_public')

    try:
        before = _stat_subset(regenerate_site_statistics())
        collection_handle.insert_one(unlisted)
        after = _stat_subset(regenerate_site_statistics())

        assert after['hidden_public_proj_count'] == before['hidden_public_proj_count'] + 1
        assert after['hidden_public_sample_count'] == before['hidden_public_sample_count'] + 2
        assert _count(after, 'hidden_public_amplicon_classifications', 'ecDNA') == (
            _count(before, 'hidden_public_amplicon_classifications', 'ecDNA') + 1
        )
        assert _bucket_count(after, 'hidden_public_tissue_of_origin_count', 'brain') == (
            _bucket_count(before, 'hidden_public_tissue_of_origin_count', 'brain') + 1
        )

        # The other two buckets must not see it at all.
        for key in ('public_proj_count', 'public_sample_count',
                    'all_private_proj_count', 'all_private_sample_count',
                    'public_amplicon_classifications_count', 'all_private_amplicon_classifications_count',
                    'public_tissue_of_origin_count', 'all_private_tissue_of_origin_count'):
            assert after[key] == before[key]
    finally:
        collection_handle.delete_one({'_id': unlisted['_id']})
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_regenerate_matches_incremental_stats_for_every_visibility():
    """The running totals and a from-scratch regenerate must agree, whichever bucket a project
    is in -- disagreement between them is what issue #592 reported."""
    from caper.utils import collection_handle
    from caper.site_stats import (
        add_project_to_site_statistics,
        get_latest_site_statistics,
        regenerate_site_statistics,
    )

    start_id = get_latest_site_statistics()['_id']
    projects = [
        _project([('ecDNA', 'brain')], visibility='public'),
        _project([('BFB', 'lung')], visibility='private'),
        _project([('Virus', 'skin')], visibility='hidden_public'),
    ]

    try:
        regenerate_site_statistics()
        for project in projects:
            collection_handle.insert_one(project)
            add_project_to_site_statistics(project, project['private'])
        incremental = _latest_subset()

        # Both sides are read back through get_latest_site_statistics(), which is what the pages
        # render: it derives the otherfscna display key that regenerate does not store itself.
        regenerate_site_statistics()
        assert _latest_subset() == incremental
    finally:
        for project in projects:
            collection_handle.delete_one({'_id': project['_id']})
        _cleanup_stats_since(start_id)


def _upload_placeholder(visibility='public', failed=False):
    """A project document as views.py leaves it when an upload is still aggregating
    (aggregation_in_progress) or aggregation failed. Both stay current/undeleted."""
    placeholder = {
        '_id': ObjectId(),
        'project_name': 'StatsGhostProject (Processing...)',
        'original_project_name': 'StatsGhostProject',
        'private': visibility,
        'sample_count': 0,
        'runs': {},
        'delete': False,
        'current': True,
    }
    if failed:
        placeholder.update({'FINISHED?': True, 'aggregation_failed': True})
    else:
        placeholder.update({'FINISHED?': False, 'aggregation_in_progress': True})
    return placeholder


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.parametrize('visibility,proj_key', [
    ('public', 'public_proj_count'),
    ('private', 'all_private_proj_count'),
])
@pytest.mark.parametrize('failed', [False, True], ids=['in_progress', 'aggregation_failed'])
def test_regenerate_excludes_failed_and_in_progress_uploads(visibility, proj_key, failed):
    """Regression test for issue #592.

    Upload placeholders and failed aggregations are never added by the incremental
    stats functions, so a regenerate must not count them either. Because they carry
    runs={}, counting one inflated the project count by 1 while leaving the sample
    count untouched -- the symptom originally reported against unlisted projects.
    """
    from caper.utils import collection_handle
    from caper.site_stats import get_latest_site_statistics, regenerate_site_statistics

    start_id = get_latest_site_statistics()['_id']
    ghost = _upload_placeholder(visibility=visibility, failed=failed)

    try:
        before = _stat_subset(regenerate_site_statistics())
        collection_handle.insert_one(ghost)
        after = _stat_subset(regenerate_site_statistics())

        assert after[proj_key] == before[proj_key]
        assert after == before
    finally:
        collection_handle.delete_one({'_id': ghost['_id']})
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_regenerate_still_counts_projects_extracting_files():
    """A real project is inserted with FINISHED? False while its files extract, and the
    incremental path counts it at insert time. Regeneration must agree, so the
    placeholder filter must not key off FINISHED?."""
    from caper.utils import collection_handle
    from caper.site_stats import get_latest_site_statistics, regenerate_site_statistics

    start_id = get_latest_site_statistics()['_id']
    extracting = _project([('ecDNA', 'brain')], visibility='public')
    extracting['FINISHED?'] = False

    try:
        before = _stat_subset(regenerate_site_statistics())
        collection_handle.insert_one(extracting)
        after = _stat_subset(regenerate_site_statistics())

        assert after['public_proj_count'] == before['public_proj_count'] + 1
        assert after['public_sample_count'] == before['public_sample_count'] + 1
    finally:
        collection_handle.delete_one({'_id': extracting['_id']})
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
        add_project_to_site_statistics(old_project, 'private')
        after_create = _latest_subset()
        assert after_create['all_private_proj_count'] == before['all_private_proj_count'] + 1
        assert after_create['all_private_sample_count'] == before['all_private_sample_count'] + 1

        delete_project_from_site_statistics(old_project, 'private')
        add_project_to_site_statistics(new_project, 'private')
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
        add_project_to_site_statistics(project, 'public')
        after_create = _latest_subset()

        delete_project_from_site_statistics(project, 'public')
        add_project_to_site_statistics(project, 'public')

        assert _latest_subset() == after_create
    finally:
        _cleanup_stats_since(start_id)


def _legacy_stats_document():
    """A statistics snapshot in the shape written before the unlisted bucket existed."""
    return {
        'public_proj_count': 2,
        'public_sample_count': 5,
        'public_coral_project_count': 0,
        'public_coral_sample_count': 0,
        'public_amplicon_classifications_count': {'ecDNA': 3},
        'public_tissue_of_origin_count': {'lung': 2},
        'all_private_proj_count': 1,
        'all_private_sample_count': 4,
        'all_private_coral_project_count': 0,
        'all_private_coral_sample_count': 0,
        'all_private_amplicon_classifications_count': {'BFB': 1},
        'all_private_tissue_of_origin_count': {'brain': 1},
        'date': '2026-01-01T00:00:00.000000',
    }


@pytest.mark.slow
@pytest.mark.integration
def test_latest_statistics_fills_in_buckets_missing_from_older_documents():
    from caper.site_stats import get_latest_site_statistics, site_statistics_handle

    start_id = get_latest_site_statistics()['_id']
    try:
        site_statistics_handle.insert_one(_legacy_stats_document())
        latest = get_latest_site_statistics()

        assert latest['hidden_public_proj_count'] == 0
        assert latest['hidden_public_sample_count'] == 0
        assert latest['hidden_public_coral_project_count'] == 0
        assert latest['hidden_public_coral_sample_count'] == 0
        assert latest['hidden_public_amplicon_classifications_count'] == {}
        assert latest['hidden_public_tissue_of_origin_count'] == {}
        # buckets the older document did carry stay exactly as stored
        assert latest['public_proj_count'] == 2
        assert latest['all_private_tissue_of_origin_count'] == {'brain': 1}
    finally:
        _cleanup_stats_since(start_id)


@pytest.mark.slow
@pytest.mark.integration
def test_admin_stats_table_renders_from_a_document_written_before_the_unlisted_bucket(test_user):
    """A missing bucket key reaches a template as '', not as a number or dict.

    That took the whole admin stats page down with an AttributeError from the get_item filter
    on every server whose newest statistics document predated the unlisted bucket.
    """
    from django.template.loader import render_to_string
    from django.test import RequestFactory

    from caper.site_stats import get_latest_site_statistics, site_statistics_handle

    # the page's own context processors need a request; the stats table itself only reads site_stats
    request = RequestFactory().get('/admin-stats/')
    request.user = test_user

    start_id = get_latest_site_statistics()['_id']
    try:
        site_statistics_handle.insert_one(_legacy_stats_document())
        html = render_to_string('pages/admin_stats.html',
                                {'site_stats': get_latest_site_statistics()},
                                request=request)

        assert 'Unlisted public' in html
    finally:
        _cleanup_stats_since(start_id)
