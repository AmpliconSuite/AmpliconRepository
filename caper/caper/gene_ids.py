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

import csv
import gzip
import json
import os
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
    """``{(universe, NAME): (gene_id or None, resolved_by)}`` and ``{gene_id: (symbol, full_name)}``."""
    by_name, genes = {}, {}
    with gzip.open(os.path.join(GENE_MAP_DIR, 'ac_gene_ids.tsv.gz'), 'rt', encoding='utf-8') as handle:
        for row in csv.DictReader(handle, delimiter='\t'):
            gene_id = int(row['gene_id']) if row['gene_id'] else None
            by_name[(row['universe'], row['ac_name'].upper())] = (gene_id, row['resolved_by'])
            if gene_id is not None:
                genes[gene_id] = (row['symbol'], row['full_name'])
    return by_name, genes


def universe_of(reference_build):
    return UNIVERSE_OF_BUILD.get(str(reference_build or '').strip())


def gene_id_of(reference_build, name):
    """The Gene ID AC's ``name`` means in results on ``reference_build``, or None.

    None for a build with no human gene map (mm10), a name outside AC's
    universe for that build, or one of the few names that resolve to no single
    gene (``resolved_by`` starts with ``unresolved``).
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
    """``(current NCBI symbol, full name)`` for a Gene ID in the map, or None."""
    return _ac_gene_table()[1].get(gene_id)
