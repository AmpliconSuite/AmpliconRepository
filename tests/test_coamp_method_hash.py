"""A cached co-amplification graph cannot outlive the method that built it.

The neo4j graph and the saved edge CSV both hold statistics computed by
coamp_graph.py from the gene annotation through scipy and statsmodels.  The
cache key carries a hash of all of that, derived at import, so nobody has to
remember to bump anything: change the method and every existing key is
unreachable.  These tests pin the mechanism -- what is in the hash, what is
not, that the key uses it, and that stale keys are swept.
"""

import pytest


def test_the_hash_is_in_every_cache_key():
    from caper.coamp_graph import METHOD_HASH
    from caper.neo4j_utils import generate_cache_key, is_current_method_key

    assert len(METHOD_HASH) == 12 and int(METHOD_HASH, 16) >= 0
    short = generate_cache_key(['b', 'a'])
    long = generate_cache_key([f'{i:024x}' for i in range(10)])
    assert short == f'a_b.{METHOD_HASH}'
    assert long.endswith(f'.{METHOD_HASH}') and len(long) == 64 + 1 + 12
    assert is_current_method_key(short) and is_current_method_key(long)
    assert not is_current_method_key('a_b')                # pre-hash key
    assert not is_current_method_key('a_b.000000000000')   # another method
    assert not is_current_method_key(None)


SOURCE = "def perform_tests(edge):\n    return edge['weight'] * 2\n"
ANNOTATION = {'hg38_genes.bed': b'chr8\t1\t2\tMYC\n', 'hg19_genes.bed': b'chr8\t3\t4\tMYC\n'}
VERSIONS = {'scipy': '1.11.0', 'statsmodels': '0.14.1', 'numpy': '1.26.0', 'intervaltree': '3.1.0'}


def test_what_changes_the_hash_and_what_does_not():
    from caper.coamp_graph import method_hash

    base = method_hash(SOURCE, ANNOTATION, VERSIONS)

    # Formatting and comments are not the method.
    reformatted = "# a comment\ndef perform_tests(edge):\n\n    return   edge['weight']*2   # inline\n"
    assert method_hash(reformatted, ANNOTATION, VERSIONS) == base

    # The statistics code is.
    assert method_hash(SOURCE.replace('* 2', '* 3'), ANNOTATION, VERSIONS) != base
    # So is the gene annotation.
    moved = dict(ANNOTATION, **{'hg38_genes.bed': b'chr8\t1\t3\tMYC\n'})
    assert method_hash(SOURCE, moved, VERSIONS) != base
    # And the version of a statistics dependency.
    assert method_hash(SOURCE, ANNOTATION, dict(VERSIONS, scipy='1.12.0')) != base
    # Deterministic across dict orderings.
    assert method_hash(SOURCE, dict(reversed(list(ANNOTATION.items()))),
                       dict(reversed(list(VERSIONS.items())))) == base


def test_the_module_hash_is_built_from_the_module_itself(monkeypatch):
    """The import-time value must be what method_hash says for the real file,
    BEDs and installed packages -- otherwise the mechanism could be quietly
    bypassed by hashing something else."""
    import importlib.metadata
    from caper import coamp_graph

    with open(coamp_graph.__file__, encoding='utf-8') as fh:
        source = fh.read()
    annotation = {}
    for name in ('hg19_genes.bed', 'hg38_genes.bed'):
        with open(coamp_graph.Graph().get_gene_bed_path(name[:4]), 'rb') as fh:
            annotation[name] = fh.read()
    versions = {n: importlib.metadata.version(n) for n in coamp_graph.METHOD_DEPENDENCIES}
    assert coamp_graph.METHOD_HASH == coamp_graph.method_hash(source, annotation, versions)


class _Session:
    def __init__(self, keys_with_metadata, keys_with_nodes):
        self.meta, self.nodes, self.runs = keys_with_metadata, keys_with_nodes, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        self.runs.append((' '.join(query.split()), params))
        if 'GraphMetadata' in query and 'collect' in query:
            return _Single({'keys': self.meta})
        if 'n:Node' in query and 'collect' in query:
            return _Single({'keys': self.nodes})
        return _Single(None)


class _Single:
    def __init__(self, row):
        self._row = row

    def single(self):
        return self._row


def test_stale_method_graphs_are_swept_and_current_ones_kept(monkeypatch, tmp_path):
    from caper import neo4j_utils
    from caper.coamp_edges import save_edges, open_edges
    from caper.neo4j_utils import generate_cache_key, sweep_stale_method_graphs
    monkeypatch.setenv('COAMP_EDGES_DIR', str(tmp_path))

    current = generate_cache_key(['p1'])
    old_bare, old_hash, orphan = 'p1', 'p1.deadbeef0000', 'p2_p3.deadbeef0000'
    session = _Session([current, old_bare, old_hash], [current, orphan])
    monkeypatch.setattr(neo4j_utils, 'get_driver',
                        lambda: type('D', (), {'session': lambda self: session})())

    import pandas as pd

    class _Graph:
        def get_edges_dataframe(self, include_sample_ids=False):
            return pd.DataFrame({'gene1': ['A'], 'gene2': ['B'], 'gene1_sample_ids': ['s'],
                                 'gene2_sample_ids': ['s'], 'shared_sample_ids': ['s']})
    for key in (current, old_hash):
        save_edges(key, _Graph())

    assert sweep_stale_method_graphs() == sorted([old_bare, old_hash, orphan])
    deleted = [p['cache_key'] for q, p in session.runs if 'DELETE' in q or 'DETACH' in q]
    assert current not in deleted and set(deleted) == {old_bare, old_hash, orphan}
    assert open_edges(current, False) is not None, 'the current CSV was removed'
    assert open_edges(old_hash, False) is None, 'the stale CSV survived'


def test_clearing_a_project_covers_both_key_forms(monkeypatch):
    from caper import neo4j_utils
    from caper.neo4j_utils import clear_graph_cache_for_project, generate_cache_key

    session = _Session([], [])
    monkeypatch.setattr(neo4j_utils, 'get_driver',
                        lambda: type('D', (), {'session': lambda self: session})())
    monkeypatch.setattr(neo4j_utils, '_clear_cache_keys',
                        lambda s, keys: session.runs.append(('CLEAR', {'keys': list(keys)})))
    clear_graph_cache_for_project('p9')
    cleared = [p['keys'] for q, p in session.runs if q == 'CLEAR'][0]
    assert generate_cache_key(['p9']) in cleared and 'p9' in cleared
