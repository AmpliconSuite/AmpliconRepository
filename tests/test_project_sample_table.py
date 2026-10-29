"""
The project page's sample table: rows as JSON, filtered by gene, class or name.

The filtering itself is client-side and Jens tests it in a browser. What is
pinned here is the data it filters on -- every amplified gene per sample, and
classifications under the names the summary chart's bars carry -- and that the
template draws the table from that data rather than from server-rendered rows.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from caper.sample_table import sample_table_rows
from caper.summarybar import summarybar


TEMPLATE = Path(__file__).resolve().parents[1] / 'caper' / 'templates' / 'pages' / 'project.html'


def _feature(sample, classification, genes, oncogenes=()):
    return {'Sample_name': sample, 'Classification': classification,
            'All_genes': list(genes), 'Oncogenes': list(oncogenes),
            'Feature_ID': f'{sample}_{classification}'}


def test_rows_carry_every_amplified_gene():
    runs = {
        'S1': [_feature('S1', 'ecDNA', ['MYC', 'PVT1', 'CASC8'], ['MYC']),
               _feature('S1', 'Linear', ['ERBB2'], ['ERBB2'])],
        'S2': [_feature('S2', 'BFB', [])],
    }
    sample_data = [
        {'Sample_name': 'S1', 'Features': 2, 'Oncogenes': ['ERBB2', 'MYC'],
         'Classifications': ['ecDNA', 'Linear'], 'Classifications_counted': ['Linear', 'ecDNA']},
        {'Sample_name': 'S2', 'Features': 1, 'Oncogenes': [],
         'Classifications': ['BFB'], 'Classifications_counted': ['BFB']},
    ]
    rows = sample_table_rows(sample_data, runs)

    assert [r['n'] for r in rows] == ['S1', 'S2']
    assert rows[0]['g'] == 'CASC8 ERBB2 MYC PVT1'
    assert rows[0]['o'] == 'ERBB2, MYC'
    assert rows[0]['c'] == 'Linear, ecDNA'
    assert rows[0]['k'] == ['Linear', 'ecDNA']
    assert rows[1]['g'] == ''


def test_rows_survive_the_shapes_old_documents_hold():
    """NaN for a missing key (what the old pandas rollup stored), bare strings, None."""
    nan = float('nan')
    runs = {'S1': [{'Sample_name': 'S1', 'Classification': 'ecDNA', 'All_genes': 'MYCN',
                    'Oncogenes': nan},
                   {'Sample_name': 'S1', 'Classification': 'ecDNA', 'All_genes': None}]}
    sample_data = [{'Sample_name': 'S1', 'Features': nan, 'Oncogenes': nan,
                    'Classifications': ['ecDNA'], 'Classifications_counted': ['ecDNA (2)']}]
    [row] = sample_table_rows(sample_data, runs)

    assert row['g'] == 'MYCN'
    assert row['o'] == ''
    assert row['f'] == 0
    # json_script output must parse in a browser: no NaN.
    json.loads(json.dumps(row, allow_nan=False))


def test_bar_names_and_counts_are_the_rows_classifications():
    """A click on a bar must list exactly the samples that bar counted.

    The chart counts from feature rows and renames legacy classes; the rows'
    ``k`` must use the same names, or a click on "Linear" finds nothing in a
    project whose older samples say "Linear amplification".
    """
    features = [
        _feature('S1', 'Linear amplification', ['A']),
        _feature('S2', 'Linear', ['B']),
        _feature('S2', 'Complex non-cyclic', ['C']),
        _feature('S3', 'Complex-non-cyclic', ['D']),
        _feature('S3', 'ecDNA', ['E']),
        _feature('S3', 'ecDNA', ['F']),
    ]
    runs = {}
    for f in features:
        runs.setdefault(f['Sample_name'], []).append(f)
    sample_data = [
        {'Sample_name': s, 'Features': len(fs), 'Oncogenes': [],
         'Classifications': sorted({f['Classification'] for f in fs}),
         'Classifications_counted': []}
        for s, fs in runs.items()
    ]
    rows = sample_table_rows(sample_data, runs)

    html = summarybar(pd.DataFrame(features), {})
    bars = dict(re.findall(r'"name":"([^"]+)".*?"text":\["[\d.]+% \((\d+)\)"\]', html))
    assert bars == {'Linear': '2', 'Complex-non-cyclic': '2', 'ecDNA': '1'}

    for name, count in bars.items():
        assert sum(name in r['k'] for r in rows) == int(count), name


@pytest.mark.integration
def test_project_page_passes_the_rows(monkeypatch, request_factory):
    from caper import views

    project_id = '64b000000000000000000002'
    project = {
        '_id': project_id, 'linkid': project_id, 'project_name': 'sample-table-rows',
        'private': 'public', 'delete': False, 'current': True, 'FINISHED?': True,
        'project_members': [],
        'runs': {'S1': [_feature('S1', 'ecDNA', ['MYC', 'PVT1'], ['MYC'])]},
    }
    monkeypatch.setattr(views, 'get_one_project', lambda _id: project)
    monkeypatch.setattr(views, 'validate_project', lambda value, _name: value)
    monkeypatch.setattr(views, 'previous_versions', lambda _project: ([], None))
    monkeypatch.setattr(views, 'set_project_edit_OK_flag', lambda *_a: None)
    monkeypatch.setattr(views, 'initialize_ecDNA_context', lambda *_a: None)
    monkeypatch.setattr(views, 'reference_genome_from_project', lambda *_a: 'hg38')
    monkeypatch.setattr(views, 'create_aggregate_df',
                        lambda *_a: (pd.DataFrame(), '/tmp/unused-sample-table.csv'))
    monkeypatch.setattr(views, 'get_cached_chart', lambda *_a: '')
    monkeypatch.setattr(views, 'session_visit', lambda *_a: (0, 0))
    monkeypatch.setattr(views, 'collection_handle',
                        SimpleNamespace(update_one=lambda *_a, **_k: None))
    monkeypatch.setattr(views, 'render', lambda _req, _tpl, context: context)

    context = views.project_page(request_factory.get(f'/project/{project_id}'), project_id)
    [row] = context['sample_table_rows']
    assert row['n'] == 'S1'
    assert row['g'] == 'MYC PVT1'
    assert row['k'] == ['ecDNA']


def test_template_draws_the_table_from_the_rows():
    source = TEMPLATE.read_text()
    table = source[source.index("<table id='myTable1'"):source.index('</table>', source.index("<table id='myTable1'"))]

    assert '<tbody></tbody>' in table
    assert '{% for sample in sample_data %}' not in table
    assert '{{ sample_table_rows|json_script:"sample-table-rows" }}' in source
    assert 'deferRender: true' in source
    # DataTables' own search box would sit beside the gene filter.
    assert "dom: 'lrtip'" in source
    assert 'id="sample-filter-input"' in source
    # The bar click is wired to the chart summarybar.py draws.
    assert "getElementById('summarybar_plotly_div')" in source
    assert "on('plotly_click'" in source
    # The filtered download reads the sample name from the row data now.
    assert 'this.data().n' in source
