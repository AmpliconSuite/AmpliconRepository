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
