"""CNV BED parsing must tolerate every layout AmpliconSuite emits.

The plotting code was written against 5-column CNVkit output
(chrom/start/end/source/CN).  A 4-column file (chrom/start/end/CN) left the
frame with no 'Copy Number' column, and every sample page in the project
returned a 500 with ``KeyError: 'Copy Number'``.
"""
import pandas as pd
import pytest


def _frame(rows):
    return pd.DataFrame(rows)


def test_five_column_cnvkit_layout_keeps_source_and_cn():
    from caper.sample_plot import normalize_cnv_frame

    out = normalize_cnv_frame(_frame([
        ['chr1', 641700, 766119, 'CNVkit', 2.41922507815],
        ['chr7', 54000000, 55000000, 'CNVkit', 41.5],
    ]))

    assert list(out.columns) == ["Chromosome Number", "Feature Start Position",
                                "Feature End Position", "Source", "Copy Number"]
    assert out['Source'].tolist() == ['CNVkit', 'CNVkit']
    assert out['Copy Number'].tolist() == [2.41922507815, 41.5]


def test_four_column_layout_gets_a_copy_number_column():
    """The HCMI regression: CN is the last column and there is no source."""
    from caper.sample_plot import normalize_cnv_frame

    out = normalize_cnv_frame(_frame([
        ['chr1', 1, 83246006, 3],
        ['chr1', 83246007, 84640006, 2],
    ]))

    assert list(out.columns) == ["Chromosome Number", "Feature Start Position",
                                "Feature End Position", "Source", "Copy Number"]
    assert out['Copy Number'].tolist() == [3, 2]
    assert out['Source'].tolist() == ['Not Provided', 'Not Provided']


def test_copy_number_is_taken_from_the_last_column_when_extra_columns_exist():
    from caper.sample_plot import normalize_cnv_frame

    out = normalize_cnv_frame(_frame([
        ['chr1', 1, 1000, 'CNVkit', 'extra', 7.5],
    ]))

    assert out['Copy Number'].tolist() == [7.5]
    # Past 5 columns we cannot say which field is the source, so we do not guess.
    assert out['Source'].tolist() == ['Not Provided']


def test_rows_with_unparseable_coordinates_or_cn_are_dropped():
    from caper.sample_plot import normalize_cnv_frame

    out = normalize_cnv_frame(_frame([
        ['chr1', 1, 1000, 3],
        ['track name=cnv', None, None, None],
        ['chr2', 'start', 'end', 4],
        ['chr3', 10, 20, 'not-a-number'],
    ]))

    assert len(out) == 1
    assert out['Chromosome Number'].tolist() == ['chr1']
    # Positional reads downstream (row[1], row[2], row[-1]) need real numbers.
    assert out['Feature Start Position'].dtype.kind in 'if'
    assert out['Copy Number'].dtype.kind in 'if'


@pytest.mark.parametrize('rows', [
    [],                                  # no data at all
    [['chr1', 1, 1000]],                 # too few columns to be a CNV file
])
def test_degenerate_input_yields_the_canonical_empty_frame(rows):
    from caper.sample_plot import normalize_cnv_frame

    out = normalize_cnv_frame(_frame(rows))

    assert out.empty
    assert list(out.columns) == ["Chromosome Number", "Feature Start Position",
                                "Feature End Position", "Source", "Copy Number"]
