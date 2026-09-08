"""Answer a search from the feature index instead of from every project document.

``perform_search`` reads every LIVE project in full and filters with pandas.
This translates the same parameters into a query against ``feature_index`` and
returns the same shape, so the caller cannot tell which path served it.

## The rule this module is written to

Every difference is a bug, including the improvements. Deduplicating a gene
list, tidying a spelling, sorting a result -- each would be a better answer and
each would break the only property that makes swapping the paths safe, which is
that they are indistinguishable. Improvements come after the swap, deliberately,
not smuggled in with it.

That is also why the matching primitives are imported from ``search`` rather
than reimplemented here. ``tokenize_query``, ``wildcard_to_regex`` and
``_field_filter`` decide what a query means; a second implementation of them
would agree on the cases somebody thought of and diverge on the rest.

## What it cannot serve, and why that is stated rather than approximated

``can_serve()`` returns False for a free-text ``extra_metadata`` query. Those
search arbitrary uploaded columns, and the existing implementation narrows the
frame column by column, keeping the last narrowing that matched anything --
behaviour that depends on column order and on what other columns happened to
match. Reproducing that against a document store means reproducing the
accident, and approximating it means returning different samples. The old path
still serves those, and says so.

## Names are resolved against a smaller collection first

DocumentDB will not use an index for a regex, anchored or not -- ``/^MY/`` was
measured as a COLLSCAN on dev on 2026-09-07. So a substring name search does
not run against the 30,597-row feature collection. It runs against
``search_names``, which holds one row per distinct name (15,524 on dev), and
the exact names it finds are handed back as an indexed ``$in``. Two indexed
steps instead of one scan over everything.
"""

import re

import pandas as pd
from bson.objectid import ObjectId

from .feature_index import (
    feature_index_handle,
    index_access_filter,
    search_names_handle,
)
from .search import _field_filter, tokenize_query, wildcard_to_regex
from .utils import collection_handle, prepare_project_linkid
from .visibility import PUBLIC_QUERY_VALUES, format_visibility_for_display


def can_serve(extra_metadata=None, **_ignored):
    """Whether the index reproduces this query exactly.

    Deliberately permissive about what it accepts and strict about what it
    claims: a parameter this does not know about cannot make it return True by
    accident, because the only thing that turns it False is the one case that
    is known not to be reproducible.
    """
    return not extra_metadata


# ---------------------------------------------------------------------------
# Gene terms
# ---------------------------------------------------------------------------

def _gene_clause(term):
    """One gene term as a query clause, wildcards included.

    Mirrors ``_gene_matches``: a term containing ``*`` is an anchored regex,
    anything else is an equality match against the upper-cased array. The array
    is upper-cased at index time and the term is upper-cased here, which is what
    makes the comparison case-insensitive without a regex -- and a regex is what
    would cost the index.
    """
    upper = term.upper()
    pattern = wildcard_to_regex(upper)
    if pattern:
        return {'genes': {'$regex': pattern}}
    return {'genes': upper}


def _gene_query(genequery):
    """Translate a gene query, preserving today's operator precedence.

    ``&`` wins over ``|`` regardless of position and the loser stays literal
    text inside its term -- that is what ``get_samples_from_features`` does, and
    reproducing it is the point. It also means ``MYC|EGFR&CDK4`` searches for a
    gene literally named ``MYC|EGFR``, finds nothing, and reports no error.
    That is a real defect in the query language, and it is not this module's to
    fix: fixing it here would make the two paths disagree.
    """
    if not genequery:
        return None

    if '&' in genequery:
        terms = [term.strip() for term in genequery.split('&') if term.strip()]
        clauses = [_gene_clause(term) for term in terms]
        return clauses[0] if len(clauses) == 1 else {'$and': clauses}

    if '|' in genequery:
        terms = [term.strip() for term in genequery.split('|') if term.strip()]
        clauses = [_gene_clause(term) for term in terms]
        return clauses[0] if len(clauses) == 1 else {'$or': clauses}

    return _gene_clause(genequery)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Two classifications are spelled more than one way in the data, and the search
# has always accepted both spellings. Copied here from get_samples_from_features
# rather than imported because they are written inline there; the guard test
# checks the two agree.
CLASSIFICATION_ALIASES = {
    'LINEAR AMPLIFICATION': 'LINEAR AMPLIFICATION|LINEAR',
    'COMPLEX NON-CYCLIC': r'COMPLEX.?NON.?CYCLIC',
}


def _classification_pattern(classquery):
    """The combined regex today's class filter builds."""
    patterns = []
    for term in classquery.split('|'):
        term = term.strip()
        if not term:
            continue
        patterns.append(CLASSIFICATION_ALIASES.get(term.upper(), re.escape(term)))
    return '|'.join(patterns) if patterns else None


def _classification_query(classquery, include_no_amp, no_filter):
    """Class filter, including the zero-feature branches.

    The three no-class branches are not interchangeable and the existing code
    is easy to misread, so they are spelled out:

    * a class filter with ``include_no_amp`` keeps zero-feature rows *beside*
      the matching amplicons, because "ecDNA or nothing found" is a question
      people ask;
    * no class filter and ``include_no_amp`` means only the no-amp box was
      ticked, so it keeps zero-feature rows and nothing else;
    * no class filter and no ``include_no_amp`` means every amplicon type was
      ticked, which excludes the zero-feature rows.
    """
    if classquery:
        pattern = _classification_pattern(classquery)
        if not pattern:
            return None
        match = {'classification': {'$regex': pattern, '$options': 'i'}}
        if include_no_amp:
            return {'$or': [match, {'has_amplicon': False}]}
        return match

    if not no_filter and include_no_amp:
        return {'has_amplicon': False}
    if not no_filter and not include_no_amp:
        return {'has_amplicon': True}
    return None


# ---------------------------------------------------------------------------
# Names, resolved against the small collection
# ---------------------------------------------------------------------------

def _resolve_names(kind, pattern, strip=True):
    """Exact names matching a name query.

    The filter is ``search._field_filter`` over a pandas Series of the distinct
    names, which is the same function the old path applies to the sample-name
    column -- so quoting, ``*`` anchors and the ``&``/``|`` operators mean
    exactly what they mean everywhere else, and there is no second copy of that
    logic to fall behind.

    Returns None when the query filters nothing out, so the caller can leave
    the clause off entirely rather than adding a match on every name there is.
    """
    terms, _operator = tokenize_query(pattern)
    if not terms:
        return None

    names = [row['name'] for row in
             search_names_handle.find({'kind': kind}, {'name': 1, '_id': 0})]
    if not names:
        return []

    series = pd.Series(names, dtype=object)
    if strip:
        series = series.str.strip()
    mask = _field_filter(series, pattern)
    return [name for name, keep in zip(names, mask) if keep]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _term_regex_for_value(text, quoted):
    """Regex for one metadata term, mirroring ``_term_mask``."""
    if quoted:
        return '^' + re.escape(text) + '$'
    pattern = wildcard_to_regex(text)
    if pattern:
        return pattern
    return re.escape(text)


def _metadata_query(field_paths, pattern):
    """Substring filter over one or more metadata fields, ORed together.

    ``field_paths`` is a list because the cancer-type box searches Cancer_type
    and Tissue_of_origin together, which is a single OR in the old path and has
    to stay one here -- splitting it into two filters would AND them.
    """
    terms, operator = tokenize_query(pattern)
    if not terms:
        return None

    def any_field(text, quoted):
        regex = _term_regex_for_value(text, quoted)
        clauses = [{path: {'$regex': regex, '$options': 'i'}} for path in field_paths]
        return clauses[0] if len(clauses) == 1 else {'$or': clauses}

    clauses = [any_field(text, quoted) for text, quoted in terms]
    if len(clauses) == 1:
        return clauses[0]
    return {'$and': clauses} if operator == 'and' else {'$or': clauses}


# ---------------------------------------------------------------------------
# Assembling a query
# ---------------------------------------------------------------------------

def build_index_query(genequery=None, project_name=None, classquery=None,
                      metadata_sample_name=None, metadata_sample_type=None,
                      metadata_cancer_type=None, metadata_tissue_origin=None,
                      access=None):
    """The whole search as one query, or None when nothing can match.

    None is returned rather than a query that matches nothing, because a name
    query that resolves to no names is a different situation from an empty
    filter and the caller has to be able to tell them apart. A filter that
    matches every name contributes no clause at all.
    """
    clauses = []
    if access:
        clauses.append(access)

    gene_clause = _gene_query(genequery)
    if gene_clause:
        clauses.append(gene_clause)

    if project_name:
        names = _resolve_names('project', project_name, strip=False)
        if names is not None:
            if not names:
                return None
            clauses.append({'project_name': {'$in': names}})

    if metadata_sample_name:
        names = _resolve_names('sample', metadata_sample_name)
        if names is not None:
            if not names:
                return None
            clauses.append({'sample_name': {'$in': names}})

    if metadata_sample_type:
        clause = _metadata_query(['metadata.Sample_type'], metadata_sample_type)
        if clause:
            clauses.append(clause)

    if metadata_cancer_type:
        # One OR across both fields, not two filters: the cancer-type box on the
        # search page searches cancer type *or* tissue of origin, and splitting
        # it would quietly turn that into an AND.
        clause = _metadata_query(
            ['metadata.Cancer_type', 'metadata.Tissue_of_origin'], metadata_cancer_type)
        if clause:
            clauses.append(clause)

    if metadata_tissue_origin:
        clause = _metadata_query(['metadata.Tissue_of_origin'], metadata_tissue_origin)
        if clause:
            clauses.append(clause)

    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {'$and': clauses}


# ---------------------------------------------------------------------------
# Turning rows back into what the page expects
# ---------------------------------------------------------------------------

# The keys a search result row is read by -- the template reads ten of them,
# and the view reads project_linkid and Sample_name. Written down because a row
# built from the index has to carry exactly these: a missing key renders as an
# empty cell rather than as an error, so an omission here is invisible until
# somebody notices a blank column.
RESULT_ROW_KEYS = (
    'Sample_name', 'Feature_ID', 'Classification', 'All_genes', 'Oncogenes',
    'Sample_type', 'Cancer_type', 'Tissue_of_origin',
    'project_name', 'project_linkid', 'project_url',
)


def _result_row(row, project_url):
    """One index row in the shape ``get_samples_from_features`` returns."""
    metadata = row.get('metadata') or {}
    return {
        'Sample_name': row.get('sample_name'),
        'Feature_ID': row.get('feature_id'),
        'Classification': row.get('classification'),
        # The display arrays, not the upper-cased ones that were matched
        # against: 1,083 symbols in the corpus are not upper-case.
        'All_genes': row.get('genes_display') or [],
        'Oncogenes': row.get('oncogenes_display') or [],
        'Sample_type': metadata.get('Sample_type', ''),
        'Cancer_type': metadata.get('Cancer_type', ''),
        'Tissue_of_origin': metadata.get('Tissue_of_origin', ''),
        'project_name': row.get('project_name'),
        'project_linkid': row.get('project_id'),
        'project_url': project_url,
    }


def _project_stub(row):
    """The project as the results table reads it, without reading ``runs``.

    Everything the table shows comes off the index row or off a projection that
    excludes the payload. ``sample_count_display`` is the count the table
    prints; it is taken from the index rather than from the project's stored
    ``sample_count``, which is missing or wrong on 4 of 52 LIVE projects on dev.
    """
    return {
        '_id': row.get('project_id'),
        'project_name': row.get('project_name'),
        'sample_count_display': row.get('project_sample_count', 0),
    }


# The project fields the results table shows. Everything else on a project
# document -- runs, sample_data, aggregate_df, ecDNA_context -- is payload, and
# not reading it is the entire point: those fields are 61.5 MiB of the 61.5 MiB
# a search transfers today (measured on prod, 33 public LIVE projects,
# 2026-09-07).
PROJECT_TABLE_PROJECTION = {
    'project_name': 1,
    'description': 1,
    'date': 1,
    'private': 1,
}


def _projects_for(rows, user_is_member_view):
    """Project rows for the results table, in the order the old path returns them.

    The old path returns the projects its own query returned, filtered to those
    that contributed a sample. Both paths therefore list the same projects; this
    one just never loads their payload to do it.
    """
    counts = {}
    for row in rows:
        project_id = row.get('project_id')
        if project_id is not None and project_id not in counts:
            counts[project_id] = row.get('project_sample_count', 0)
    if not counts:
        return []

    projects = []
    for document in collection_handle.find({'_id': {'$in': list(counts)}},
                                           PROJECT_TABLE_PROJECTION):
        prepare_project_linkid(document)
        document['visibility_display'] = format_visibility_for_display(
            document.get('private', True if user_is_member_view else False))
        document['sample_count_display'] = counts.get(document['_id'], 0)
        projects.append(document)
    return projects


def search_from_index(genequery=None, project_name=None, classquery=None,
                      metadata_sample_name=None, metadata_sample_type=None,
                      metadata_cancer_type=None, metadata_tissue_origin=None,
                      extra_metadata=None, include_no_amp=True, no_filter=False,
                      user=None):
    """``perform_search``, served from the feature index.

    Returns the same four keys with the same contents. The public/private split
    is done on the rows rather than by running the query twice: one indexed
    query returns everything this user may see, and ``visibility`` on each row
    says which bucket it belongs in. The old path issues two queries because it
    is reading whole project documents and cannot afford to sort them out
    afterwards; here the rows are small and already in hand.
    """
    access = index_access_filter(user)
    filters = _classification_query(classquery, include_no_amp, no_filter)

    query = build_index_query(
        genequery=genequery, project_name=project_name, classquery=classquery,
        metadata_sample_name=metadata_sample_name,
        metadata_sample_type=metadata_sample_type,
        metadata_cancer_type=metadata_cancer_type,
        metadata_tissue_origin=metadata_tissue_origin,
        access=access)

    if query is None:
        rows = []
    else:
        if filters:
            query = {'$and': [query, filters]} if query else filters
        rows = list(feature_index_handle.find(query))

    public_rows, private_rows = [], []
    for row in rows:
        if row.get('visibility') in PUBLIC_QUERY_VALUES:
            public_rows.append(row)
        else:
            private_rows.append(row)

    # One reverse() per project rather than one per row: project_linkid takes
    # as many distinct values as there are projects, and resolving the route per
    # row is what made the unfiltered search re-resolve it 16,950 times.
    from django.urls import reverse
    urls = {}
    for row in rows:
        project_id = row.get('project_id')
        if project_id not in urls:
            urls[project_id] = reverse('project_page',
                                       kwargs={'project_name': str(project_id)})

    return {
        'public_projects': _projects_for(public_rows, user_is_member_view=False),
        'private_projects': _projects_for(private_rows, user_is_member_view=True),
        'public_sample_data': [_result_row(row, urls.get(row.get('project_id')))
                               for row in public_rows],
        'private_sample_data': [_result_row(row, urls.get(row.get('project_id')))
                                for row in private_rows],
    }
