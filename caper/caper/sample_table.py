"""
The rows behind the project page's sample table.

The table is drawn from JSON rather than from one ``<tr>`` per sample: the
largest public project (HMF, 4,170 samples) built every row into the DOM on
every visit, and DataTables then hid all but ten of them.  With the rows as
data and ``deferRender`` only the visible page is ever built, which leaves room
for the one thing the HTML never carried: every amplified gene of a sample, so
the filter box can find samples by any gene and not just by oncogene.

Measured from the 2026-10-26 off-site copies of the public projects, the gene
lists cost HMF 0.98 MB uncompressed and 0.38 MB gzipped (the site gzips); the
next largest, TCGA unfiltered, 0.62 MB and 0.18 MB.  Most samples carry none --
the median is zero genes -- so the cost sits in a few heavily amplified ones.
"""

import math

from .summarybar import canonical_classification


def _strings(value):
    """A feature field as a list of strings, whatever shape it was stored in."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def sample_table_rows(sample_data, runs):
    """One compact row per sample in ``sample_data``, in the same order.

    Keys are short because there is one copy of each per sample:

    ``n``  sample name
    ``f``  feature count
    ``o``  oncogenes, comma-separated, as the table shows them
    ``c``  classifications with their counts, as the table shows them
    ``k``  the sample's classifications under their current names -- what a
           click on a bar of the summary chart filters by, so they are
           normalised exactly as that chart normalises them
    ``g``  every amplified gene, space-separated (gene symbols have no spaces)
    """
    genes_by_sample = {}
    for sample_name, features in (runs or {}).items():
        for feature in features or []:
            name = feature.get('Sample_name', sample_name)
            genes = genes_by_sample.setdefault(name, set())
            genes.update(_strings(feature.get('All_genes', feature.get('All genes'))))
            genes.update(_strings(feature.get('Oncogenes')))

    rows = []
    for sample in sample_data:
        name = sample.get('Sample_name')
        features = sample.get('Features', 0)
        if not isinstance(features, (int, float)) or isinstance(features, bool) \
                or not math.isfinite(features):
            features = 0   # json_script would write NaN, which JSON.parse rejects
        classes = sorted({canonical_classification(c)
                          for c in _strings(sample.get('Classifications'))})
        rows.append({
            'n': name,
            'f': int(features),
            'o': ', '.join(_strings(sample.get('Oncogenes'))),
            'c': ', '.join(_strings(sample.get('Classifications_counted'))),
            'k': classes,
            'g': ' '.join(sorted(genes_by_sample.get(name, ()))),
        })
    return rows
