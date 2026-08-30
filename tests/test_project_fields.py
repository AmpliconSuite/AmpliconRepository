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
    ALL_LEVELS, CARRIED_ON_PROMOTION, CHAIN_LEVEL, DECLARED, KEPT_WHERE_EARNED,
    LINEAGE, TRANSIENT, VERSION_LEVEL, level_of, unclassified_fields,
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


def _promotion_source():
    """The statement in ``delete_project_version()`` that builds the copy set."""
    with open(VIEWS) as handle:
        source = handle.read()
    match = re.search(r'metadata_to_copy = (.*?)\n\n', source, re.S)
    assert match, 'could not find the promotion copy set in views.py'
    return match.group(1)


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


def test_promotion_reads_the_declared_set_rather_than_a_literal_list():
    """The list in views.py is the thing that went wrong; there must not be one.

    ``project_downloads`` was missing from every promotion for as long as the
    feature existed, because the fields to carry were written out by hand in
    ``delete_project_version()`` and nobody edited that list when the field was
    added. Classifying the field correctly does not stop that recurring; only
    removing the second place to remember does.
    """
    statement = _promotion_source()

    assert 'project_fields.CARRIED_ON_PROMOTION' in statement, statement
    assert "'" not in statement and '"' not in statement, (
        f'promotion is naming fields by hand again: {statement}')


def test_everything_promotion_carries_is_chain_level():
    misfiled = {name: level_of(name) for name in CARRIED_ON_PROMOTION
                if level_of(name) != 'chain'}
    assert not misfiled, misfiled


def test_the_download_counters_are_kept_where_they_were_earned():
    """Chain-level and carried-on-promotion are not the same set, on purpose.

    A chain-level field must survive the emptying of a chain. It does not
    follow that it must live on the head. Each of these is per version, starts
    at zero, and is never carried forward, so the project's number is the sum
    across the chain -- and a version that is deleted keeps its share on its
    tombstone, which is why the total does not shrink. Copying one onto the
    promoted head as well would report the same downloads twice.
    """
    assert KEPT_WHERE_EARNED == {'project_downloads', 'sample_downloads',
                                 'downloads'}
    assert KEPT_WHERE_EARNED < CHAIN_LEVEL
    assert CARRIED_ON_PROMOTION | KEPT_WHERE_EARNED == CHAIN_LEVEL
    assert CARRIED_ON_PROMOTION.isdisjoint(KEPT_WHERE_EARNED)


def test_views_is_still_carried_and_the_declaration_says_why():
    """The counter that has not been fixed, named as such rather than left out.

    There has never been a per-date record of views, and reaggregation seeds a
    new version's count from its predecessor, so the values overlap by an
    unknown amount and cannot be summed. Until a per-version count is started,
    views is read off the head and carried on promotion -- which is what the
    site has always done.
    """
    assert 'views' in CARRIED_ON_PROMOTION
    assert 'views' not in KEPT_WHERE_EARNED
    assert level_of('views') == 'chain'


def test_what_the_old_hand_written_list_got_wrong_in_both_directions():
    """The nine names promotion used to copy, against what it copies now.

    Kept as an explicit list because it is the before-and-after of this change,
    and because a set difference that quietly became empty would look like a
    passing test. It got the question wrong in both directions: two fields that
    belong to the project were missing, and one that is per version was being
    copied.
    """
    old_hand_written_list = {
        'project_members', 'subscribers', 'views', 'downloads', 'alias_name',
        'publication_link', 'private', 'privateKey', 'featured',
    }

    # Chain-level and forgotten: deleting a renamed version used to revert the
    # project's name.
    assert CARRIED_ON_PROMOTION - old_hand_written_list == {'project_name',
                                                            'alias'}
    # Per version and copied anyway, which is what made the total unsummable.
    assert old_hand_written_list - CARRIED_ON_PROMOTION == {'downloads'}


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
