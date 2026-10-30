"""AmpliconClassifier gene names -> NCBI Gene IDs, from the committed gene map.

The map (``caper/gene_map/``) is built by ``build_gene_map.py`` from the data
repo's gene GFFs, UCSC refLink and NCBI gene_info/gene_history; that script's
docstring explains the resolution order and the measurements behind it.  This
module only reads it.

Lookups are by (universe, upper-cased AC name).  The universe is the gene file
AC annotated with: ``hg19`` for hg19 and GRCh37 results, ``hg38`` for GRCh38
and GRCh38_viral -- the same grouping as ``feature_index.REFERENCE_EQUIVALENCE``.
A name is only meaningful inside its universe: the same spelling can name
different genes in the 2010 and current annotations.

Loaded once per process.  The two tables are about 43,000 and 93,000 rows, a
few MB in memory, which is why they are not a database collection: nothing has
to be written to prod for the site to know them, and a refresh is a commit.
"""

import bisect
import csv
import gzip
import json
import os
import re
from functools import lru_cache

GENE_MAP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gene_map')

# The reference builds whose results are named in each universe's gene file.
UNIVERSE_OF_BUILD = {'hg19': 'hg19', 'GRCh37': 'hg19', 'hg38': 'hg38', 'GRCh38': 'hg38', 'GRCh38_viral': 'hg38'}


@lru_cache(maxsize=1)
def _manifest():
    with open(os.path.join(GENE_MAP_DIR, 'SOURCES.json')) as handle:
        return json.load(handle)


def gene_map_version():
    """A short hash of the map's contents; changes whenever the map is rebuilt."""
    return _manifest()['version']


@lru_cache(maxsize=1)
def _ac_gene_table():
    """``{(universe, NAME): (gene_id or None, resolved_by)}`` and ``{gene_id: (symbol, full_name, status)}``."""
    by_name, genes = {}, {}
    with gzip.open(os.path.join(GENE_MAP_DIR, 'ac_gene_ids.tsv.gz'), 'rt', encoding='utf-8') as handle:
        for row in csv.DictReader(handle, delimiter='\t'):
            gene_id = int(row['gene_id']) if row['gene_id'] else None
            by_name[(row['universe'], row['ac_name'].upper())] = (gene_id, row['resolved_by'])
            if gene_id is not None:
                genes[gene_id] = (row['symbol'], row['full_name'], row['status'])
    return by_name, genes


def universe_of(reference_build):
    return UNIVERSE_OF_BUILD.get(str(reference_build or '').strip())


def gene_id_of(reference_build, name):
    """The Gene ID AC's ``name`` means in results on ``reference_build``, or None.

    None for a build with no human gene map (mm10), a name outside AC's
    universe for that build, or one of the few names that resolve to no single
    gene (``resolved_by`` is ``no_ncbi_record`` or starts with ``unresolved``).
    A gene NCBI has withdrawn keeps its retired Gene ID; see ``gene_status``.
    """
    universe = universe_of(reference_build)
    if universe is None:
        return None
    entry = _ac_gene_table()[0].get((universe, str(name).upper()))
    return entry[0] if entry else None


def gene_ids_of(reference_build, names):
    """Gene IDs for a list of AC names, deduplicated, in the order first seen."""
    seen = {}
    for name in names:
        gene_id = gene_id_of(reference_build, name)
        if gene_id is not None:
            seen.setdefault(gene_id, None)
    return list(seen)


def is_ac_name(reference_build, name):
    """Whether ``name`` is in AC's gene universe for that build at all."""
    universe = universe_of(reference_build)
    return universe is not None and (universe, str(name).upper()) in _ac_gene_table()[0]


def gene_record(gene_id):
    """``(NCBI symbol, full name)`` for a Gene ID in the map, or None.

    For a withdrawn gene the symbol is the last one NCBI gave it and the full
    name is empty: NCBI no longer describes it.
    """
    record = _ac_gene_table()[1].get(gene_id)
    return record[:2] if record else None


def gene_status(gene_id):
    """``'current'``, or ``'withdrawn'`` for a gene NCBI retired with no successor.

    Withdrawn genes are still real AC annotations -- 21 of the genes AC's two
    gene files name, measured 2026-10-29 -- so they stay searchable under their
    retired ID, and a page showing one should say NCBI no longer lists it.
    """
    record = _ac_gene_table()[1].get(gene_id)
    return record[2] if record else None


# ---------------------------------------------------------------------------
# What a typed name means.  Shared by the home-page search, the feature API and
# the autocomplete, so the three cannot disagree about it.
# ---------------------------------------------------------------------------

# How strongly a name points at a gene, strongest first.  A gene's current NCBI
# symbol is its preferred name (Jens, 2026-10-29); every other kind of match is
# weaker, and a name that is weak for several genes means none of them.
NAME_KIND_RANK = {'symbol': 0, 'ac_name': 1, 'withdrawn_symbol': 2, 'retired_symbol': 3, 'alias': 4}

_GENE_ID_TEXT = re.compile(r'^(?:GENEID:)?(\d+)$')


@lru_cache(maxsize=1)
def _name_table():
    """``{NAME: [(gene_id, kind, label), ...]}`` and the names sorted, for prefix lookups.

    ``NAME`` is upper-cased for matching; ``label`` is a real spelling of it
    (C17orf37) for showing.
    """
    by_name = {}
    with gzip.open(os.path.join(GENE_MAP_DIR, 'gene_names.tsv.gz'), 'rt', encoding='utf-8') as handle:
        for row in csv.DictReader(handle, delimiter='\t'):
            by_name.setdefault(row['name'], []).append((int(row['gene_id']), row['kind'], row['label']))
    return by_name, sorted(by_name)


@lru_cache(maxsize=1)
def _names_without_id():
    """Upper-cased AC names the map could not give a Gene ID, by universe."""
    return {key for key, (gene_id, _how) in _ac_gene_table()[0].items() if gene_id is None}


def _candidate(gene_id, matched=None, kind=None):
    symbol, full_name = gene_record(gene_id)
    entry = {'gene_id': gene_id, 'symbol': symbol, 'full_name': full_name,
             'status': gene_status(gene_id)}
    if matched is not None and matched.upper() != symbol.upper():
        entry['matched'] = matched
        entry['matched_as'] = kind
        entry['matched_label'] = MATCH_LABELS.get(kind, kind)
    return entry


# How a match is described to a visitor ("Gene 5170, alias of PDK1").
MATCH_LABELS = {'ac_name': 'AmpliconSuite name', 'withdrawn_symbol': 'former symbol',
                'retired_symbol': 'former symbol', 'alias': 'alias'}


def resolve_gene_term(term):
    """What one typed gene term means.

    Returns a dict whose ``outcome`` is one of:

    ``gene``       one gene: ``gene`` holds it (``gene_id``, ``symbol``, ...).
                   ``also_alias_of`` lists other genes the same text is an
                   alias of, when it is first of all this gene's own symbol
                   (PDK1 is also an alias of PDPK1).
    ``ambiguous``  the text names several genes and is no one's current
                   symbol (NEU; ABP1, AOC1's hg19 name and an alias of DBNL):
                   ``candidates`` lists them.  The web search asks the visitor
                   to pick; the API answers 400 with the list.
    ``name_only``  an AC gene name with no NCBI Gene ID (FLJ22763): search
                   it by name.
    ``not_tracked`` nothing AmpliconSuite annotates.
    """
    text = str(term or '').strip()
    upper = text.upper()
    match = _GENE_ID_TEXT.match(upper)
    if match and gene_record(int(match.group(1))):
        return {'outcome': 'gene', 'term': text, 'gene': _candidate(int(match.group(1)))}

    hits = _name_table()[0].get(upper, [])
    preferred = [gene_id for gene_id, kind, _label in hits if kind == 'symbol']
    others = sorted({gene_id for gene_id, kind, _label in hits if kind != 'symbol'} - set(preferred))
    best = {}   # gene_id -> (kind, label) of the strongest name the text matched
    for gene_id, kind, label in hits:
        if gene_id not in best or NAME_KIND_RANK[kind] < NAME_KIND_RANK[best[gene_id][0]]:
            best[gene_id] = (kind, label)
    candidate = lambda g: _candidate(g, best[g][1], best[g][0])

    if len(preferred) == 1:
        result = {'outcome': 'gene', 'term': text, 'gene': _candidate(preferred[0])}
        if others:
            result['also_alias_of'] = [candidate(g) for g in others]
        return result
    genes = sorted(set(preferred) | set(others))
    if len(genes) == 1:
        return {'outcome': 'gene', 'term': text, 'gene': candidate(genes[0])}
    if genes:
        return {'outcome': 'ambiguous', 'term': text, 'candidates': [candidate(g) for g in genes]}
    if any((universe, upper) in _names_without_id() for universe in ('hg19', 'hg38')):
        return {'outcome': 'name_only', 'term': text, 'name': upper}
    return {'outcome': 'not_tracked', 'term': text}


def not_tracked_message(term):
    return f'{term} is not tracked by AmpliconSuite annotations'


def ambiguous_message(resolution):
    names = ', '.join(f"{c['symbol']} (Gene {c['gene_id']})" for c in resolution['candidates'])
    return f"{resolution['term']} could mean several genes: {names}. Search for one of them by name."


# Autocomplete: suggestions start at this many characters; below it only an
# exact match is offered (AR, ALK).  Jens, 2026-10-29.
SUGGEST_MIN_PREFIX = 3
SUGGEST_LIMIT = 10


def suggest_genes(text, limit=SUGGEST_LIMIT):
    """Autocomplete entries for ``text``: one per gene, best match first.

    Ranked by how the typed text matches (exactly, then as a prefix), then by
    the kind of name it matched (current symbol, AC name, withdrawn symbol,
    retired symbol, alias -- gnomAD's ordering, where a gene whose own symbol
    matches outranks one that only matches through an alias), then by the
    shorter name.
    """
    upper = str(text or '').strip().upper()
    if not upper:
        return []
    by_name, names = _name_table()
    matched = []
    if upper in by_name:
        matched.append(upper)
    if len(upper) >= SUGGEST_MIN_PREFIX:
        start = bisect.bisect_left(names, upper)
        for name in names[start:]:
            if not name.startswith(upper):
                break
            if name != upper:
                matched.append(name)
    best = {}
    for name in matched:
        for gene_id, kind, label in by_name[name]:
            key = (name != upper, NAME_KIND_RANK[kind], len(name), name)
            if gene_id not in best or key < best[gene_id][0]:
                best[gene_id] = (key, label, kind)
    ranked = sorted(best.items(), key=lambda item: item[1][0])[:limit]
    return [_candidate(gene_id, label, kind) for gene_id, (_key, label, kind) in ranked]


def query_terms(genequery):
    """The gene terms of a search box query, split the way the search splits them.

    ``&`` wins over ``|`` wherever it appears (``search_index._gene_query``),
    and a term holding ``*`` is a wildcard, which stays a match on spelling.
    """
    text = str(genequery or '')
    separator = '&' if '&' in text else ('|' if '|' in text else None)
    terms = [t.strip() for t in text.split(separator)] if separator else [text.strip()]
    return [t for t in terms if t and '*' not in t]


def resolve_gene_query(genequery, resolve=None):
    """``resolve_gene_term`` (or ``resolve``) for every term of a query, in order."""
    resolve = resolve or resolve_gene_term
    return [resolve(term) for term in query_terms(genequery)]


@lru_cache(maxsize=1)
def _ac_names_by_gene():
    names = {}
    with gzip.open(os.path.join(GENE_MAP_DIR, 'ac_gene_ids.tsv.gz'), 'rt', encoding='utf-8') as handle:
        for row in csv.DictReader(handle, delimiter='\t'):
            if row['gene_id']:
                names.setdefault(int(row['gene_id']), {}).setdefault(row['universe'], []).append(row['ac_name'])
    return names


def ac_names_of(gene_id):
    """``{universe: [AC names]}`` -- what this gene is called in results of each build."""
    return _ac_names_by_gene().get(gene_id, {})


def other_names_note(gene_id):
    """``'called C17orf37 in hg19 results'`` when AC's names differ from the symbol, else ''."""
    record = gene_record(gene_id)
    if not record:
        return ''
    parts = [f"{', '.join(sorted(names))} in {universe} results"
             for universe, names in sorted(ac_names_of(gene_id).items())
             if [n.upper() for n in names] != [record[0].upper()]]
    return ('called ' + '; '.join(parts)) if parts else ''


def gene_resolution_notes(genequery, resolve=None):
    """What the search results page says about each gene term.

    Every resolution from ``resolve_gene_query``, with a ``note`` for a gene
    whose results use other names (``called C17orf37 in hg19 results``).  Terms
    that are simply a gene's own symbol and match it under no other name get
    no note and are left out, so an ordinary MYC search shows nothing extra.
    """
    notes = []
    for resolution in resolve_gene_query(genequery, resolve):
        if resolution['outcome'] == 'gene':
            gene = resolution['gene']
            gene['note'] = other_names_note(gene['gene_id'])
            if not (gene['note'] or gene.get('matched') or resolution.get('also_alias_of')
                    or gene['status'] == 'withdrawn'):
                continue
        elif resolution['outcome'] == 'ambiguous':
            for candidate in resolution['candidates']:
                candidate['note'] = other_names_note(candidate['gene_id'])
        notes.append(resolution)
    return notes
