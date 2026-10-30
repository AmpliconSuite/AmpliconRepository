#!/usr/bin/env python3
"""Build the table that maps AmpliconClassifier's gene names to NCBI Gene IDs.

AmpliconClassifier can only report genes named in the data repo's gene GFF for
the reference it ran on, so every gene name on the site comes from one of two
files: ``Genes_July_2010_hg19.gff`` (hg19 and GRCh37) and ``Genes_hg38.gff``
(GRCh38 and GRCh38_viral).  Measured 2026-10-29 over the 35 public projects,
every one of their 148,699 per-project gene names is in one of those two
universes.  So the site maps names, not projects: one row per (universe, AC
name), built once from those files and committed.

Each AC name resolves to a Gene ID through the first step that gives exactly
one answer:

1. ``transcript``: the RefSeq transcript AC reports for that name (the GFF's
   ``Accession=``), through UCSC ``hgFixed.refLink`` (``mrnaAcc`` ->
   ``locusLinkId``), carried forward through NCBI ``gene_history`` when the ID
   was retired.  96.7% of hg19 names and 99.7% of hg38 names (2026-10-29).
2. ``symbol``: the name is the current NCBI symbol of exactly one gene.
3. ``retired_symbol``: the name is a discontinued NCBI symbol with exactly one
   current successor.
4. ``alias``: the name is an NCBI synonym of exactly one gene.

Anything else is ``unresolved``: still searchable by its exact name, never
guessed.

Outputs, in ``caper/gene_map/``:

``ac_gene_ids.tsv.gz``  universe, ac_name, transcript, gene_id, resolved_by,
                        symbol, full_name
``gene_names.tsv.gz``   name (upper-cased), gene_id, kind -- every name a
                        visitor might type for a gene in the AC universe:
                        the current symbol, AC names, retired symbols, aliases
``SOURCES.json``        where each input came from, its md5 and date, the
                        counts above, and ``version`` -- a hash of the two
                        tables, folded into the search index's digests so a
                        refreshed map shows up as drift until reindexed

Usage::

    python build_gene_map.py                    # downloads the NCBI/UCSC files
    python build_gene_map.py --sources DIR      # or reads them from DIR

``$AA_DATA_REPO`` (or ``--data-repo``) must hold GRCh37 and GRCh38.
"""

import argparse
import collections
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import sys
import urllib.request

SOURCE_URLS = {
    'refLink.txt.gz': 'https://hgdownload.soe.ucsc.edu/goldenPath/hgFixed/database/refLink.txt.gz',
    'Homo_sapiens.gene_info.gz': 'https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz',
    'gene_history.gz': 'https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_history.gz',
}

# The universe each reference's results are named in, and the data repo build
# whose file_list.txt names that universe's gene file.  hg19 and GRCh37 share
# one file, as do GRCh38 and GRCh38_viral (data repo file_list.txt, 2026-10-29),
# which is also how feature_index.REFERENCE_EQUIVALENCE groups them.
UNIVERSES = {'hg19': 'GRCh37', 'hg38': 'GRCh38'}

HUMAN = '9606'
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper', 'gene_map')


def gene_file(data_repo, build):
    with open(os.path.join(data_repo, build, 'file_list.txt')) as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == 'gene_filename':
                return os.path.join(data_repo, build, fields[1])
    sys.exit(f'No gene_filename in {data_repo}/{build}/file_list.txt')


def ac_names(path):
    """``{name: unversioned accession}`` exactly as ``ac_io.parse_genes`` reads it.

    First transcript per name; names starting LOC, LINC or MIR skipped (AC's
    default, ``keep_all=False``).  Mirrors AC 2.0.1.
    """
    names = {}
    with open(path) as handle:
        for line in handle:
            if line.startswith('#'):
                continue
            fields = line.rstrip().split()
            if not fields:
                continue
            props = {x.split('=')[0]: x.split('=')[1] for x in fields[-1].rstrip(';').split(';')}
            name = props.get('Name') or props.get('gene_name')
            accession = props.get('Accession') or props.get('ID')
            if name.startswith(('LOC', 'LINC', 'MIR')):
                continue
            names.setdefault(name, accession.split('.')[0])
    return names


def md5(path):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch_sources(directory):
    os.makedirs(directory, exist_ok=True)
    info = {}
    for name, url in SOURCE_URLS.items():
        path = os.path.join(directory, name)
        last_modified = None
        if not os.path.exists(path):
            print(f'downloading {url}', file=sys.stderr)
            with urllib.request.urlopen(url, timeout=600) as response, open(path, 'wb') as out:
                last_modified = response.headers.get('Last-Modified')
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
        info[name] = {'url': url, 'md5': md5(path), 'bytes': os.path.getsize(path),
                      'last_modified': last_modified,
                      'file_mtime': datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds')}
    return info


def read_reflink(path):
    accession_gene = {}
    with gzip.open(path, 'rt', errors='replace') as handle:
        for line in handle:
            fields = line.rstrip('\n').split('\t')
            if len(fields) > 6 and fields[6] not in ('', '0'):
                accession_gene[fields[2]] = int(fields[6])
    return accession_gene


def read_gene_info(path):
    genes, by_symbol, by_synonym = {}, collections.defaultdict(set), collections.defaultdict(set)
    with gzip.open(path, 'rt') as handle:
        for line in handle:
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            gene_id = int(fields[1])
            genes[gene_id] = (fields[2], fields[11] if fields[11] != '-' else fields[8])
            by_symbol[fields[2].upper()].add(gene_id)
            if fields[4] != '-':
                for synonym in fields[4].split('|'):
                    by_synonym[synonym.upper()].add(gene_id)
    return genes, by_symbol, by_synonym


def read_history(path):
    """Human ``{retired GeneID: successor or None}`` and ``{retired SYMBOL: {successors}}``."""
    successor, retired_symbols = {}, collections.defaultdict(set)
    with gzip.open(path, 'rt') as handle:
        for line in handle:
            if not line.startswith(HUMAN + '\t'):
                continue
            fields = line.rstrip('\n').split('\t')
            new = int(fields[1]) if fields[1] != '-' else None
            successor[int(fields[2])] = new
            if new is not None:
                retired_symbols[fields[3].upper()].add(new)
    return successor, retired_symbols


def forward(gene_id, successor, genes):
    """Follow retirements to a current Gene ID, or None if the chain ends withdrawn."""
    seen = set()
    while gene_id is not None and gene_id not in genes and gene_id not in seen:
        seen.add(gene_id)
        gene_id = successor.get(gene_id)
    return gene_id if gene_id in genes else None


def resolve(name, accession, accession_gene, genes, by_symbol, by_synonym, successor, retired_symbols):
    gene_id = forward(accession_gene.get(accession), successor, genes)
    if gene_id is not None:
        return gene_id, 'transcript'
    upper = name.upper()
    for kind, candidates in (('symbol', by_symbol.get(upper, set())),
                             ('retired_symbol', {forward(g, successor, genes) for g in retired_symbols.get(upper, set())} - {None}),
                             ('alias', by_synonym.get(upper, set()))):
        if len(candidates) == 1:
            return next(iter(candidates)), kind
        if candidates:
            return None, f'unresolved_ambiguous_{kind}'
    return None, 'unresolved'


def tsv_gz(rows, header):
    text = io.StringIO()
    writer = csv.writer(text, delimiter='\t', lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    # mtime=0 so the same table always gives the same bytes and the same version.
    return gzip.compress(text.getvalue().encode('utf-8'), mtime=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--data-repo', default=os.environ.get('AA_DATA_REPO'))
    parser.add_argument('--sources', default=os.path.join(OUT_DIR, '.sources'),
                        help='directory holding (or to download) refLink, gene_info and gene_history')
    parser.add_argument('--out', default=OUT_DIR)
    args = parser.parse_args(argv)
    if not args.data_repo:
        sys.exit('Set AA_DATA_REPO or pass --data-repo.')

    sources = fetch_sources(args.sources)
    accession_gene = read_reflink(os.path.join(args.sources, 'refLink.txt.gz'))
    genes, by_symbol, by_synonym = read_gene_info(os.path.join(args.sources, 'Homo_sapiens.gene_info.gz'))
    successor, retired_symbols = read_history(os.path.join(args.sources, 'gene_history.gz'))

    map_rows, counts, gff_info = [], {}, {}
    names_by_gene = collections.defaultdict(set)
    for universe, build in UNIVERSES.items():
        path = gene_file(args.data_repo, build)
        gff_info[universe] = {'path': os.path.relpath(path, args.data_repo), 'md5': md5(path)}
        tally = collections.Counter()
        for name, accession in sorted(ac_names(path).items()):
            gene_id, how = resolve(name, accession, accession_gene, genes, by_symbol, by_synonym,
                                   successor, retired_symbols)
            tally[how] += 1
            symbol, full_name = genes[gene_id] if gene_id else ('', '')
            map_rows.append((universe, name, accession, gene_id or '', how, symbol, full_name))
            if gene_id:
                names_by_gene[gene_id].add((name.upper(), 'ac_name'))
        counts[universe] = dict(sorted(tally.items()))

    # Every name a visitor might type for a gene in the AC universe.
    in_universe = set(names_by_gene)
    for gene_id in in_universe:
        names_by_gene[gene_id].add((genes[gene_id][0].upper(), 'symbol'))
    for upper, gene_ids in by_synonym.items():
        for gene_id in gene_ids & in_universe:
            names_by_gene[gene_id].add((upper, 'alias'))
    for upper, gene_ids in retired_symbols.items():
        for gene_id in {forward(g, successor, genes) for g in gene_ids} & in_universe:
            names_by_gene[gene_id].add((upper, 'retired_symbol'))
    # One row per (name, gene); when a name reaches a gene several ways, the
    # strongest kind is kept, in the order a search ranks them.
    rank = {'symbol': 0, 'ac_name': 1, 'retired_symbol': 2, 'alias': 3}
    best = {}
    for gene_id, entries in names_by_gene.items():
        for upper, kind in entries:
            key = (upper, gene_id)
            if key not in best or rank[kind] < rank[best[key]]:
                best[key] = kind
    name_rows = sorted((upper, gene_id, kind) for (upper, gene_id), kind in best.items())

    map_blob = tsv_gz(map_rows, ('universe', 'ac_name', 'transcript', 'gene_id', 'resolved_by', 'symbol', 'full_name'))
    names_blob = tsv_gz(name_rows, ('name', 'gene_id', 'kind'))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'ac_gene_ids.tsv.gz'), 'wb') as handle:
        handle.write(map_blob)
    with open(os.path.join(args.out, 'gene_names.tsv.gz'), 'wb') as handle:
        handle.write(names_blob)
    manifest = {
        'version': hashlib.sha1(map_blob + names_blob).hexdigest()[:12],
        'generated': datetime.date.today().isoformat(),
        'generator': 'build_gene_map.py',
        'gene_files': gff_info,
        'sources': sources,
        'resolution': counts,
        'genes_in_universe': len(in_universe),
        'name_rows': len(name_rows),
    }
    with open(os.path.join(args.out, 'SOURCES.json'), 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print(json.dumps({k: manifest[k] for k in ('version', 'resolution', 'genes_in_universe', 'name_rows')}, indent=2))


if __name__ == '__main__':
    main()
