"""Gene names on the site resolve to NCBI Gene IDs through the committed gene map.

The map is data built by ``build_gene_map.py``; these tests pin what the site
relies on it for, and that the files are the ones the generator wrote.
"""

import csv
import gzip
import hashlib
import json
import os

from bson.objectid import ObjectId

from caper import feature_index, gene_ids
from caper.feature_index import feature_rows_for_project, gene_catalog_documents, project_digest
from caper.gene_ids import GENE_MAP_DIR, gene_id_of, gene_ids_of, gene_record, is_ac_name


def test_one_gene_under_its_names_in_both_builds():
    """C17orf37 is what the 2010 hg19 annotation calls MIEN1 (NCBI Gene 84299)."""
    assert gene_id_of('GRCh37', 'C17orf37') == 84299
    assert gene_id_of('hg19', 'C17ORF37') == 84299
    assert gene_id_of('GRCh38', 'MIEN1') == 84299
    assert gene_id_of('GRCh38_viral', 'MIEN1') == 84299
    # Different transcripts in the two builds, one gene.
    assert gene_id_of('hg19', 'MYC') == gene_id_of('hg38', 'MYC') == 4609
    assert gene_record(84299)[0] == 'MIEN1'


def test_names_outside_a_universe_resolve_to_nothing():
    assert gene_id_of('hg38', 'C17orf37') is None      # not an hg38 AC name
    assert gene_id_of('hg38', 'NOT_A_GENE') is None
    assert gene_id_of('mm10', 'Myc') is None           # no mouse map yet
    assert gene_id_of('', 'MYC') is None
    assert not is_ac_name('hg38', 'C17orf37') and is_ac_name('hg19', 'C17orf37')


def test_gene_ids_of_deduplicates_in_order():
    assert gene_ids_of('hg19', ['MYC', 'C17orf37', 'myc', 'NOT_A_GENE']) == [4609, 84299]


def _feature(sample, build, genes):
    return {'Sample_name': sample, 'Feature_ID': f'{sample}_amplicon1', 'Classification': 'ecDNA',
            'All_genes': genes, 'Oncogenes': [], 'Reference_version': build}


def test_index_rows_carry_gene_ids_by_their_own_build():
    project = {'_id': ObjectId(), 'project_name': 'p', 'private': 'public', 'project_members': [],
               'runs': {'old': [_feature('old', 'GRCh37', ["'C17orf37'", "'ERBB2'"])],
                        'new': [_feature('new', 'GRCh38', ["'MIEN1'", "'ERBB2'"])],
                        'mouse': [_feature('mouse', 'mm10', ["'Myc'"])],
                        'clean': []}}
    rows = {row['run_key']: row for row in feature_rows_for_project(project)}
    assert rows['old']['gene_ids'] == rows['new']['gene_ids'] == [84299, 2064]
    assert rows['mouse']['gene_ids'] == []
    assert rows['clean']['gene_ids'] == []
    assert rows['old']['genes'] == ['C17ORF37', 'ERBB2']   # name search unchanged


def test_catalogue_records_the_gene_id_per_name():
    rows = [{'genes': ['C17ORF37'], 'oncogenes': [], 'reference_build': 'hg19'},
            {'genes': ['MIEN1'], 'oncogenes': [], 'reference_build': 'hg38'},
            {'genes': ['NOT_A_GENE'], 'oncogenes': [], 'reference_build': 'hg38'}]
    catalogue = gene_catalog_documents(rows)
    assert catalogue['C17ORF37']['gene_ids'] == catalogue['MIEN1']['gene_ids'] == [84299]
    assert catalogue['NOT_A_GENE']['gene_ids'] == []


def test_a_rebuilt_map_makes_every_project_stale(monkeypatch):
    project = {'_id': ObjectId(), 'project_name': 'p', 'runs': {}}
    before = project_digest(project)
    monkeypatch.setattr(feature_index, 'gene_map_version', lambda: 'different')
    assert project_digest(project) != before


def test_map_files_are_the_ones_the_generator_wrote():
    """SOURCES.json's version is a hash of the two tables; a hand edit breaks it."""
    with open(os.path.join(GENE_MAP_DIR, 'SOURCES.json')) as handle:
        manifest = json.load(handle)
    blobs = b''.join(open(os.path.join(GENE_MAP_DIR, name), 'rb').read()
                     for name in ('ac_gene_ids.tsv.gz', 'gene_names.tsv.gz'))
    assert hashlib.sha1(blobs).hexdigest()[:12] == manifest['version'] == gene_ids.gene_map_version()


def test_no_two_ac_names_in_a_build_differ_only_by_case():
    """Lookups upper-case the name; two spellings folding together would be ambiguous."""
    seen = set()
    with gzip.open(os.path.join(GENE_MAP_DIR, 'ac_gene_ids.tsv.gz'), 'rt') as handle:
        for row in csv.DictReader(handle, delimiter='\t'):
            key = (row['universe'], row['ac_name'].upper())
            assert key not in seen, key
            seen.add(key)


def test_genes_ncbi_withdrew_keep_their_retired_id():
    """CRIPAK's transcript belongs to Gene 285464, withdrawn in 2019 with no successor."""
    from caper.gene_ids import gene_status
    assert gene_id_of('hg19', 'CRIPAK') == 285464
    assert gene_status(285464) == 'withdrawn'
    assert gene_status(84299) == 'current'


def test_transcripts_refLink_lacks_are_placed_by_ncbi():
    """DEC1's transcript NM_017418 is NCBI Gene 50514, now called DELEC1."""
    assert gene_id_of('hg19', 'DEC1') == 50514
    assert gene_record(50514)[0] == 'DELEC1'


def test_the_chromosome_settles_a_shared_symbol():
    """Two current genes are called RNR1; AC's hg38 file puts its RNR1 on chrM."""
    assert gene_id_of('hg38', 'RNR1') == 4549
    assert gene_id_of('hg38', 'RNR2') == 4550


def test_a_name_with_no_ncbi_record_is_left_unmapped():
    assert gene_id_of('hg19', 'FLJ22763') is None
    assert is_ac_name('hg19', 'FLJ22763')


# --- What a typed name means (phase 2) -------------------------------------

from caper.gene_ids import (  # noqa: E402
    gene_resolution_notes, query_terms, resolve_gene_term, suggest_genes)


def test_a_current_symbol_is_its_gene_even_when_it_is_also_an_alias():
    """PDK1 is PDK1's symbol and an alias of PDPK1: the symbol wins, the alias is reported."""
    result = resolve_gene_term('pdk1')
    assert result['outcome'] == 'gene' and result['gene']['symbol'] == 'PDK1'
    assert [c['symbol'] for c in result['also_alias_of']] == ['PDPK1']


def test_an_old_name_or_alias_of_one_gene_resolves_to_it():
    assert resolve_gene_term('C17orf37')['gene']['symbol'] == 'MIEN1'
    assert resolve_gene_term('HER2')['gene']['symbol'] == 'ERBB2'
    assert resolve_gene_term('84299')['gene']['symbol'] == 'MIEN1'
    assert resolve_gene_term('GeneID:2064')['gene']['symbol'] == 'ERBB2'


def test_a_name_with_no_single_preferred_meaning_is_ambiguous():
    neu = resolve_gene_term('NEU')
    assert neu['outcome'] == 'ambiguous'
    assert {'ERBB2', 'NEU1', 'NEURL1'} <= {c['symbol'] for c in neu['candidates']}
    # AOC1's hg19 name, and an alias of DBNL today: no one's current symbol.
    assert resolve_gene_term('ABP1')['outcome'] == 'ambiguous'


def test_untracked_and_unmapped_names():
    assert resolve_gene_term('FLJ12345')['outcome'] == 'not_tracked'
    assert resolve_gene_term('FLJ22763') == {'outcome': 'name_only', 'term': 'FLJ22763', 'name': 'FLJ22763'}


def test_query_terms_split_like_the_search():
    assert query_terms('MYC & egfr') == ['MYC', 'egfr']
    assert query_terms('MYC|EGFR') == ['MYC', 'EGFR']
    assert query_terms('MYC|EGFR&CDK4') == ['MYC|EGFR', 'CDK4']   # & wins, as in search_index
    assert query_terms('MYC*') == ['MYC*']


def test_gene_search_has_no_wildcards():
    assert resolve_gene_term('MYC*') == {'outcome': 'wildcard', 'term': 'MYC*'}
    [note] = gene_resolution_notes('MYC*')
    assert note['outcome'] == 'wildcard'
    assert [n['outcome'] for n in gene_resolution_notes('EGFR&MYC*')] == ['wildcard']


def test_mixing_and_with_or_is_refused():
    """MYC|EGFR&CDK4 used to search for a gene named 'MYC|EGFR'."""
    assert gene_resolution_notes('MYC|EGFR&CDK4') == [{'outcome': 'mixed_operators', 'term': 'MYC|EGFR&CDK4'}]


def test_results_mark_every_name_of_the_searched_gene():
    from caper.gene_ids import highlight_names
    assert {'MIEN1', 'C17ORF37'} <= set(highlight_names('C17orf37'))
    assert 'ERBB2' in highlight_names('HER2&MYC') and 'MYC' in highlight_names('HER2&MYC')


def test_suggestions_start_at_three_characters_or_an_exact_name():
    assert [s['symbol'] for s in suggest_genes('AR')][:1] == ['AR']
    assert all(s['symbol'] == 'AR' or s.get('matched') == 'AR' for s in suggest_genes('AR'))
    assert suggest_genes('MY') == []                              # no exact MY, too short for a prefix
    myc = [s['symbol'] for s in suggest_genes('MYC')]
    assert myc[0] == 'MYC' and 'MYCN' in myc and len(myc) <= 10


def test_suggestions_rank_a_symbol_above_an_alias_match():
    ranked = [s['symbol'] for s in suggest_genes('PDK1')]
    assert ranked.index('PDK1') < ranked.index('PDPK1')


def test_notes_skip_plain_symbols_and_explain_the_rest():
    assert gene_resolution_notes('MYC') == []
    [note] = gene_resolution_notes('C17orf37')
    assert note['gene']['symbol'] == 'MIEN1' and 'C17orf37' in note['gene']['note']


def test_an_alias_never_merges_two_genes_ac_names_separately():
    """ZASP (chr7) is an alias of LDB3 (chr10), but AC's hg38 file names both."""
    from caper.gene_ids import gene_status
    assert gene_id_of('hg38', 'ZASP') != gene_id_of('hg38', 'LDB3')
    assert gene_status(gene_id_of('hg38', 'ZASP')) == 'withdrawn'


def test_ncbi_merges_are_followed():
    """NCBI merged C2orf48 into RRM2; AC's 2010 file still names both."""
    assert gene_id_of('hg19', 'C2orf48') == gene_id_of('hg19', 'RRM2') == 6241


def test_a_reissued_record_beats_the_withdrawn_one():
    """NCBI withdrew PPP1R2P1's old record and re-issued the gene under a new one."""
    from caper.gene_ids import gene_status
    assert gene_status(gene_id_of('hg19', 'PPP1R2P1')) == 'current'


def test_an_amplicon_suite_name_says_which_builds_use_it():
    """C17orf37 is MIEN1's name in hg19 results; hg38 results call it MIEN1."""
    [first] = suggest_genes('C17orf37')[:1]
    assert first['symbol'] == 'MIEN1' and first['matched_builds'] == ['hg19']
    assert first['annotated_in'] == ['hg19', 'hg38']


def test_a_gene_one_build_never_reports_is_flagged():
    """ACOD1 is in AC's hg38 gene file only, CT62 in its hg19 file only."""
    [acod1] = gene_resolution_notes('ACOD1')
    assert acod1['gene']['annotated_in'] == ['hg38'] and acod1['gene']['missing_in'] == ['hg19']
    [ct62] = gene_resolution_notes('CT62')
    assert ct62['gene']['missing_in'] == ['hg38']
    assert gene_resolution_notes('MYC') == []      # in both builds: still no note
