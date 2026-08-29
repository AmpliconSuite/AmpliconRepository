"""Which fields belong to a project, and which belong to one of its versions.

A project document is two things wearing one shape: a *version* -- one
aggregation run, its samples, its tool versions -- and the *project* that owns
the version, with its name, its members, its visibility and its counters. When
a version is deleted and an older one promoted in its place,
``delete_project_version()`` has to decide which of the two the promoted
document inherits. Today it decides by a hand-written list of nine field names
written once and never extended; ``downloads`` is on it and ``project_downloads``
is not, which is the whole defect in miniature.

This module is the declaration that list should have been. It does not change
what promotion copies -- nothing reads these sets yet. What it does is make
"which level is this field?" a question with a written answer, so the next
field to appear cannot fall through in silence.

**The test that decides the level** is not taste and not the field's name: a
field is chain-level exactly when it must survive the deletion of every version.
Name, members, visibility, ownership and alias must. Sample data,
classifications, tool versions and payloads must not.

Four sets, because two were never enough:

``CHAIN_LEVEL``
    Belongs to the project. Survives the emptying of a chain, and is what the
    site displays for the project as a whole. Per-version values are not
    discarded -- ``project_downloads`` keeps its dict on every version -- they
    are simply not the number the project reports.

``VERSION_LEVEL``
    Belongs to one aggregation run. Never carried forward: the promoted version
    already has its own, and taking the deleted version's would be a lie about
    what the promoted one contains.

``LINEAGE``
    The version machinery's own fields -- position, state, pointers. Promotion
    rewrites these rather than copying or keeping them, which makes them a
    third category and not an awkward member of either other one.

``TRANSIENT``
    Upload scaffolding. Set when a placeholder document is inserted and
    ``$unset`` when the finished project replaces it. A completed project must
    not carry these at all, and ``clear_stale_uploads.py`` finds crashed uploads
    by looking for documents that still do.

## What the levels were measured against

Every name below was present on prod (``caper``, 311 documents) or dev
(``caper-dev``, 246 documents) when both were censused on 2026-08-29. The
adjudication of the six disputed fields was made on 2026-08-27 against a prod
measurement: 45 multi-version chains, 43 of them holding ``project_downloads``
on more than one version, and PCAWG's live version reporting 30 of the chain's
1,430 project downloads and 2,898 of its 114,310 sample downloads.

Two classifications came out against what the field's name suggests, and both
were settled by reading the writer and then counting:

``date_created`` is **version-level**. The name says it records when the
project was created; ``create_project()`` sets it to ``get_date()`` on every
version it writes, and on prod it differs across all 45 multi-version chains.
So do ``date``, ``update_date`` and ``created_at``.

``creator`` is **version-level**, while ``project_members`` is chain-level.
``creator`` is whoever ran *this* aggregation -- it varies across 12 of the 45
chains, because a project can be re-aggregated by someone other than the person
who first uploaded it. Ownership of the project is ``project_members``.

Variance across a chain is evidence and not proof, in one direction only. A
chain-level counter varies too, precisely because promotion has been dropping
it -- that is the ``project_downloads`` defect, not a refutation of its level.
What variance does settle is the other direction: a field that differs on every
member of every chain is carrying per-version information whatever its name is.
"""

CHAIN_LEVEL = frozenset({
    'project_name',
    'project_members',
    'subscribers',
    'private',
    'privateKey',
    'featured',
    'publication_link',
    'alias_name',
    'alias',
    'views',
    'downloads',
    'project_downloads',
    'sample_downloads',
})

VERSION_LEVEL = frozenset({
    # what this run produced
    'runs',
    'tarfile',
    'sample_data',
    'sample_count',
    'aggregate_df',
    'Classification',
    'Oncogenes',
    'reference_genome',
    'ecDNA_context',
    'features_list',
    'metadata_stored',
    'description',
    # who ran it, and when
    'creator',
    'date',
    'date_created',
    'created_at',
    'update_date',
    # what it was run with
    'AA_version',
    'AC_version',
    'ASP_version',
    'CoRAL_version',
    'Reconstruction_tools',
    'aggregator_version',
    'sample_name_remap_enabled',
    # how it turned out
    'FINISHED?',
    'aggregation_failed',
    'error_message',
    'rollback_project_id',
    # Version-level emptiness: whether *this* run produced any samples. Not the
    # chain-level emptiness of the target model, which is every member being a
    # tombstone and stays derived. Legacy and unreliable -- on prod 22 live
    # projects have no runs and 3 carry EMPTY? -- so read, do not extend.
    'EMPTY?',
    'empty',
})

LINEAGE = frozenset({
    '_id',
    'linkid',
    'delete',
    'current',
    'status',
    'is_latest',
    'version_chain_id',
    'version_ordinal',
    'previous_version_id',
    'next_version_id',
    'previous_versions',
    'previous_project_ids',
    'redirect_to_project',
    'version_deleted_from_history',
    'payload_purged',
    'delete_date',
    'delete_user',
})

TRANSIENT = frozenset({
    'owner',
    'original_project_name',
    'aggregation_in_progress',
})

ALL_LEVELS = {
    'chain': CHAIN_LEVEL,
    'version': VERSION_LEVEL,
    'lineage': LINEAGE,
    'transient': TRANSIENT,
}

DECLARED = CHAIN_LEVEL | VERSION_LEVEL | LINEAGE | TRANSIENT


def level_of(field):
    """``'chain'``, ``'version'``, ``'lineage'``, ``'transient'`` or ``None``.

    ``None`` means the field has never been adjudicated, which is a finding and
    not a default: it is what the guard test exists to catch.
    """
    for level, names in ALL_LEVELS.items():
        if field in names:
            return level
    return None


def unclassified_fields(doc):
    """The document's top-level field names that belong to no level, sorted."""
    return sorted(field for field in doc if field not in DECLARED)
