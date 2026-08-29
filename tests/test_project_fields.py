"""The chain-level / version-level split, and the guard that keeps it complete.

The defect this exists to stop is not a wrong classification. It is a field
appearing on project documents and belonging to no classification at all --
which is how ``project_downloads`` came to sit next to ``downloads`` with only
one of the two carried forward by promotion.
"""
import os
import re

import pytest

from caper.project_fields import (
    ALL_LEVELS, CHAIN_LEVEL, DECLARED, LINEAGE, TRANSIENT, VERSION_LEVEL,
    level_of, unclassified_fields,
)

VIEWS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'caper', 'caper', 'views.py')

# Every top-level field name found on a project document when prod (caper, 311
# documents) and dev (caper-dev, 246 documents) were censused on 2026-08-29.
# The union of the two, so a field only one deployment has still counts.
#
# This list is a measurement, not a schema. It is here so that a field which
# exists in production but which no test creates cannot quietly go
# unclassified: the code that wrote it may be long gone, but the data is still
# there and promotion still has to decide what to do with it.
MEASURED_2026_08_29 = frozenset({
    '_id', 'AA_version', 'AC_version', 'ASP_version', 'Classification',
    'CoRAL_version', 'EMPTY?', 'FINISHED?', 'Oncogenes',
    'Reconstruction_tools', 'aggregate_df', 'aggregation_failed',
    'aggregation_in_progress', 'aggregator_version', 'alias', 'alias_name',
    'created_at', 'creator', 'current', 'date', 'date_created', 'delete',
    'delete_date', 'delete_user', 'description', 'downloads', 'ecDNA_context',
    'empty', 'error_message', 'features_list', 'featured', 'is_latest',
    'linkid', 'metadata_stored', 'next_version_id', 'original_project_name',
    'owner', 'payload_purged', 'previous_project_ids', 'previous_version_id',
    'previous_versions', 'private', 'privateKey', 'project_downloads',
    'project_members', 'project_name', 'publication_link', 'redirect_to_project',
    'reference_genome', 'rollback_project_id', 'runs', 'sample_count',
    'sample_data', 'sample_downloads', 'sample_name_remap_enabled', 'status',
    'subscribers', 'tarfile', 'update_date', 'version_chain_id',
    'version_deleted_from_history', 'version_ordinal', 'views',
})


def _promotion_copy_list():
    """The field names ``delete_project_version()`` copies onto the promotion.

    Read out of the source rather than duplicated here, so that adding a name
    to that list without classifying it fails this file instead of shipping.
    """
    with open(VIEWS) as handle:
        source = handle.read()
    match = re.search(r'metadata_to_copy = \{\}\s*\n\s*for field in \[(.*?)\]:',
                      source, re.S)
    assert match, 'could not find the promotion copy list in views.py'
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


def test_the_levels_are_disjoint():
    seen = {}
    for level, names in ALL_LEVELS.items():
        for name in names:
            assert name not in seen, (
                f'{name!r} is declared both {seen[name]} and {level}')
            seen[name] = level


def test_every_field_measured_on_prod_or_dev_is_classified():
    assert not (MEASURED_2026_08_29 - DECLARED), (
        'measured on a real deployment and classified nowhere: '
        f'{sorted(MEASURED_2026_08_29 - DECLARED)}')


def test_nothing_is_declared_that_was_never_seen():
    """A set that grows by guesswork is the thing this module replaces."""
    invented = DECLARED - MEASURED_2026_08_29
    assert not invented, (
        f'declared but present on neither deployment on 2026-08-29: {sorted(invented)}. '
        'Either the census is stale and should be re-run, or the name is wrong.')


def test_promotion_only_copies_chain_level_fields():
    copied = _promotion_copy_list()
    assert copied, 'the promotion copy list parsed as empty'
    misfiled = {name: level_of(name) for name in copied
                if level_of(name) != 'chain'}
    assert not misfiled, (
        f'delete_project_version() copies these forward, but they are not '
        f'declared chain-level: {misfiled}')


def test_the_2026_08_27_adjudication_is_what_is_encoded():
    """The six fields D12 disputed, decided against prod and written down here.

    Kept as an explicit test rather than left implicit in the sets, because the
    six are the ones somebody will be tempted to move.
    """
    assert level_of('project_downloads') == 'chain'
    assert level_of('sample_downloads') == 'chain'
    assert level_of('alias') == 'chain'
    assert level_of('alias_name') == 'chain'
    assert level_of('sample_name_remap_enabled') == 'version'
    # Neither: upload scaffolding, $unset when the finished project replaces
    # the placeholder. clear_stale_uploads.py finds crashed uploads by 'owner'.
    assert level_of('owner') == 'transient'
    assert level_of('original_project_name') == 'transient'


def test_the_counter_pairs_are_on_the_same_level():
    """``downloads`` copied and ``project_downloads`` not is the original bug."""
    assert level_of('downloads') == level_of('project_downloads') == 'chain'
    assert level_of('views') == 'chain'
    assert level_of('sample_downloads') == 'chain'


def test_an_unknown_field_is_a_finding_not_a_default():
    assert level_of('a_field_nobody_declared') is None
    assert unclassified_fields({'project_name': 'x', 'zzz_new_field': 1}) == \
        ['zzz_new_field']
    assert unclassified_fields({name: 1 for name in CHAIN_LEVEL}) == []


def test_lineage_and_transient_are_not_carried_or_kept():
    """Both exist so neither has to be mislabelled as the other kind."""
    assert level_of('version_chain_id') == 'lineage'
    assert level_of('is_latest') == 'lineage'
    assert level_of('status') == 'lineage'
    assert LINEAGE.isdisjoint(CHAIN_LEVEL | VERSION_LEVEL)
    assert TRANSIENT.isdisjoint(CHAIN_LEVEL | VERSION_LEVEL)


@pytest.mark.slow
def test_a_project_created_through_the_app_has_no_unclassified_field(
        loaded_datasets, mongo_collection):
    """The half of the guard that watches the code, not the old data.

    ``MEASURED_2026_08_29`` catches a legacy field. This catches the next one:
    a field the upload path starts writing tomorrow, on a document this suite
    creates for real through ``create_project()``.
    """
    from bson.objectid import ObjectId

    for key in ('project_small', 'project_medium'):
        doc = mongo_collection.find_one(
            {'_id': ObjectId(loaded_datasets[key])})
        assert doc, f'{key} was not found'
        assert unclassified_fields(doc) == [], (
            f'{key} carries fields that belong to no level: '
            f'{unclassified_fields(doc)}')
        for name in TRANSIENT:
            assert name not in doc, (
                f'{key} is a finished project and still carries the upload '
                f'placeholder field {name!r}')
