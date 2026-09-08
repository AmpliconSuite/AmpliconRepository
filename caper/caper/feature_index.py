"""A queryable copy of what search reads, so a search stops reading every project.

## Why this exists

``perform_search`` issues ``list(collection_handle.find(query))`` with no
projection, pulls every LIVE project document in full, and filters the result
with pandas.  Measured on prod on 2026-09-07, over 33 public LIVE projects and
37,795 feature rows: 61.5 MiB fetched and 2.73 s in the fetch alone, and the
total is the same whatever was asked for -- MYC 3.74 s (365 rows), ecDNA 4.28 s
(4,334 rows), a gene that does not exist 4.39 s (0 rows).  A query that matches
nothing costs more than one that matches thousands, because the cost is the
scan, not the match.

No index can fix that in place.  ``runs`` is a dict keyed by an arbitrary run id
(``sample_365``), so every searchable value sits at
``runs.<arbitrary-key>[].All_genes``.  MongoDB can index
``runs.sample_365.All_genes``; it cannot index ``runs.*.All_genes``.  The shape
of the document, not the query, is what forbids the index.

So the searchable values are copied out into flat rows that *can* be indexed.
On dev on 2026-09-07 a scratch collection of 30,597 such rows from 52 projects
served a page of 50 in 3.1-6.5 ms against ~4,000 ms today.

## This is derived data, and that is the whole safety argument

Every field here is a copy of something in a project document.  Nothing is
authored into this collection and nothing reads it as the truth about a
project.  A drift bug is therefore a *rebuild*, never a loss: drop the
collection and ``rebuild_feature_index()`` reconstructs it from ``projects``.
That is why the sync design below can afford to be simple.

## Keeping it in step with ``projects``

Two prongs, because either alone has a known failure mode:

1. ``index_project`` / ``unindex_project`` run when a project is written, which
   keeps the index current without a delay a user would notice.
2. ``feature_index_drift()`` recomputes each project's digest and reports what
   disagrees.  It is the standing falsifying measurement for "the index is
   current", and it is what makes prong 1 allowed to have gaps.

Prong 2 is not belt-and-braces.  A project document is written from 50 call
sites across 30 functions in this codebase (``views.py`` 40 of them, 8 inside
``_do_rollback`` alone), plus standalone scripts that connect to the database
directly and will never call a hook.  An approach that depends on every one of
those sites being found and kept correct is an approach that fails silently the
first time someone adds the 51st.  ``site_statistics`` is the local precedent
and it already carries a regenerate button for exactly this reason.

That denormalized copies drift *here specifically* is measured, not assumed:
``sample_data`` has 100% coverage on prod and 331 real content divergences from
the ``runs`` it was derived from (measured 2026-09-07).

## Reference builds

``reference_build`` is stored per row and normalised through ``ref_equivalence``
in ``coamp_graph``: GRCh37 -> hg19, GRCh38 and GRCh38_viral -> hg38.  It is
stored because gene symbols are build-dependent and the corpus contains both
vocabularies.  Measured on prod 2026-09-07, over 33 public LIVE projects: no
single project mixes spellings (7 hg19-era, 17 hg38-era, 9 with neither), but
the corpus holds both, so one gene reaches users under two names --
``C17ORF37`` on 283 feature rows and ``MIEN1``, the same gene, on 160;
``MYCL1`` on 59 and ``MYCL`` on 56.

There is deliberately no alias map.  Reconciling symbol histories is the
caller's to do, and a half-maintained mapping would be worse than none.  What
this module owes them instead is the evidence that a fence exists: the build is
on every row, so a result set can be faceted by it and a query that returns
hg38 rows only can be *seen* to have returned hg38 rows only.
"""

import datetime
import hashlib
import json

from .project_status import LIVE, status_query
from .utils import (
    collection_handle,
    db_handle_primary,
    get_collection_handle,
)
from .search import METADATA_COLUMN_FOR_KEY
from .visibility import (
    PUBLIC_QUERY_VALUES,
    RESTRICTED_QUERY_VALUES,
    normalize_visibility_field,
)

# Bump when feature_rows_for_project changes what it emits.  The version is
# folded into every digest, so a builder change invalidates every stored row
# and the drift check reports the whole corpus as stale -- which is correct: it
# is stale, against the new builder.
SCHEMA_VERSION = 3

FEATURE_INDEX_COLLECTION = 'feature_index'
GENE_CATALOG_COLLECTION = 'gene_catalog'
SEARCH_NAMES_COLLECTION = 'search_names'

feature_index_handle = get_collection_handle(db_handle_primary, FEATURE_INDEX_COLLECTION)
gene_catalog_handle = get_collection_handle(db_handle_primary, GENE_CATALOG_COLLECTION)
search_names_handle = get_collection_handle(db_handle_primary, SEARCH_NAMES_COLLECTION)

# Normalised reference builds.  Same mapping as coamp_graph.preprocess_dataset,
# which is the only other place that has to decide whether two samples are
# comparable.  Unknown values are kept verbatim rather than dropped: a build we
# have not seen is still worth being able to facet on, and silently mapping it
# to one of these two would be a claim we cannot support.
REFERENCE_EQUIVALENCE = {
    'hg19': 'hg19',
    'GRCh37': 'hg19',
    'hg38': 'hg38',
    'GRCh38': 'hg38',
    'GRCh38_viral': 'hg38',
}

# Row fields lifted straight off a feature.  Keys are read after
# replace_space_to_underscore has run, so they are the underscored spellings.
_SAMPLE_METADATA_FIELDS = ('Sample_type', 'Cancer_type', 'Tissue_of_origin')

# Classifications that mean "this sample was analysed and carries no focal
# amplification", as opposed to a real amplicon.  Mirrors _zero_feature_mask in
# search.py: 'No FSCNA' comes from AmpliconClassifier, 'NA' is the
# AmpliconSuiteAggregator convention.  Kept as a set of upper-case spellings so
# the comparison cannot be defeated by case.
NO_AMPLICON_CLASSIFICATIONS = frozenset({'NO FSCNA', 'NA'})


def normalize_gene(value):
    """One canonical spelling for a gene symbol.

    ``All_genes`` is stored as the repr of a Python list in some documents and
    as a real list in others, so members arrive carrying stray quote characters.
    Upper-casing is safe because refGene symbols are upper-case by convention
    and the existing search already compares upper-case.
    """
    return str(value).replace("'", '').replace('"', '').strip().upper()


def normalize_reference(value):
    """Normalise a Reference_version to hg19/hg38, or keep it as given."""
    text = str(value or '').strip()
    if not text:
        return ''
    return REFERENCE_EQUIVALENCE.get(text, text)


def _gene_list(feature, key='All_genes'):
    """Genes on one feature, upper-cased, deduplicated, order preserved.

    This is the array that gets indexed and matched against. Upper-casing is
    what makes an equality match case-insensitive without a regex, and it
    agrees with the existing search, which compares ``genequery.upper()``
    against ``[g.upper() for g in All_genes]``.

    It is emphatically **not** the array to display. Measured on caper-dev
    2026-09-07 over 30,597 feature rows and 325,264 gene mentions, **11,986
    mentions across 1,083 distinct symbols are not upper-case** -- the open
    reading frame names, whose canonical refGene spelling is mixed case
    (``C17orf37``, 514 mentions; ``C19orf2``, 326). Rendering those upper-cased
    would show a symbol that is not the gene's name. See ``_gene_display_list``.
    """
    seen = {}
    for gene in feature.get(key) or []:
        symbol = normalize_gene(gene)
        if symbol:
            seen.setdefault(symbol, None)
    return list(seen)


def _gene_display_list(feature, key='All_genes'):
    """Genes on one feature exactly as a search result reports them today.

    Quote characters stripped and whitespace trimmed, case preserved, and
    **not** deduplicated -- which is precisely what
    ``get_samples_from_features`` returns:
    ``[i.replace("'", "").strip() for i in sample_dict['All_genes']]``.
    Deduplicating here would be an improvement, and an improvement is a
    difference: this array exists so that a result served from the index is
    indistinguishable from one served the old way.
    """
    return [str(gene).replace("'", '').strip()
            for gene in feature.get(key) or []]


def _oncogene_list(feature):
    """Oncogenes on one feature, upper-cased the same way as ``genes``.

    Not indexed: oncogene status is a property of the symbol, not of the row,
    so it lives in the gene catalog. Measured on prod 2026-09-07, 0 of 1,025
    distinct oncogenes appear outside that row's All_genes, which is what
    AmpliconClassifier building both columns from one sorted gene list would
    predict. Stored anyway because it is what the result rows report.
    """
    return _gene_list(feature, 'Oncogenes')


def _location_list(feature):
    """Raw Location strings, untouched.

    Not parsed here.  Region search needs an interval tree keyed by reference
    build and is a separate piece of work; storing the strings now means that
    work can be done against the index rather than against ``runs``.  Some rows
    carry a sentinel Location standing in for a missing feature BED file, so a
    parser must expect entries it cannot read.
    """
    location = feature.get('Location')
    if location is None:
        return []
    if isinstance(location, (list, tuple)):
        return [str(item) for item in location]
    return [str(location)]


def _metadata_key_map(runs):
    """Lower-cased metadata key -> the spelling actually stored, per project.

    Rows normally share one sheet's columns, but a project whose metadata was
    uploaded more than once holds the union of them, so every row is consulted
    and the first spelling seen wins. Mirrors ``search._metadata_key_map``,
    including that tie-break: picking the other spelling would lift a different
    column and the two paths would disagree about a sample's cancer type.
    """
    key_map = {}
    for features in (runs or {}).values():
        for feature in features or []:
            if not isinstance(feature, dict):
                continue
            for key in feature.get('extra_metadata_from_csv') or {}:
                key_map.setdefault(str(key).lower(), key)
    return key_map


def _clean_metadata_value(value):
    """A metadata cell as a stripped string, with blanks and NaN as ''."""
    if value is None:
        return ''
    if isinstance(value, float) and value != value:  # NaN
        return ''
    return str(value).strip()


def _lift_metadata(feature, key_map, fallback):
    """The three dedicated metadata fields, uploaded sheet taking precedence.

    A run row normally carries Sample_type, Cancer_type and Tissue_of_origin
    already, denormalised at upload. Not always: a project re-uploaded without a
    fresh metadata sheet before mid-2026 carried ``extra_metadata_from_csv``
    forward without rewriting them, so the row holds the metadata and no
    Cancer_type at all. ``add_extra_metadata`` lifts the values back out at
    search time, and the index has to do the same or a cancer-type search stops
    agreeing with the project page.

    The sheet **overwrites** the row's own value wherever the sheet has a
    non-blank one -- it is not a fallback for blanks. That is what
    ``df.loc[values.index, column] = values`` does, and it was worth reading
    twice: 3 of the dev corpus's projects differ on this, and the index
    returned '' for samples the old path reports as 'Adenocarcinoma'.
    """
    metadata = feature.get('extra_metadata_from_csv') or {}
    lifted = {}
    for lower, column in METADATA_COLUMN_FOR_KEY.items():
        value = ''
        stored_key = key_map.get(lower)
        if stored_key is not None:
            value = _clean_metadata_value(metadata.get(stored_key))
        lifted[column] = value or fallback(column)
    return lifted


def _sample_metadata_lookup(project):
    """Sample name -> that sample's cached metadata row.

    Zero-feature samples have no feature to carry Sample_type and friends, so
    they inherit them from ``sample_data`` exactly as get_samples_from_features
    does when it builds its placeholders.
    """
    lookup = {}
    for row in project.get('sample_data') or []:
        name = row.get('Sample_name')
        if name:
            lookup[name] = row
    return lookup


def feature_rows_for_project(project):
    """Every indexable row for one project document.  Pure; no database access.

    One row per feature, plus one row per zero-feature sample.  The zero-feature
    rows are not padding: a sample that was analysed and found to carry no focal
    amplification is a result, and the search page has a checkbox for it.  They
    are marked with ``has_amplicon: False`` rather than by an absent field, so a
    query can ask for them or exclude them without relying on a field's absence
    -- which this codebase has been bitten by before.
    """
    project_id = project.get('_id')
    project_name = project.get('project_name')
    # Normalised on the way in, so the derived data holds one encoding rather
    # than the two the project documents hold. 'private' is not a boolean --
    # it is a three-valued string, with a handful of pre-change documents
    # holding a boolean instead -- and every bug that field has caused came
    # from reading it raw.
    visibility = normalize_visibility_field(project.get('private'))
    members = list(project.get('project_members') or [])
    runs = project.get('runs') or {}
    if not isinstance(runs, dict):
        return []

    cached_metadata = _sample_metadata_lookup(project)
    # len(runs), carried on every row. The search results table reports a
    # project's sample count, and the stored 'sample_count' field cannot supply
    # it: measured on caper-dev 2026-09-07, of 52 LIVE projects one has no
    # sample_count at all and three disagree with len(runs) (9 vs 8, 1 vs 0,
    # 4 vs 2). Counting the run keys here is exact by construction, and putting
    # it on the row is what lets the results page report it without reading
    # 'runs' back out of the project document -- which is the whole cost this
    # index exists to avoid.
    project_sample_count = len(runs)
    metadata_keys = _metadata_key_map(runs)
    rows = []

    for run_key, features in runs.items():
        if not features:
            # An empty list is a sample AmpliconClassifier found nothing focal
            # in.  The run key is the sample name here; there is no feature to
            # take one from.
            cached = cached_metadata.get(run_key, {})
            rows.append(_row(
                project_id=project_id,
                project_name=project_name,
                project_sample_count=project_sample_count,
                visibility=visibility,
                members=members,
                run_key=run_key,
                sample_name=run_key,
                feature_id='',
                classification='NA',
                genes=[],
                genes_display=[],
                oncogenes=[],
                oncogenes_display=[],
                locations=[],
                reference_build='',
                metadata={field: str(cached.get(field, '') or '').strip()
                          for field in _SAMPLE_METADATA_FIELDS},
                extra_metadata=cached.get('extra_metadata_from_csv') or {},
                has_amplicon=False,
            ))
            continue

        for feature in features:
            if not isinstance(feature, dict):
                continue
            # Keys are read in both spellings: the site underscores them before
            # searching, but the stored documents are not all written that way.
            get = lambda *names: next(
                (feature[name] for name in names if feature.get(name) not in (None, '')), '')
            classification = str(get('Classification') or '')
            rows.append(_row(
                project_id=project_id,
                project_name=project_name,
                project_sample_count=project_sample_count,
                visibility=visibility,
                members=members,
                run_key=run_key,
                sample_name=str(get('Sample_name', 'Sample name') or run_key),
                feature_id=str(get('Feature_ID', 'Feature ID') or ''),
                classification=classification,
                genes=_gene_list(feature),
                genes_display=_gene_display_list(feature),
                oncogenes=_oncogene_list(feature),
                oncogenes_display=_gene_display_list(feature, 'Oncogenes'),
                locations=_location_list(feature),
                reference_build=normalize_reference(get('Reference_version', 'Reference version')),
                # Stripped, because that is how the values are displayed and
                # how the existing filters compare them: _term_mask() calls
                # .str.strip() before matching, so a stored 'Lung ' has to be
                # findable by searching for 'Lung'.
                metadata=_lift_metadata(
                    feature, metadata_keys,
                    lambda column: str(get(column, column.replace('_', ' ')) or '').strip()),
                extra_metadata=feature.get('extra_metadata_from_csv') or {},
                has_amplicon=classification.strip().upper() not in NO_AMPLICON_CLASSIFICATIONS
                and bool(str(get('Feature_ID', 'Feature ID') or '')),
            ))

    return rows


def _row(*, project_id, project_name, project_sample_count, visibility, members, run_key, sample_name,
         feature_id, classification, genes, genes_display, oncogenes,
         oncogenes_display, locations, reference_build, metadata, extra_metadata,
         has_amplicon):
    """Assemble one index document.

    ``sample_name_lower`` and ``project_name_lower`` are stored rather than
    derived at query time because DocumentDB will not use an index for a
    case-insensitive match, and will not use one for an anchored prefix regex
    either -- ``/^MY/`` was measured as a COLLSCAN on dev on 2026-09-07, where
    MongoDB would have used the index.  Substring name search is therefore not
    served from this collection at all; it is served from ``search_names``,
    which is small enough to scan.
    """
    return {
        'project_id': project_id,
        'project_name': project_name,
        'project_name_lower': str(project_name or '').lower(),
        'project_sample_count': project_sample_count,
        'visibility': visibility,
        'project_members': members,
        'run_key': run_key,
        'sample_name': sample_name,
        'sample_name_lower': str(sample_name or '').lower(),
        'feature_id': feature_id,
        'classification': classification,
        'has_amplicon': has_amplicon,
        # Two arrays for the same genes, and the split is load-bearing: 'genes'
        # is upper-cased so an equality match is case-insensitive and indexable,
        # '*_display' is what a result reports, because 1,083 symbols are not
        # upper-case and showing C17ORF37 for C17orf37 would be showing a name
        # that does not exist.
        'genes': genes,
        'genes_display': genes_display,
        'oncogenes': oncogenes,
        'oncogenes_display': oncogenes_display,
        'locations': locations,
        'reference_build': reference_build,
        'metadata': metadata,
        'extra_metadata': extra_metadata,
        'schema_version': SCHEMA_VERSION,
    }


def project_digest(project):
    """A hash of exactly what feature_rows_for_project reads.

    The point is that a change which would alter the rows changes this, and a
    change which would not -- a download counter ticking, a new version pointer
    -- does not.  Hashing the whole document would make every counter increment
    look like drift and the drift report would be noise within a day.

    ``SCHEMA_VERSION`` is part of the input so that changing the builder
    invalidates every digest without anyone having to remember to.
    """
    source = {
        'schema_version': SCHEMA_VERSION,
        'project_name': project.get('project_name'),
        # The normalised value, not the raw one: a document whose 'private'
        # is rewritten from True to 'private' means the same thing and
        # produces identical rows, so it is not drift and must not be
        # reported as drift.
        'visibility': normalize_visibility_field(project.get('private')),
        'project_members': sorted(str(member) for member in (project.get('project_members') or [])),
        'runs': project.get('runs') or {},
        'sample_data': project.get('sample_data') or [],
    }
    encoded = json.dumps(source, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha1(encoded).hexdigest()


# Fields of a project document that the builder and the digest read.  Every
# read of ``projects`` in this module uses it, so a full rebuild does not pull
# the payload fields that make a project document large.
INDEX_SOURCE_PROJECTION = {
    'project_name': 1,
    'private': 1,
    'project_members': 1,
    'runs': 1,
    'sample_data': 1,
}


# One document per indexed project, holding the digest of the source it was
# built from.  Kept beside the rows rather than on them: the drift check reads
# one small document per project, where reading a digest off the rows would
# mean touching every row to answer "is this project current".
FEATURE_INDEX_MANIFEST_COLLECTION = 'feature_index_manifest'
manifest_handle = get_collection_handle(db_handle_primary, FEATURE_INDEX_MANIFEST_COLLECTION)


# Every collection this module owns. Exported so that anything which wipes the
# project documents can wipe what was derived from them by importing this
# rather than re-typing a list -- the divergence that keeps costing this
# codebase. Dropping ``projects`` without dropping these leaves the index
# describing projects that no longer exist, which ``feature_index_drift``
# reports as orphaned and a rebuild fixes, but which a search would report as
# results in the meantime.
DERIVED_COLLECTIONS = (
    FEATURE_INDEX_COLLECTION,
    FEATURE_INDEX_MANIFEST_COLLECTION,
    GENE_CATALOG_COLLECTION,
    SEARCH_NAMES_COLLECTION,
)


def indexable_projects_query():
    """The projects that belong in the index.

    LIVE only, which is not a narrowing: ``perform_search`` already resolves
    ``status_query(LIVE, ...)`` at the database, and a measurement on prod on
    2026-09-07 confirmed it -- 33 public LIVE projects against 51 public
    SUPERSEDED, and the search query returns exactly the 33.  Superseded
    versions are reachable by URL and are not, and have never been, search
    results.  Indexing them would grow the collection by 2.5x to serve a
    behaviour the site does not have.
    """
    return status_query(LIVE)


def ensure_feature_index_indexes():
    """Create the indexes the search paths need.  Safe to call repeatedly.

    Verified against DocumentDB on dev on 2026-09-07 with a 30,597-row scratch
    collection.  Three findings shaped what is below, and each is a real
    divergence from MongoDB rather than a preference:

    * A multikey index on ``genes`` is used (IXSCAN), and a compound index
      works with the array member in either position.
    * A compound index containing **two** array fields is rejected outright, so
      ``genes`` and ``oncogenes`` can never share one.  This is why oncogene
      status lives in the gene catalog instead of a second array here.
    * A text index on a multikey path is rejected, so there is no full-text
      path to fall back on.
    """
    feature_index_handle.create_index([('genes', 1), ('classification', 1)], name='ix_genes_class')
    feature_index_handle.create_index([('project_id', 1)], name='ix_project')
    feature_index_handle.create_index([('sample_name', 1)], name='ix_sample_name')
    feature_index_handle.create_index([('project_name', 1)], name='ix_project_name')
    feature_index_handle.create_index([('classification', 1)], name='ix_class')
    feature_index_handle.create_index([('reference_build', 1)], name='ix_reference_build')
    manifest_handle.create_index([('project_id', 1)], name='ix_manifest_project', unique=True)
    gene_catalog_handle.create_index([('symbol', 1)], name='ix_symbol', unique=True)
    search_names_handle.create_index([('kind', 1), ('lower', 1)], name='ix_kind_lower')


def index_project(project):
    """Replace one project's rows.  Returns the number of rows written.

    Delete-then-insert rather than a per-row upsert: a re-aggregation can
    rename, merge or drop samples, so the rows that should no longer exist have
    no key to be updated by.  Deleting the project's rows first is the only
    form of this that cannot leave a stale row behind.
    """
    project_id = project.get('_id')
    rows = feature_rows_for_project(project)
    feature_index_handle.delete_many({'project_id': project_id})
    if rows:
        feature_index_handle.insert_many(rows)
    manifest_handle.update_one(
        {'project_id': project_id},
        {'$set': {
            'project_id': project_id,
            'project_name': project.get('project_name'),
            'digest': project_digest(project),
            'row_count': len(rows),
            'schema_version': SCHEMA_VERSION,
            'indexed_at': datetime.datetime.utcnow(),
        }},
        upsert=True,
    )
    return len(rows)


def unindex_project(project_id):
    """Drop one project's rows and its manifest entry.  Returns rows removed."""
    removed = feature_index_handle.delete_many({'project_id': project_id}).deleted_count
    manifest_handle.delete_one({'project_id': project_id})
    return removed


def rebuild_feature_index(limit=None, progress=None):
    """Rebuild every project's rows from ``projects``.

    Not a drop-and-recreate of the collection: projects are reindexed one at a
    time, so a rebuild that fails part way leaves the index serving stale rows
    for the projects it did not reach rather than serving nothing at all.  The
    drift check then names exactly those projects.

    ``limit`` exists so a rebuild can be staged on a handful of projects and
    the result diffed before the rest is run.
    """
    cursor = collection_handle.find(indexable_projects_query(), INDEX_SOURCE_PROJECTION)
    if limit:
        cursor = cursor.limit(limit)

    projects_seen = set()
    rows_written = 0
    for project in cursor:
        rows_written += index_project(project)
        projects_seen.add(project['_id'])
        if progress:
            progress(project.get('project_name'), len(projects_seen), rows_written)

    if not limit:
        # Anything indexed that is no longer indexable -- deleted, superseded by
        # a newer version, made private-then-deleted -- goes now.  Only safe on
        # a full pass: with a limit, the projects that were not visited would
        # all look stale.
        for stale in list(manifest_handle.find({'project_id': {'$nin': list(projects_seen)}},
                                               {'project_id': 1})):
            unindex_project(stale['project_id'])

    return {'projects': len(projects_seen), 'rows': rows_written}


def feature_index_drift():
    """What disagrees between ``projects`` and the index.  Reads only.

    This is the standing falsifying measurement for "the index is current", and
    it answers the load-bearing question rather than the adjacent one: not "are
    the write hooks all in place" but "does the index say what the projects
    say".  A clean result means the hooks are working *and* nothing wrote
    around them.

    Returns three lists of project ids:

    ``missing``   indexable, with no rows                (a search under-reports)
    ``stale``     indexed, but the source has changed    (a search reports the old truth)
    ``orphaned``  indexed, but no longer indexable       (a search over-reports)
    """
    live_digests = {
        project['_id']: project_digest(project)
        for project in collection_handle.find(indexable_projects_query(), INDEX_SOURCE_PROJECTION)
    }
    indexed = {entry['project_id']: entry.get('digest') for entry in manifest_handle.find({})}

    missing = sorted((pid for pid in live_digests if pid not in indexed), key=str)
    orphaned = sorted((pid for pid in indexed if pid not in live_digests), key=str)
    stale = sorted(
        (pid for pid, digest in live_digests.items()
         if pid in indexed and indexed[pid] != digest),
        key=str,
    )
    return {
        'missing': missing,
        'stale': stale,
        'orphaned': orphaned,
        'indexable': len(live_digests),
        'indexed': len(indexed),
    }


def rebuild_gene_catalog():
    """One document per distinct gene symbol in the index.

    ``is_oncogene`` is taken from the corpus -- a symbol is an oncogene here
    exactly when AmpliconClassifier put it in some row's ``Oncogenes`` -- rather
    than from a copy of AC's list.  Reading it from the data means the flag
    cannot disagree with the rows it describes, and it means this site is not
    maintaining a second copy of a resource file that belongs upstream.

    ``reference_builds`` records which builds a symbol was seen under.  It is
    the evidence a caller needs to notice that a symbol they searched for is
    build-restricted, which is a real effect: measured on prod on 2026-09-07,
    ``C17ORF37`` and ``MIEN1`` are the same gene under two refGene vocabularies,
    on 283 and 160 feature rows respectively, and no single query reaches both.
    """
    catalogue = {}
    for row in feature_index_handle.find({}, {'genes': 1, 'oncogenes': 1, 'reference_build': 1}):
        build = row.get('reference_build') or ''
        oncogenes = set(row.get('oncogenes') or [])
        for symbol in row.get('genes') or []:
            entry = catalogue.setdefault(symbol, {'symbol': symbol,
                                                  'is_oncogene': False,
                                                  'reference_builds': set()})
            if symbol in oncogenes:
                entry['is_oncogene'] = True
            if build:
                entry['reference_builds'].add(build)

    gene_catalog_handle.delete_many({})
    if catalogue:
        gene_catalog_handle.insert_many([
            {'symbol': entry['symbol'],
             'is_oncogene': entry['is_oncogene'],
             'reference_builds': sorted(entry['reference_builds']),
             # Where the names came from, carried on every document rather than
             # written in a doc page that a caller of the API will not read.
             'source': 'refGene, via AmpliconClassifier',
             'schema_version': SCHEMA_VERSION}
            for entry in catalogue.values()
        ])
    return len(catalogue)


def rebuild_search_names():
    """One document per distinct sample name and project name.

    This collection exists because substring name search cannot be served from
    an index on DocumentDB: an anchored prefix regex was measured as a COLLSCAN
    on dev on 2026-09-07, and an unanchored one is a scan on any engine.  The
    answer is not to make the scan indexable but to make it small.  Measured on
    prod on 2026-09-07: 17,094 distinct sample names and 33 distinct project
    names against 37,795 feature rows.

    So a name search scans names -- a fraction of the corpus, and no payload --
    collects the exact names that matched, and hands them to the feature index
    as ``{'sample_name': {'$in': [...]}}``, which is an indexed lookup.  Two
    indexed steps in place of one scan over everything.
    """
    names = {}
    for row in feature_index_handle.find({}, {'sample_name': 1, 'project_name': 1, 'project_id': 1}):
        for kind, value in (('sample', row.get('sample_name')), ('project', row.get('project_name'))):
            text = str(value or '').strip()
            if not text:
                continue
            names[(kind, text)] = {
                'kind': kind,
                'name': text,
                'lower': text.lower(),
                'project_id': row.get('project_id'),
            }

    search_names_handle.delete_many({})
    if names:
        search_names_handle.insert_many(list(names.values()))
    return len(names)


def index_access_filter(user):
    """The rows ``user`` is allowed to see, as a query fragment.

    This is the access-control boundary of the new search path, so it is
    written once and imported, never restated at a call site. The visibility
    values come from ``visibility.py`` for the same reason: that module records
    that these lists were hand-copied into ten query sites across six modules,
    which is how one of them ended up a value behind.

    The shape mirrors ``perform_search`` exactly -- public projects for
    everyone, plus restricted projects the user is a member of, where
    membership is matched against both username and email because both
    spellings appear in ``project_members``.  ``hidden_public`` projects are
    restricted here, not public: they are unlisted, and unlisted means a
    non-member does not get them from a search.
    """
    public = {'visibility': {'$in': PUBLIC_QUERY_VALUES}}
    if not getattr(user, 'is_authenticated', False):
        return public

    identities = [value for value in (getattr(user, 'username', None),
                                      getattr(user, 'email', None)) if value]
    if not identities:
        return public

    return {'$or': [
        public,
        {'visibility': {'$in': RESTRICTED_QUERY_VALUES},
         'project_members': {'$in': identities}},
    ]}


def index_coverage():
    """How many projects should be indexed, and how many are. Two counted queries.

    This is the cheap half of ``feature_index_drift``. That one recomputes every
    project's digest, which means reading ``runs`` for every project -- the very
    scan the index exists to avoid, and far too expensive to do per request.
    This is two ``count_documents`` calls against indexed fields.

    What it catches: a project that was created, deleted, promoted or demoted
    by something that did not go through ``project_events`` -- a migration, a
    backfill, a script, a test fixture inserting straight into the collection.
    That is the common shape of writing around the hooks, and it is the shape
    that makes a search silently return fewer results than the site holds.

    What it does not catch: a project whose *content* changed without its
    indexability changing. Only the digest finds that, so
    ``rebuild_feature_index --check`` is still the standing measurement and
    this does not replace it.
    """
    return {
        'indexable': collection_handle.count_documents(indexable_projects_query()),
        'indexed': manifest_handle.count_documents({}),
    }


def index_is_usable():
    """Whether a search may be served from the index right now.

    A stale index does not fail: it quietly answers with fewer rows than the
    site holds, which is worse than being slow, because nothing about the
    result says it is incomplete. So the reads check first, and a deployment
    whose index has fallen behind serves the old way until someone rebuilds.

    The cost of being wrong in each direction is what sets the default: a false
    'unusable' costs one slow search, a false 'usable' costs a wrong answer.
    """
    coverage = index_coverage()
    return coverage['indexable'] == coverage['indexed'] and coverage['indexed'] > 0
