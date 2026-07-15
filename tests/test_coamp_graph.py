"""Compatibility tests for co-amplification graph construction."""

import pandas as pd

from caper.coamp_graph import Graph


def test_mixed_ecdna_and_fan_dataset_builds_ecdna_graph():
    dataset = pd.DataFrame([
        {
            'Sample_name': 'mixed-sample',
            'Feature_ID': 'ecdna-feature',
            'Classification': 'ecDNA',
            'Reference_version': 'GRCh37',
            'Location': "['chr7:55000000-55300000', 'chr8:127700000-128800000']",
            'Oncogenes': ['EGFR', 'MYC'],
            'All_genes': ['EGFR', 'MYC'],
        },
        {
            'Sample_name': 'mixed-sample',
            'Feature_ID': 'fan-feature',
            'Classification': 'FAN',
            'Reference_version': 'GRCh37',
            'Location': "['chr12:69000000-69300000']",
            'Oncogenes': ['MDM2'],
            'All_genes': ['MDM2'],
        },
    ])

    graph = Graph(dataset)

    assert graph.is_valid()
    assert list(graph.preprocessed_dataset['Feature_ID']) == ['ecdna-feature']
    assert {node['label'] for node in graph.Nodes()} >= {'EGFR', 'MYC'}
    assert 'MDM2' not in {node['label'] for node in graph.Nodes()}
