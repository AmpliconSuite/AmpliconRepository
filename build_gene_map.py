#!/usr/bin/env python3
"""Build the table that maps AmpliconClassifier's gene names to NCBI Gene IDs.

AmpliconClassifier can only report genes named in the data repo's gene GFF for
the reference it ran on, so every gene name on the site comes from one of two
files: ``Genes_July_2010_hg19.gff`` (hg19 and GRCh37) and ``Genes_hg38.gff``
(GRCh38 and GRCh38_viral).  Measured 2026-10-29 over the 35 public projects,
every one of their 148,699 per-project gene names is in one of those two
universes.  So the site maps names, not projects: one row per (universe, AC
name), built once from those files and committed.

Each AC name resolves to a Gene ID in two passes (``resolve_universe``):

1. The transcript AC reports for the name (the GFF's ``Accession=``) and the
   current gene it belongs to: by UCSC ``hgFixed.refLink`` (``mrnaAcc`` ->
   ``locusLinkId``, ``transcript``; 96.7% of hg19 names and 99.7% of hg38
   names on 2026-10-29), else by NCBI's own record of the accession
   (``ncbi_transcript``), looked up once and cached in the committed
   ``transcript_lookups.json`` so a rebuild needs no network and gives the
   same answer.  Retired IDs are carried forward through NCBI ``gene_history``.
   This is what AC literally reported, and it outranks anything inferred.
2. The rest, by name: the current NCBI symbol, a retired symbol's successor
   (NCBI's record of a merge), or an alias -- each only when it leaves one
   gene, with the chromosome AC placed the name on as the tie-break
   (``*_by_chromosome``: RNR1 and RNR2 are chrM in AC's hg38 file).  An alias
   may not take a gene that pass 1 gave to another name in the same file, or
   two distinct AC genes would be merged (ZASP, chr7, is not LDB3, chr10).
   When the transcript belongs to a gene NCBI withdrew with no successor, a
   current gene found by name wins only on the same chromosome -- NCBI
   re-issues some genes under a new record without recording a successor
   (PPP1R2P1, SNORA11E) -- and otherwise the name keeps the withdrawn
   record's retired Gene ID (``withdrawn``; CRIPAK, HPVC1, SPHAR...), which
   is still unique and still NCBI's.  A withdrawn symbol with one record does
   the same (``withdrawn_symbol``; OCLM).

Anything else is ``no_ncbi_record`` or ``unresolved_ambiguous_*``: still
searchable by its exact name, never guessed.  On 2026-10-29 that is two names
of 43,359 (FLJ22763 and FLJ40292, which NCBI has no record of at all).

Outputs, in ``caper/gene_map/``:

``ac_gene_ids.tsv.gz``  universe, ac_name, transcript, chromosome, gene_id,
                        resolved_by, status (current/withdrawn), symbol,
                        full_name
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
    python build_gene_map.py --offline          # never call NCBI; use the cache

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
import re
import sys
import time
import urllib.parse
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

# Names as NCBI spells them, for display: {'synonym': {spelling: {gene ids}},
# 'retired': {spelling: {successor gene ids}}}.  Filled while reading.
SPELLINGS = {'synonym': {}, 'retired': {}}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'caper', 'gene_map')


def gene_file(data_repo, build):
    with open(os.path.join(data_repo, build, 'file_list.txt')) as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == 'gene_filename':
                return os.path.join(data_repo, build, fields[1])
    sys.exit(f'No gene_filename in {data_repo}/{build}/file_list.txt')


def normalize_chromosome(value):
    value = str(value)
    value = value[3:] if value.lower().startswith('chr') else value
    return 'MT' if value in ('M', 'MT') else value


def ac_names(path):
    """``{name: (unversioned accession, chromosome)}`` exactly as ``ac_io.parse_genes`` reads it.

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
            names.setdefault(name, (accession.split('.')[0], normalize_chromosome(fields[0])))
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
    genes, by_symbol, by_synonym, chromosome = {}, collections.defaultdict(set), collections.defaultdict(set), {}
    with gzip.open(path, 'rt') as handle:
        for line in handle:
            if line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            gene_id = int(fields[1])
            genes[gene_id] = (fields[2], fields[11] if fields[11] != '-' else fields[8])
            chromosome[gene_id] = fields[6]
            by_symbol[fields[2].upper()].add(gene_id)
            if fields[4] != '-':
                for synonym in fields[4].split('|'):
                    by_synonym[synonym.upper()].add(gene_id)
                    SPELLINGS['synonym'].setdefault(synonym, set()).add(gene_id)
    return genes, by_symbol, by_synonym, chromosome


def read_history(path):
    """Human retirements.

    ``successor``         {retired GeneID: successor GeneID or None}
    ``retired_symbols``   {retired SYMBOL: {successor GeneIDs}}
    ``withdrawn``         {retired GeneID: (its last symbol, date)} for those with no successor
    ``withdrawn_symbols`` {SYMBOL: {retired GeneIDs with no successor}}
    """
    successor, retired_symbols = {}, collections.defaultdict(set)
    withdrawn, withdrawn_symbols = {}, collections.defaultdict(set)
    with gzip.open(path, 'rt') as handle:
        for line in handle:
            if not line.startswith(HUMAN + '\t'):
                continue
            fields = line.rstrip('\n').split('\t')
            new = int(fields[1]) if fields[1] != '-' else None
            successor[int(fields[2])] = new
            if new is not None:
                retired_symbols[fields[3].upper()].add(new)
                SPELLINGS['retired'].setdefault(fields[3], set()).add(new)
            else:
                withdrawn[int(fields[2])] = (fields[3], fields[4])
                withdrawn_symbols[fields[3].upper()].add(int(fields[2]))
    return successor, retired_symbols, withdrawn, withdrawn_symbols


def forward(gene_id, successor, genes):
    """Follow retirements to a current Gene ID, or None if the chain ends withdrawn."""
    seen = set()
    while gene_id is not None and gene_id not in genes and gene_id not in seen:
        seen.add(gene_id)
        gene_id = successor.get(gene_id)
    return gene_id if gene_id in genes else None


def withdrawn_end(gene_id, successor):
    """The last retired ID on a chain that ends withdrawn (no successor), else None."""
    seen = set()
    while gene_id is not None and gene_id not in seen:
        seen.add(gene_id)
        if gene_id not in successor:
            return None
        if successor[gene_id] is None:
            return gene_id
        gene_id = successor[gene_id]
    return None


EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'


def _eutils(path, as_json=True):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(EUTILS + path, timeout=60) as response:
                body = response.read().decode('utf-8', 'replace')
            time.sleep(0.4)   # NCBI asks for at most 3 requests a second without a key
            return json.loads(body) if as_json else body
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'NCBI E-utilities did not answer: {path}')


def ncbi_genes_for_transcript(accession):
    """NCBI's Gene IDs for a RefSeq accession, current or retired, or []."""
    term = urllib.parse.quote(f'{accession}[accn]')
    ids = _eutils(f'esearch.fcgi?db=gene&retmode=json&term={term}')['esearchresult']['idlist']
    if ids:
        return sorted(int(i) for i in ids), 'gene_accn'
    nuc = _eutils(f'esearch.fcgi?db=nuccore&retmode=json&term={term}')['esearchresult']['idlist']
    if nuc:
        record = _eutils(f'efetch.fcgi?db=nuccore&rettype=gb&retmode=text&id={nuc[0]}', as_json=False)
        xrefs = sorted({int(x) for x in re.findall(r'/db_xref="GeneID:(\d+)"', record)})
        if xrefs:
            return xrefs, 'nuccore_xref'
    return [], 'none'


def lookup_transcripts(accessions, cache_path, offline):
    """``{accession: [gene ids]}`` for accessions refLink cannot place, cached on disk."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path) as handle:
            cache = json.load(handle)
    todo = sorted(a for a in set(accessions) if a not in cache)
    if todo and offline:
        sys.exit(f'{len(todo)} transcripts are not in {cache_path}; run without --offline.')
    for n, accession in enumerate(todo, 1):
        gene_ids, via = ncbi_genes_for_transcript(accession)
        cache[accession] = {'gene_ids': gene_ids, 'via': via, 'checked': datetime.date.today().isoformat()}
        if n % 50 == 0:
            print(f'  {n}/{len(todo)} transcripts looked up at NCBI', file=sys.stderr)
    with open(cache_path, 'w') as handle:
        json.dump(dict(sorted(cache.items())), handle, indent=1, sort_keys=True)
        handle.write('\n')
    return {a: cache[a]['gene_ids'] for a in set(accessions)}


def transcript_gene(accession, ncbi, ref):
    """``(current gene, withdrawn gene)`` the transcript AC reported belongs to.

    refLink first, then NCBI's own record (``ncbi``).  Either may be None.
    """
    genes, successor = ref['genes'], ref['successor']
    for how, candidates in (('transcript', [ref['accession_gene'][accession]] if accession in ref['accession_gene'] else []),
                            ('ncbi_transcript', ncbi.get(accession, []))):
        current = {forward(g, successor, genes) for g in candidates} - {None}
        if len(current) == 1:
            return (current.pop(), how), None
    retired = {withdrawn_end(g, successor)
               for g in [ref['accession_gene'].get(accession)] + ncbi.get(accession, [])} - {None}
    return None, (retired.pop() if len(retired) == 1 else None)


def name_gene(name, chromosome, claimed, ref):
    """``(current gene or None, how)`` from the name alone.

    ``claimed`` holds the genes other AC names in the same file own by
    transcript; an alias match may not take one, or two distinct AC genes would
    be merged -- the old map sent ZASP (chr7) to LDB3 (chr10) by alias while
    the same file names LDB3 separately.  A name matching several genes is
    settled by the chromosome AC placed it on, when that leaves one.
    """
    genes, successor = ref['genes'], ref['successor']
    upper = name.upper()
    on_chromosome = lambda ids: {g for g in ids if ref['chromosome'].get(g) == chromosome}
    retired_successors = {forward(g, successor, genes) for g in ref['retired_symbols'].get(upper, set())} - {None}
    for kind, found in (('symbol', ref['by_symbol'].get(upper, set())),
                        ('retired_symbol', retired_successors),
                        ('alias', ref['by_synonym'].get(upper, set()))):
        # Only an alias is held back from a claimed gene.  A symbol is NCBI's
        # name for the gene, and a retired symbol's successor is NCBI's own
        # record of a merge (C2orf48 into RRM2), so two AC names reaching one
        # gene that way are one gene by NCBI's account.
        candidates = set(found) - claimed if kind == 'alias' else set(found)
        if len(candidates) == 1:
            return next(iter(candidates)), kind
        if len(on_chromosome(candidates)) == 1:
            return on_chromosome(candidates).pop(), f'{kind}_by_chromosome'
        if candidates:
            return None, f'unresolved_ambiguous_{kind}'
    return None, None


def resolve_universe(names, ncbi, ref):
    """``{name: (gene_id or None, how)}`` for one gene file, in two passes.

    1. Every name whose transcript belongs to a current gene takes it.  This is
       what AC literally reported and it outranks anything inferred.
    2. The rest, by name (``name_gene``), but never taking a gene pass 1 gave
       to another name.  Against a transcript that belongs to a withdrawn
       record, a current gene wins only when it sits on the chromosome AC
       placed the name on: NCBI re-issues some genes under a new record without
       recording a successor (PPP1R2P1, SNORA11E), and the live record carrying
       the same name at the same place is that gene.  Otherwise the withdrawn
       record's retired ID is kept, then a withdrawn symbol's, then nothing.
    """
    resolved, claimed, pending = {}, set(), {}
    for name, (accession, chromosome) in names.items():
        current, withdrawn = transcript_gene(accession, ncbi, ref)
        if current:
            resolved[name] = current
            claimed.add(current[0])
        else:
            pending[name] = (chromosome, withdrawn)
    for name, (chromosome, withdrawn) in pending.items():
        gene_id, how = name_gene(name, chromosome, claimed - {None}, ref)
        if gene_id is not None and (withdrawn is None or ref['chromosome'].get(gene_id) == chromosome):
            resolved[name] = (gene_id, how)
        elif withdrawn is not None:
            resolved[name] = (withdrawn, 'withdrawn')
        elif len(ref['withdrawn_symbols'].get(name.upper(), set())) == 1:
            resolved[name] = (next(iter(ref['withdrawn_symbols'][name.upper()])), 'withdrawn_symbol')
        else:
            resolved[name] = (None, how or 'no_ncbi_record')
    return resolved


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
    parser.add_argument('--offline', action='store_true',
                        help='use only the cached NCBI transcript lookups; fail if one is missing')
    args = parser.parse_args(argv)
    if not args.data_repo:
        sys.exit('Set AA_DATA_REPO or pass --data-repo.')

    sources = fetch_sources(args.sources)
    ref = {'accession_gene': read_reflink(os.path.join(args.sources, 'refLink.txt.gz'))}
    ref['genes'], ref['by_symbol'], ref['by_synonym'], ref['chromosome'] = read_gene_info(
        os.path.join(args.sources, 'Homo_sapiens.gene_info.gz'))
    ref['successor'], ref['retired_symbols'], ref['withdrawn'], ref['withdrawn_symbols'] = read_history(
        os.path.join(args.sources, 'gene_history.gz'))
    genes, by_synonym, successor = ref['genes'], ref['by_synonym'], ref['successor']
    ref['synonym_spellings'], ref['retired_spellings'] = SPELLINGS['synonym'], SPELLINGS['retired']
    retired_symbols = ref['retired_symbols']

    universes = {universe: ac_names(gene_file(args.data_repo, build)) for universe, build in UNIVERSES.items()}
    # Only the transcripts refLink cannot place go to NCBI; the answers are cached
    # in the committed transcript_lookups.json.
    unplaced = [accession for names in universes.values() for accession, _ in names.values()
                if forward(ref['accession_gene'].get(accession), successor, genes) is None]
    ncbi = lookup_transcripts(unplaced, os.path.join(args.out, 'transcript_lookups.json'), args.offline)

    map_rows, counts, gff_info = [], {}, {}
    names_by_gene = collections.defaultdict(set)
    for universe, build in UNIVERSES.items():
        path = gene_file(args.data_repo, build)
        gff_info[universe] = {'path': os.path.relpath(path, args.data_repo), 'md5': md5(path)}
        tally = collections.Counter()
        resolved = resolve_universe(universes[universe], ncbi, ref)
        for name, (accession, chromosome) in sorted(universes[universe].items()):
            gene_id, how = resolved[name]
            tally[how] += 1
            if gene_id in genes:
                status, (symbol, full_name) = 'current', genes[gene_id]
            elif gene_id is not None:
                status, symbol, full_name = 'withdrawn', ref['withdrawn'][gene_id][0], ''
            else:
                status, symbol, full_name = '', '', ''
            map_rows.append((universe, name, accession, chromosome, gene_id or '', how, status, symbol, full_name))
            if gene_id:
                names_by_gene[gene_id].add((name, 'ac_name'))
                names_by_gene[gene_id].add((symbol, 'symbol' if status == 'current' else 'withdrawn_symbol'))
        counts[universe] = dict(sorted(tally.items()))

    # Every name a visitor might type for a gene in the AC universe.
    in_universe = set(names_by_gene)
    for spelled, gene_ids in ref['synonym_spellings'].items():
        for gene_id in gene_ids & in_universe:
            names_by_gene[gene_id].add((spelled, 'alias'))
    for spelled, gene_ids in ref['retired_spellings'].items():
        for gene_id in {forward(g, successor, genes) for g in gene_ids} & in_universe:
            names_by_gene[gene_id].add((spelled, 'retired_symbol'))
    # One row per (name, gene); when a name reaches a gene several ways, the
    # strongest kind is kept, in the order a search ranks them.
    rank = {'symbol': 0, 'ac_name': 1, 'withdrawn_symbol': 2, 'retired_symbol': 3, 'alias': 4}
    # Matched upper-cased; ``label`` keeps a real spelling to show (C17orf37,
    # not C17ORF37), from the strongest kind of name that spelled it.
    best = {}
    for gene_id, entries in names_by_gene.items():
        for spelled, kind in entries:
            key = (spelled.upper(), gene_id)
            if key not in best or rank[kind] < rank[best[key][0]]:
                best[key] = (kind, spelled)
    name_rows = sorted((upper, gene_id, kind, label) for (upper, gene_id), (kind, label) in best.items())

    map_blob = tsv_gz(map_rows, ('universe', 'ac_name', 'transcript', 'chromosome', 'gene_id', 'resolved_by',
                                 'status', 'symbol', 'full_name'))
    names_blob = tsv_gz(name_rows, ('name', 'gene_id', 'kind', 'label'))
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
        'withdrawn_genes': sum(1 for g in in_universe if g not in genes),
        'transcripts_looked_up_at_ncbi': len(set(unplaced)),
        'name_rows': len(name_rows),
    }
    with open(os.path.join(args.out, 'SOURCES.json'), 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print(json.dumps({k: manifest[k] for k in ('version', 'resolution', 'genes_in_universe', 'withdrawn_genes',
                                               'transcripts_looked_up_at_ncbi', 'name_rows')}, indent=2))


if __name__ == '__main__':
    main()
