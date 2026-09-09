"""``GET /api/v1/features/`` -- search amplicons, and count them, from the index.

The project endpoints answer "what is in this project". This answers "which
samples, anywhere, look like *this*" -- and it is the question an agent actually
arrives with. Until now the only way to ask it was to list every project, fetch
every project's samples, and filter client-side, which transfers the whole
corpus to answer a question about a handful of rows.

## Why this is built on the feature index and never on ``perform_search``

``perform_search`` loads every candidate project document, ``runs`` payload and
all, and filters in pandas. Measured on prod on 2026-09-07 that is 61.5 MiB and
2.73 s for a single gene, and the cost does not depend on the query: an absent
gene costs the same as ``MYC``. A published contract built on that would bake in
a limit that could not be paginated out of, because the rows are materialised
before anything can slice them. Cursor pagination, ``count_only`` and field
selection are all in from the first commit for that reason -- they are cheap
now, and they are impossible to add later without changing what the endpoint
means.

## What the parameters guarantee

Every clause is internally one operator, so no query's meaning depends on
precedence. ``gene_any`` is an OR, ``gene_all`` is an AND, and supplying both
ANDs the two clauses together -- which is unambiguous and is the kind of query
an agent composes ("MYC and CDK4, plus at least one receptor"). There is no
syntax for a mixed expression, so a mixed expression is not rejected by a
validator, it is unexpressible. That is worth more than a check: today's UI
query language accepts ``MYC|EGFR&CDK4``, gives ``&`` precedence, leaves ``|``
as literal text inside the term, and returns zero rows for a gene literally
named ``MYC|EGFR`` -- with no error.

## The one place a wrong answer is possible, and what is done about it

The index is derived from ``runs``, so a project written around the lifecycle
hooks would leave it stale, and a search would report fewer rows than the site
holds. Rather than serve that silently, every request checks
``index_is_usable()`` and a stale index is a 503 with a ``code`` a client can
back off on -- not an empty result set that reads like an answer. The search
page can fall back to the slow path; an API contract cannot, because the caller
has no way to tell a real zero from a broken index.
"""

import base64
import binascii
import logging

from bson.objectid import ObjectId
from bson.errors import InvalidId

from .classifications import (
    ACCEPTED_CLASSIFICATION_INPUTS, CANONICAL_CLASSIFICATIONS,
    NO_AMPLICON_CANONICAL, canonical_classification,
)
from .request_url import absolute_url
from .feature_index import (
    REFERENCE_EQUIVALENCE, feature_index_handle, index_access_filter,
    index_is_usable, normalize_gene,
)

# A page is capped so that one request cannot ask for the corpus.  500 is not a
# performance limit -- it is the point past which a caller should be using the
# cursor, which costs them nothing and keeps each response bounded.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# A gene AND query has to intersect samples, and the intersection is done by
# reading the matching rows' sample keys.  That read is bounded so a two-gene
# query against a very common pair cannot pull an unbounded key list into
# memory; past the bound the request is refused with a code that says to narrow
# it, rather than being served slowly or partially.
MAX_AND_SAMPLE_KEYS = 200_000

# What a row reports.  Field selection picks from exactly this set: a client
# asking for a field that does not exist is told, rather than silently getting a
# response missing the column it was counting on.
ROW_FIELDS = (
    'project_id', 'project_name', 'sample_name', 'feature_id',
    'classification', 'genes', 'oncogenes', 'locations', 'reference_build',
    'sample_type', 'cancer_type', 'tissue_of_origin', 'project_url',
    'sample_url',
)
DEFAULT_FIELDS = ROW_FIELDS

# Every query parameter ``/api/v1/features/`` accepts.  Anything else is a 400
# rather than silence.  Silence is the dangerous answer here: an unrecognised
# filter that is ignored does not narrow the query, so the caller gets the whole
# corpus back and reads it as the answer to the question they thought they
# asked.  That is exactly how ``tissue_of_origin`` went unnoticed -- see below.
ACCEPTED_PARAMS = frozenset({
    'gene_any', 'gene_all', 'same_amp', 'classification', 'oncogenes_only',
    'project_id', 'project_name', 'sample_name', 'sample_type', 'cancer_type',
    'tissue_of_origin', 'reference_build', 'fields', 'limit', 'cursor',
    'count_only',
    # DRF's own content negotiation reads this one; it is not ours to refuse.
    'format',
})

# ``/features/facets/`` keys its tissue facet ``tissue_of_origin``, and a row
# reports the field under that name too, but the filter originally accepted only
# ``tissue``.  So a client that read the facets response and filtered on the name
# it found there had its parameter silently dropped and got every row back.
# Measured on prod 2026-09-08: ``?tissue_of_origin=lung`` returned 37,795 -- the
# whole corpus -- where ``?tissue=lung`` returned 467.  The facet name is now the
# parameter name; ``tissue`` stays accepted so existing callers keep working.
PARAM_ALIASES = {'tissue': 'tissue_of_origin'}


def reject_unknown_params(params):
    """400 on any parameter this endpoint does not implement.

    Returns the parameter names folded through PARAM_ALIASES, so the caller of
    this function reads canonical names only.
    """
    seen = {}
    unknown = []
    for name in params.keys():
        canonical = PARAM_ALIASES.get(name, name)
        if canonical not in ACCEPTED_PARAMS:
            unknown.append(name)
        else:
            seen[canonical] = name
    if unknown:
        raise FeatureQueryError(
            '%s is not a parameter of this endpoint. Accepted: %s.'
            % (', '.join(repr(u) for u in sorted(unknown)),
               ', '.join(sorted(ACCEPTED_PARAMS))),
            'invalid_parameter')
    return seen


class FeatureQueryError(Exception):
    """A caller's request cannot be answered as asked.

    Carries the stable ``code`` the v1 error contract promises, because the
    prose is not what a program should branch on.
    """

    def __init__(self, message, code='bad_request', status_code=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

_TRUE = {'true', '1', 'yes', 'on'}
_FALSE = {'false', '0', 'no', 'off', ''}


def parse_bool(value, name, default=False):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise FeatureQueryError(
        f"'{name}' must be true or false, not {value!r}.", 'invalid_parameter')


def parse_csv(value):
    """A comma-delimited list, the OpenAPI ``style: form, explode: false`` shape.

    Comma rather than a repeated parameter or ``&``: ``&`` cannot survive a URL
    (``?gene=MYC&EGFR`` parses as ``gene=MYC`` plus a valueless parameter, and
    would silently return MYC-only results), ``+`` decodes to a space, and ``;``
    is split as a query separator by some servers.  Comma needs no encoding,
    which matters because agents write these URLs by hand.
    """
    if not value:
        return []
    return [part.strip() for part in str(value).split(',') if part.strip()]


def parse_limit(value):
    if value is None or str(value).strip() == '':
        return DEFAULT_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise FeatureQueryError(
            f"'limit' must be a whole number, not {value!r}.", 'invalid_parameter')
    if limit < 1:
        raise FeatureQueryError("'limit' must be at least 1.", 'invalid_parameter')
    if limit > MAX_LIMIT:
        raise FeatureQueryError(
            f"'limit' may not exceed {MAX_LIMIT}; use 'cursor' to page.",
            'invalid_parameter')
    return limit


def encode_cursor(object_id):
    """An opaque cursor.

    It is the id of the last row of the page, base64'd so that it reads as
    opaque and a client is not tempted to compute one.  Deliberately not an
    offset: ``skip`` degrades linearly with depth and invites a client to walk
    the whole corpus a page at a time.
    """
    return base64.urlsafe_b64encode(str(object_id).encode()).decode()


def decode_cursor(value):
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(str(value).encode()).decode()
        return ObjectId(raw)
    except (binascii.Error, InvalidId, UnicodeDecodeError, ValueError):
        raise FeatureQueryError(
            "'cursor' is not a cursor this endpoint issued.", 'invalid_cursor')


def parse_fields(value):
    if not value:
        return list(DEFAULT_FIELDS)
    requested = parse_csv(value)
    unknown = [name for name in requested if name not in ROW_FIELDS]
    if unknown:
        raise FeatureQueryError(
            f"Unknown field(s): {', '.join(sorted(unknown))}. "
            f"Valid fields are: {', '.join(ROW_FIELDS)}.", 'invalid_parameter')
    return requested


def parse_classifications(values):
    """Validate against the vocabulary, and fold aliases onto one spelling.

    An unknown classification is a 400 and not an empty result, because an empty
    result is indistinguishable from a real answer and a typo would read as
    'this repository holds no ecDNA'.
    """
    wanted = []
    for raw in values:
        for value in parse_csv(raw):
            if value.upper() not in ACCEPTED_CLASSIFICATION_INPUTS:
                raise FeatureQueryError(
                    f"Unknown classification {value!r}. Valid values are: "
                    f"{', '.join(CANONICAL_CLASSIFICATIONS)}.",
                    'invalid_classification')
            canonical = canonical_classification(value)
            if canonical not in wanted:
                wanted.append(canonical)
    return wanted


def parse_reference_build(value):
    """Fold an equivalent build name, but never reject one the corpus contains.

    REFERENCE_EQUIVALENCE folds the names that mean the same assembly (GRCh38 ->
    hg38). It is not the list of builds that exist. Prod carries **mm10**, which
    neither the local corpus nor dev had, so a static allowlist advertised mm10
    through /facets/ and then answered ?reference_build=mm10 with a 400 -- the
    same facets-versus-filter break this endpoint had for classification, found
    the same way, on the first real query after it shipped.

    So an unrecognised name is checked against the index before it is called a
    typo. The distinct() runs only for a name outside the fold map, so the
    common builds cost nothing.
    """
    if not value:
        return None
    build = str(value).strip()
    normalized = REFERENCE_EQUIVALENCE.get(build, REFERENCE_EQUIVALENCE.get(build.lower()))
    if normalized:
        return normalized

    present = set(feature_index_handle.distinct('reference_build'))
    for candidate in (build, build.lower()):
        if candidate in present:
            return candidate
    known = sorted(set(REFERENCE_EQUIVALENCE.values()) | {p for p in present if p})
    raise FeatureQueryError(
        f"Unknown reference build {build!r}. Valid values are: "
        f"{', '.join(known)}.", 'invalid_parameter')


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

def _and(clauses):
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {'$and': clauses}


def _classification_clause(classifications):
    """Match the canonical spellings *and* the aliases that fold onto them.

    The rows carry whatever spelling the classifier wrote, so filtering on the
    canonical value alone would miss ``LINEAR AMPLIFICATION`` when the caller
    asked for ``Linear``.  Matching is case-insensitive on an exact value rather
    than by regex so the classification index is still usable.
    """
    if not classifications:
        return None

    branches = []
    wanted = set()
    for canonical in classifications:
        if canonical == NO_AMPLICON_CANONICAL:
            # 'None' is not a spelling in the rows -- it is the absence of an
            # amplicon, which the index already carries as a flag.  Matching the
            # flag rather than the sentinel strings means a project written by a
            # classifier version that spells it differently is still found.
            #
            # The blank is matched as well, because rows indexed before
            # is_no_amplicon() learned about a null Classification carry
            # has_amplicon: True with an empty classification -- 1,002 rows on
            # prod and 4,117 on dev when this was measured, 2026-09-08.  A
            # reindex rewrites them to 'NA' and the flag becomes sufficient; the
            # clause stays correct either way, and costs one extra branch.
            branches.append({'$or': [{'has_amplicon': False},
                                     {'classification': ''}]})
            continue
        wanted.add(canonical)
        for alias, _folds_to in _ALIAS_BY_CANONICAL.get(canonical, ()):
            wanted.add(alias)

    if wanted:
        # Take the spellings from the rows themselves and fold each through the
        # same function /facets/ uses, so the two cannot disagree.  Enumerating
        # case variants of the alias table instead -- which is what this did --
        # left any spelling the table did not anticipate reachable through
        # /facets/ and unreachable through the filter: 'Linear' was advertised
        # with 1,420 rows locally and returned 1,417.  Deriving both sides from
        # one source is the only version of this that stays true as the
        # classifier's spellings change.
        spellings = sorted(
            raw for raw in feature_index_handle.distinct('classification')
            if isinstance(raw, str)
            and canonical_classification(raw) in wanted)
        if spellings:
            branches.append({'classification': {'$in': spellings}})

    if len(branches) == 1:
        return branches[0]
    return {'$or': branches}


def _build_alias_index():
    from .classifications import _CANONICAL_CLASSIFICATION
    out = {}
    for alias, canonical in _CANONICAL_CLASSIFICATION.items():
        out.setdefault(canonical, []).append((alias, canonical))
    return out


_ALIAS_BY_CANONICAL = _build_alias_index()


def _gene_clauses(gene_any, gene_all, same_amp):
    """The gene half of the query, as clauses on a single row.

    ``gene_any`` is one ``$in``.  ``gene_all`` with ``same_amp`` is one ``$all``
    -- both genes on the same focal amplification, which is one row.  Without
    ``same_amp`` the AND spans a whole sample and cannot be answered by a
    single-row clause at all; ``sample_keys_for_and`` does that pass and this
    returns only the cheap prefilter for it.
    """
    clauses = []
    if gene_any:
        clauses.append({'genes': {'$in': [normalize_gene(g) for g in gene_any]}})
    if gene_all:
        genes = [normalize_gene(g) for g in gene_all]
        if same_amp:
            clauses.append({'genes': {'$all': genes}})
        else:
            # Not the answer -- just the rows the sample-level pass has to look
            # at.  Narrowing here keeps that pass off the whole collection.
            clauses.append({'genes': {'$in': genes}})
    return clauses


def sample_keys_for_and(genes, base_clauses):
    """Samples carrying every one of ``genes`` across any of their features.

    A sample's genes are spread over its amplicons, so "MYC and CDK4 in the same
    sample" is not a predicate on any single row.  This asks the indexed
    collection once per gene for the sample keys carrying it and intersects the
    sets, which is why ``sample_key`` is stored on the row: intersecting keys is
    cheap, and the result feeds back as one indexed ``$in`` rather than a large
    ``$or`` over (project, sample) pairs.

    Returns None when there is nothing to do, or a set -- possibly empty, which
    means the query is answerable and the answer is no rows.
    """
    if not genes:
        return None

    keys = None
    for gene in genes:
        clause = _and(list(base_clauses) + [{'genes': normalize_gene(gene)}])
        found = set()
        cursor = feature_index_handle.find(clause, {'sample_key': 1, '_id': 0})
        for row in cursor:
            found.add(row.get('sample_key'))
            if len(found) > MAX_AND_SAMPLE_KEYS:
                raise FeatureQueryError(
                    'That gene combination matches too many samples to '
                    'intersect. Add a project_id, classification or metadata '
                    'filter to narrow it.', 'query_too_broad')
        keys = found if keys is None else (keys & found)
        # Nothing survives; no later gene can add anything back.
        if not keys:
            return set()
    return keys


def non_gene_clauses(*, user, classifications=(), project_id=None,
                     sample_name=None, project_name=None, sample_type=None,
                     cancer_type=None, tissue_of_origin=None,
                     reference_build=None,
                     oncogenes_only=False):
    """Every filter except the gene ones, as a list of clauses.

    Kept separate because the sample-level gene AND needs exactly these and must
    not see the row-level gene clauses.  Deciding whether a sample carries MYC
    *and* EGFR means looking at its rows one gene at a time; carrying a
    ``gene_any`` clause into that pass would require both genes on the same row,
    which is ``same_amp`` -- a different question, and the one the caller did not
    ask.  That is not hypothetical: ``gene_all=MYC,EGFR&gene_any=EGFR`` returned
    0 instead of 1 until these were split.
    """
    clauses = [index_access_filter(user)]

    classification_clause = _classification_clause(classifications)
    if classification_clause:
        clauses.append(classification_clause)

    if project_id:
        try:
            clauses.append({'project_id': ObjectId(project_id)})
        except (InvalidId, TypeError):
            raise FeatureQueryError(
                f"'project_id' is not a valid project id: {project_id!r}.",
                'invalid_parameter')

    if sample_name:
        clauses.append({'sample_name_lower': str(sample_name).strip().lower()})
    if project_name:
        clauses.append({'project_name_lower': str(project_name).strip().lower()})
    if reference_build:
        clauses.append({'reference_build': reference_build})
    if oncogenes_only:
        clauses.append({'oncogenes.0': {'$exists': True}})

    for field, value in (('Sample_type', sample_type),
                         ('Cancer_type', cancer_type),
                         ('Tissue_of_origin', tissue_of_origin)):
        # Deliberately NOT comma-split.  These values are free text written by
        # whoever submitted the project, and real ones contain commas: the index
        # carries 'BRAIN, & CRANIAL NERVES, & SPINAL CORD, (EXCL. VENTRICLE,
        # CEREBELLUM)' and 'UTERUS, NOS'.  Splitting on commas turned each of
        # those into fragments that match nothing -- /facets/ advertised the
        # first with 316 rows and filtering on it returned 1.  Repeat the
        # parameter to pass several values; that is unambiguous whatever the
        # value contains.
        values = [str(v).strip() for v in (value or []) if str(v).strip()]
        if values:
            # Exact, case-insensitively, against the values the rows carry.
            # Metadata is not unified across projects, so this is a convenience
            # and not a contract -- every row returns its own metadata for the
            # caller to interpret, which is the authoritative answer.
            spellings = sorted({s for v in values for s in (v, v.upper(), v.lower(),
                                                           v.title())})
            clauses.append({f'metadata.{field}': {'$in': spellings}})

    return clauses


def build_query(*, gene_any=(), gene_all=(), same_amp=False, **filters):
    """The whole request as one Mongo query over single rows."""
    return _and(non_gene_clauses(**filters)
                + _gene_clauses(gene_any, gene_all, same_amp))


# ---------------------------------------------------------------------------
# Rows out
# ---------------------------------------------------------------------------

def row_to_dict(row, fields, request=None):
    """One index row as the API reports it.

    Gene lists come from the ``*_display`` arrays, not the matched ones: the
    matched arrays are upper-cased so that an equality match is indexable, and
    1,083 of the corpus's symbols are not upper-case.  Reporting ``C17ORF37``
    for ``C17orf37`` would be reporting a name that does not exist.
    """
    project_id = row.get('project_id')
    linkid = str(project_id) if project_id is not None else None
    sample_name = row.get('sample_name')

    metadata = row.get('metadata') or {}
    available = {
        'project_id': linkid,
        'project_name': row.get('project_name'),
        'sample_name': sample_name,
        'feature_id': row.get('feature_id'),
        'classification': canonical_classification(row.get('classification'))
                          if row.get('classification') else row.get('classification'),
        'genes': row.get('genes_display') or [],
        'oncogenes': row.get('oncogenes_display') or [],
        'locations': row.get('locations') or [],
        'reference_build': row.get('reference_build'),
        'sample_type': metadata.get('Sample_type'),
        'cancer_type': metadata.get('Cancer_type'),
        'tissue_of_origin': metadata.get('Tissue_of_origin'),
        # Every row carries the URLs to fetch its data, so a search result is
        # directly actionable: find the samples, then pull exactly those.  The
        # search itself never returns payload -- a result set has no natural
        # size bound and download authorisation is per project, not per row.
        'project_url': _absolute(request, f'/api/v1/projects/{linkid}/') if linkid else None,
        'sample_url': (_absolute(request, f'/api/v1/projects/{linkid}/samples/')
                       if linkid else None),
    }
    return {field: available[field] for field in fields}


def _absolute(request, path):
    """Absolute URL for a row's links.

    Via request_url, not build_absolute_uri: behind the TLS-terminating ELB the
    container's own scheme is http, and prod handed out
    http://ampliconrepository.org/... on the first request after this shipped.
    """
    return absolute_url(request, path)


def reference_build_facet(query):
    """How the result set splits across reference builds.

    On every result set, not only on the facets endpoint, and that is
    deliberate.  Gene symbols are build-dependent -- the corpus carries both
    vocabularies, and measured on prod on 2026-09-07 no project mixes them: 7
    projects are hg19-era, 17 hg38-era.  So ``C17ORF37`` and ``MIEN1`` are the
    same gene under two names, and no single query reaches both.  A result of
    ``{"hg38": 56, "hg19": 0}`` is the caller's only signal that the other name
    exists on the other side of the fence.  It is not a fix; it is the evidence
    that a fix is needed for that query.
    """
    pipeline = [{'$match': query},
                {'$group': {'_id': '$reference_build', 'count': {'$sum': 1}}}]
    counts = {}
    for row in feature_index_handle.aggregate(pipeline):
        counts[row['_id'] or 'unknown'] = row['count']
    return counts


# ---------------------------------------------------------------------------
# Running one search
# ---------------------------------------------------------------------------

def _values(params, name, *aliases):
    """Repeatable parameter values, read from the canonical name or an alias.

    A QueryDict repeats; a plain dict does not.  Both are passed in by tests and
    by the view, so both are handled here rather than at each call site.
    """
    for candidate in (name,) + aliases:
        if hasattr(params, 'getlist'):
            values = params.getlist(candidate)
        else:
            value = params.get(candidate)
            values = [value] if value is not None else []
        if values:
            return values
    return []


def search_features(params, user, request=None):
    """Answer one ``/api/v1/features/`` request.

    Raises FeatureQueryError for anything the caller can fix; the view turns
    that into the v1 error body.
    """
    if not index_is_usable():
        # Deliberately not a fallback to the slow path and deliberately not an
        # empty result.  A caller cannot tell a real zero from a stale index,
        # and an API that answers "no ecDNA anywhere" when it means "ask again
        # later" is worse than one that is briefly unavailable.
        raise FeatureQueryError(
            'The search index is rebuilding and cannot answer accurately yet. '
            'Retry shortly.', 'index_unavailable', status_code=503)

    reject_unknown_params(params)

    gene_any = parse_csv(params.get('gene_any'))
    gene_all = parse_csv(params.get('gene_all'))
    same_amp = parse_bool(params.get('same_amp'), 'same_amp')
    if same_amp and not gene_all:
        raise FeatureQueryError(
            "'same_amp' narrows an AND gene query and only means something "
            "with 'gene_all'.", 'invalid_parameter')

    limit = parse_limit(params.get('limit'))
    fields = parse_fields(params.get('fields'))
    count_only = parse_bool(params.get('count_only'), 'count_only')
    after = decode_cursor(params.get('cursor'))

    filters = dict(
        user=user,
        classifications=parse_classifications(params.getlist('classification')
                                              if hasattr(params, 'getlist')
                                              else params.get('classification') or []),
        project_id=params.get('project_id'),
        sample_name=params.get('sample_name'),
        project_name=params.get('project_name'),
        sample_type=(params.getlist('sample_type') if hasattr(params, 'getlist')
                     else params.get('sample_type')),
        cancer_type=(params.getlist('cancer_type') if hasattr(params, 'getlist')
                     else params.get('cancer_type')),
        tissue_of_origin=_values(params, 'tissue_of_origin', 'tissue'),
        reference_build=parse_reference_build(params.get('reference_build')),
        oncogenes_only=parse_bool(params.get('oncogenes_only'), 'oncogenes_only'),
    )
    base = non_gene_clauses(**filters)
    query = _and(base + _gene_clauses(gene_any, gene_all, same_amp))

    # A gene AND that is not confined to one amplification is a question about a
    # sample, not about a row, so it needs a second pass.  It runs against the
    # non-gene filters only -- narrowed by project, classification and metadata,
    # but not by the row-level gene clauses, which would silently turn it into
    # same_amp.
    if gene_all and not same_amp:
        keys = sample_keys_for_and(gene_all, base)
        if not keys:
            return _empty_response(fields, count_only)
        query = _and(base + _gene_clauses(gene_any, gene_all, same_amp)
                     + [{'sample_key': {'$in': sorted(keys)}}])

    total = feature_index_handle.count_documents(query)
    if count_only:
        return {
            'count': total,
            'reference_builds': reference_build_facet(query),
        }

    # Sort by _id so the cursor is a position in a stable order.  Any sort the
    # caller could choose would need its own index and would make the cursor
    # ambiguous across ties; _id is unique, so a page boundary can never repeat
    # or skip a row.
    page_query = {'$and': [query, {'_id': {'$gt': after}}]} if after else query
    rows = list(feature_index_handle.find(page_query).sort('_id', 1).limit(limit + 1))

    has_more = len(rows) > limit
    rows = rows[:limit]

    return {
        'count': total,
        'results': [row_to_dict(row, fields, request) for row in rows],
        'next_cursor': encode_cursor(rows[-1]['_id']) if (has_more and rows) else None,
        'reference_builds': reference_build_facet(query),
    }


def _empty_response(fields, count_only):
    if count_only:
        return {'count': 0, 'reference_builds': {}}
    return {'count': 0, 'results': [], 'next_cursor': None,
            'reference_builds': {}}


# ---------------------------------------------------------------------------
# The vocabulary a caller can filter on
# ---------------------------------------------------------------------------

FACET_FIELDS = {
    'classification': 'classification',
    'sample_type': 'metadata.Sample_type',
    'cancer_type': 'metadata.Cancer_type',
    'tissue_of_origin': 'metadata.Tissue_of_origin',
    'reference_build': 'reference_build',
}


def feature_facets(user):
    """The values that actually appear, with counts, for the rows a user may see.

    So a client discovers the vocabulary instead of guessing at it and getting a
    silent empty result -- which, for metadata that is not unified across
    projects, is otherwise the normal outcome of a reasonable guess.

    ``total_rows`` is reported alongside because the facet counts alone cannot
    show what is *missing*.  A row whose project never recorded a cancer type
    carries no value to group on, so it appears in no facet entry; measured on
    prod 2026-09-08, the cancer_type entries summed to 17,122 against 37,795
    rows visible anonymously.  A client comparing those two numbers can tell
    that filtering on cancer type reaches at most 45% of the corpus.  Without
    the total there is nothing to compare against, and a filtered count reads
    as complete when it is a floor.

    Reporting the total rather than a coverage percentage is deliberate: the
    percentage moves whenever metadata is backfilled, and anything that quotes
    a percentage goes stale silently.  Two counts computed per request cannot.
    """
    if not index_is_usable():
        raise FeatureQueryError(
            'The search index is rebuilding and cannot answer accurately yet. '
            'Retry shortly.', 'index_unavailable', status_code=503)

    access = index_access_filter(user)
    facets = {}
    for name, path in FACET_FIELDS.items():
        pipeline = [{'$match': access},
                    {'$group': {'_id': f'${path}', 'count': {'$sum': 1}}},
                    {'$sort': {'count': -1}}]
        values = []
        for row in feature_index_handle.aggregate(pipeline):
            value = row['_id']
            if name == 'classification':
                # A blank is not an absence here, it is a classification: rows
                # indexed before is_no_amplicon() learned about a null
                # Classification carry '' and mean "no amplicon found".  Folding
                # before the skip puts them in the 'None' entry, which is where
                # ?classification=None now finds them.  Skipping first dropped
                # them from the facet while the filter still returned them --
                # the same facets-versus-filter break in the other direction.
                value = canonical_classification(value)
            if value in (None, '', 'Not Provided'):
                continue
            values.append({'value': value, 'count': row['count']})
        # Every value advertised here must be one the filter accepts -- see
        # test_facet_values_are_all_filterable.  A facet a client cannot then
        # use is worse than no facet: it reads as a supported query.
        # Aliases fold onto one spelling, so two rows can become one entry.
        merged = {}
        for entry in values:
            merged[entry['value']] = merged.get(entry['value'], 0) + entry['count']
        facets[name] = [{'value': v, 'count': c} for v, c in
                        sorted(merged.items(), key=lambda kv: -kv[1])]
    return {'total_rows': feature_index_handle.count_documents(access),
            'facets': facets}
