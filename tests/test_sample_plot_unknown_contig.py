"""A Location that does not name a contig must not 500 the sample page.

``get_chrom_num()`` splits a location on ':' and strips a leading 'chr'.  Given
a string that has neither -- and the aggregator writes sentinels such as
'Interval file not found' into this field -- it hands the string straight back,
which then reached ``chrom_lens[key]`` as a KeyError.  Three samples of one
production project had been returning 500 that way for at least a day when it
was found on 2026-09-06:

    File "/srv/caper/caper/sample_plot.py", line 230, in plot
        x_range = chrom_lens[key]
    KeyError: 'Interval file not found'
"""
import pytest


@pytest.fixture
def db_handle(mongo_collection):
    """The live database, which gridfs.GridFS() insists on being handed.

    Nothing here reads a file from it: every row below carries
    ``CNV_BED_file: 'Not Provided'``, which plot() resolves to an empty frame.
    """
    return mongo_collection.database


def _row(location, **over):
    row = {
        'Sample_name': 'S1',
        'Reference_version': 'GRCh37',
        'AA_amplicon_number': 1,
        'Location': location,
        'Classification': 'ecDNA',
        'CNV_BED_file': 'Not Provided',
        'Oncogenes': [],
        'Feature_ID': 'S1_amplicon1_ecDNA_1',
        'Feature_maximum_copy_number': 12.5,
        'Feature_median_copy_number': 8.0,
    }
    row.update(over)
    return row


def test_get_chrom_num_returns_a_sentinel_unchanged():
    """The behaviour that made this reachable; pinned so it is not a surprise."""
    from caper.sample_plot import get_chrom_num

    assert get_chrom_num('Interval file not found') == 'Interval file not found'
    assert get_chrom_num("'chr7:1-100'") == '7'
    # The shape production actually stores: bare contig, no 'chr', no quotes.
    assert get_chrom_num('22:40974358-40977233') == '22'


@pytest.mark.integration
@pytest.mark.parametrize('bad', ['Interval file not found', 'chrUn_KI270302v1'])
def test_unknown_contig_does_not_raise(bad, db_handle):
    from caper import sample_plot

    sample = [_row(['7:54000000-55000000', bad])]
    # The assertion is simply that this returns.  Before the guard it raised
    # KeyError, which Django turned into a 500 for the whole sample page.
    out = sample_plot.plot(db_handle, sample, 'S1', 'proj', filter_plots=True)
    assert out is not None


@pytest.mark.integration
def test_a_sample_with_only_unknown_contigs_still_renders(db_handle):
    from caper import sample_plot

    sample = [_row(['Interval file not found'])]
    out = sample_plot.plot(db_handle, sample, 'S1', 'proj', filter_plots=True)
    assert out is not None


@pytest.mark.integration
def test_the_good_locations_of_a_mixed_sample_survive(db_handle):
    """Dropping the unplottable name must not drop the real chromosome with it."""
    from caper import sample_plot

    sample = [_row(['7:54000000-55000000', 'Interval file not found'])]
    out = sample_plot.plot(db_handle, sample, 'S1', 'proj', filter_plots=True)
    # plot() returns rendered HTML.  The good interval's coordinates must appear
    # in it, and the sentinel must not have been carried through as a contig.
    assert '54000000' in out
    assert 'Interval file not found' not in out


class TestIgvFeaturesTolerateTheSameSentinel:
    """Guarding the plot alone only moved the 500 into the IGV panel.

    Fixed 2026-09-06 in sample_plot.py; the sample page still returned 500 when
    that reached production, from a second consumer of the same field:

        File "/srv/caper/caper/views.py", line 1956, in sample_page
            roi_features, locus = igv_features_creation(feature['Location'])
        File "/srv/caper/caper/views.py", line 1767, in igv_features_creation
            start = int(parsed[1])
        IndexError: list index out of range
    """

    def test_a_sentinel_location_is_skipped(self):
        from caper.views import igv_features_creation

        features, locus = igv_features_creation(['Interval file not found'])
        assert features == []

    def test_the_real_locations_of_a_mixed_list_survive(self):
        from caper.views import igv_features_creation

        features, locus = igv_features_creation(
            ["'chr11:56595156-58875237'", 'Interval file not found',
             " 'chr11:66684707-68055335'"])
        assert [f['chr'] for f in features] == ['chr11', 'chr11']
        assert [f['start'] for f in features] == [56595156, 66684707]

    def test_a_non_numeric_range_is_skipped_rather_than_raising(self):
        from caper.views import igv_features_creation

        features, locus = igv_features_creation(["'chrX:start-end'"])
        assert features == []

    def test_the_unquoted_production_shape_still_parses(self):
        from caper.views import igv_features_creation

        features, locus = igv_features_creation(['22:40974358-40977233'])
        assert features == [{'chr': '22', 'start': 40974358, 'end': 40977233}]
