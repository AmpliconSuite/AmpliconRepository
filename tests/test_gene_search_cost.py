"""
Regression tests for the cost of the gene search page.

Measured on production 2026-09-06, unauthenticated, whole-page wall time:

    /gene-search/?genequery=ZZZNOSUCHGENE   0 projects match     0.5-0.8 s
    /gene-search/?genequery=MYC             29 rows rendered     8.7-9.7 s
    /gene-search/                           16,950 rows          12.1-12.4 s

The page therefore costs about nine seconds even when it returns twenty-nine
rows: nearly all of it is fetching every matching project document -- ``runs``
is 95.7% of their bytes -- and rolling every sample up.  Two of the three pieces
this file pins down are the cheap half of that: the rollup itself, and the
per-row URL reverse in the template.  The document fetch is not addressed here.

The third is ``batch_sample_download``, which is the same read amplification in
the download path: it fetched a full project document per *selected sample*.
"""

import math

import pytest

from caper.utils import sample_data_from_feature_list


def _close(response):
    """Release a download response.

    create_zip_response() returns a streaming response and deletes the archive
    when it is closed -- which the WSGI server does in production.  A test that
    drops the response instead leaves a .zip in the repository root.
    """
    if response is not None:
        response.close()
    return response


def _row(sample, classification='ecDNA', oncogenes=None, **extra):
    row = {'Sample_name': sample, 'Classification': classification,
           'Oncogenes': list(oncogenes or []), 'Feature_ID': f'{sample}_1'}
    row.update(extra)
    return row


class TestSampleDataRollup:
    """``sample_data_from_feature_list`` replaced a pandas groupby.

    What matters is that it still produces exactly what the pandas version did,
    because views.py writes this output into ``project['sample_data']`` -- a
    shape change would be persisted, not merely rendered.
    """

    def test_groups_rows_by_sample_and_unions_oncogenes(self):
        rows = [_row('S1', oncogenes=['MYC']),
                _row('S1', oncogenes=['EGFR', 'MYC']),
                _row('S2', oncogenes=['CDK4'])]
        out = {d['Sample_name']: d for d in sample_data_from_feature_list(rows)}
        assert out['S1']['Oncogenes'] == ['EGFR', 'MYC']
        assert out['S2']['Oncogenes'] == ['CDK4']

    def test_samples_come_back_in_sorted_order(self):
        # pandas' groupby sorted its keys, and the template renders in this order.
        rows = [_row('S3'), _row('S1'), _row('S2')]
        names = [d['Sample_name'] for d in sample_data_from_feature_list(rows)]
        assert names == ['S1', 'S2', 'S3']

    def test_invalid_classifications_are_not_counted(self):
        rows = [_row('S1', classification='ecDNA'),
                _row('S1', classification='NA'),
                _row('S1', classification='Not Provided'),
                _row('S1', classification='ecDNA')]
        out = sample_data_from_feature_list(rows)[0]
        assert out['Features'] == 2
        assert out['Classifications'] == ['ecDNA']
        assert out['Classifications_counted'] == ['ecDNA (2)']

    def test_singleton_classification_carries_no_count(self):
        out = sample_data_from_feature_list([_row('S1', classification='BFB')])[0]
        assert out['Classifications_counted'] == ['BFB']

    def test_optional_field_takes_the_first_row_of_the_sample(self):
        rows = [_row('S1', Cancer_type='Breast'), _row('S1', Cancer_type='Lung')]
        assert sample_data_from_feature_list(rows)[0]['Cancer_type'] == 'Breast'

    def test_missing_optional_field_is_nan_not_none(self):
        """pd.DataFrame filled absent keys with NaN, so stored documents hold NaN.

        Returning None instead would change ``project['sample_data']`` for every
        project re-saved after the change.
        """
        rows = [_row('S1', Cancer_type='Breast'), _row('S2')]
        out = {d['Sample_name']: d for d in sample_data_from_feature_list(rows)}
        value = out['S2']['Cancer_type']
        assert isinstance(value, float) and math.isnan(value)

    def test_column_absent_from_every_row_is_absent_from_the_result(self):
        out = sample_data_from_feature_list([_row('S1')])[0]
        assert 'Cancer_type' not in out
        assert 'Tissue_of_origin' not in out

    def test_present_keys_keeps_columns_a_row_filter_would_drop(self):
        """The trap for any caller that rolls up a subset of a project's rows.

        Which optional columns appear depends on *all* the project's rows, not
        on the rows of the sample being rolled up.  "PCAWG filtered" carries
        Cancer_type on 3,749 of its 3,880 feature rows and 92 of its 2,095
        samples have it on none (measured locally 2026-09-06), so a caller that
        filters rows without passing ``present_keys`` silently drops the column
        from samples that should keep it.
        """
        all_rows = [_row('S1', Cancer_type='Breast'), _row('S2')]
        every_key = set()
        for row in all_rows:
            every_key.update(row)

        subset = [r for r in all_rows if r['Sample_name'] == 'S2']

        naive = sample_data_from_feature_list(subset)[0]
        assert 'Cancer_type' not in naive          # the trap

        guarded = sample_data_from_feature_list(subset, present_keys=every_key)[0]
        assert 'Cancer_type' in guarded
        assert math.isnan(guarded['Cancer_type'])

    def test_empty_input(self):
        assert sample_data_from_feature_list([]) == []

    def test_does_not_mutate_its_input(self):
        rows = [_row('S1', oncogenes=['MYC'])]
        before = [dict(r) for r in rows]
        sample_data_from_feature_list(rows)
        assert rows == before


class TestGeneSearchTemplateCost:
    """The project link is reversed once per project, not once per row."""

    def test_rendered_project_link_is_unchanged(self, request_factory, test_user,
                                                mongo_collection):
        """Swapping {% url %} for a precomputed value must not move the link.

        The top-level ``Oncogenes`` list is what the view's Mongo query filters
        projects on, before it ever looks at ``runs``.
        """
        from caper.views import gene_search_page

        doc = {
            'project_name': 'GeneSearchUrlCost',
            'creator': test_user.username,
            'project_members': [test_user.username],
            'private': 'public',
            'delete': False, 'current': True, 'FINISHED?': True,
            'sample_count': 1,
            'Oncogenes': ['GSCOSTGENE'],
            'runs': {'GS_SAMPLE': [_row('GS_SAMPLE', oncogenes=['GSCOSTGENE'])]},
        }
        inserted = mongo_collection.insert_one(doc)
        project_id = str(inserted.inserted_id)
        try:
            request = request_factory.get('/gene-search/',
                                          {'genequery': 'GSCOSTGENE'})
            request.user = test_user
            response = gene_search_page(request)
            assert response.status_code == 200
            content = response.content.decode()
            assert f'href="/project/{project_id}"' in content
            assert 'GS_SAMPLE' in content
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_template_does_not_reverse_per_row(self):
        """A guard on the template, since the cost is invisible in the output.

        Both sample tables rendered ``{% url 'project_page' %}`` inside the row
        loop.  On the unfiltered search that is 16,950 route resolutions for 32
        distinct values.  The project tables above them are per-project and may
        keep using the tag.
        """
        import os
        from django.conf import settings

        path = None
        for directory in settings.TEMPLATES[0]['DIRS']:
            candidate = os.path.join(directory, 'pages', 'gene_search.html')
            if os.path.exists(candidate):
                path = candidate
                break
        assert path, "gene_search.html not found"

        with open(path) as handle:
            source = handle.read()

        for loop_var, table in (('public_sample_data', 'public'),
                                ('private_sample_data', 'private')):
            start = source.index(f'for sample in {loop_var}')
            end = source.index('endfor', start)
            body = source[start:end]
            assert "{% url" not in body, (
                f"the {table} sample row loop reverses a URL per row")
            assert 'sample.project_url' in body


class TestBatchDownloadReadAmplification:
    """``batch_sample_download`` loaded a project document per selected sample.

    A "Select All" download on the unfiltered gene search names every sample on
    the site -- 16,950 of them, measured on production 2026-09-06 -- spread over
    32 distinct projects, and the loop fetched the whole project document,
    ``runs`` included, once per sample rather than once per project.  There is
    no server-side cap on the selection: the 1000-sample guard is commented out
    and ``DATA_UPLOAD_MAX_NUMBER_FIELDS`` is None.
    """

    def test_one_project_load_per_project_not_per_sample(
            self, request_factory, test_user, mongo_collection, monkeypatch):
        from caper import views

        runs = {f'SAMPLE_{c}': [_row(f'SAMPLE_{c}')] for c in 'ABCDE'}
        inserted = mongo_collection.insert_one({
            'project_name': 'BatchDownloadReadAmp',
            'creator': test_user.username,
            'project_members': [test_user.username],
            'private': 'public',
            'delete': False, 'current': True, 'FINISHED?': True,
            'sample_count': len(runs),
            'runs': runs,
        })
        project_id = str(inserted.inserted_id)

        loaded = []

        def _spy(name):
            original = getattr(views, name)

            def wrapper(*args, **kwargs):
                doc = original(*args, **kwargs)
                loaded.append((name, doc))
                return doc
            return wrapper

        for fn in ('get_one_project', 'get_one_project_sans_runs'):
            monkeypatch.setattr(views, fn, _spy(fn))

        try:
            request = request_factory.post(
                '/batch-sample-download/',
                {'samples': [f'{project_id}:{s}' for s in runs]})
            request.user = test_user
            try:
                _close(views.batch_sample_download(request))
            except Exception:
                # The zip/S3 tail of the view is not what this test pins down;
                # the loads above it have already happened either way.
                pass

            assert loaded, "the view loaded no project document at all"
            assert len(loaded) == 1, (
                f"loaded a project document {len(loaded)} times for "
                f"{len(runs)} samples of one project")
            name, doc = loaded[0]
            assert name == 'get_one_project_sans_runs'
            assert doc is not None and 'runs' not in doc, (
                "the whole project document was fetched to read its metadata")
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})


class TestBatchDownloadRefusals:
    """The two refusal paths in ``batch_sample_download`` returned 500s.

    Each passed its message as a ``reverse()`` keyword argument --
    ``redirect('gene_search_page', alert_message=...)`` -- to a route that takes
    no arguments, so every refusal raised ``NoReverseMatch``.  Neither is
    reachable from the UI, since the JavaScript returns early on an empty
    selection, so a direct request was the only way to reach them.
    """

    @staticmethod
    def _request(request_factory, test_user, method='post', data=None):
        """A request carrying message storage, which RequestFactory omits."""
        from django.contrib.messages.storage.fallback import FallbackStorage

        request = getattr(request_factory, method)('/batch-sample-download/',
                                                   data or {})
        request.user = test_user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_get_is_refused_without_a_server_error(self, request_factory, test_user):
        from caper.views import batch_sample_download

        response = batch_sample_download(
            self._request(request_factory, test_user, method='get'))
        assert response.status_code == 302
        assert response['Location'] == '/gene-search/'

    def test_empty_selection_is_refused_without_a_server_error(
            self, request_factory, test_user):
        from caper.views import batch_sample_download

        response = batch_sample_download(self._request(request_factory, test_user))
        assert response.status_code == 302
        assert response['Location'] == '/gene-search/'

    def test_the_refusal_message_reaches_the_user(self, request_factory, test_user):
        """A redirect that drops the message is the bug in a quieter form."""
        from caper.views import batch_sample_download

        request = self._request(request_factory, test_user)
        batch_sample_download(request)
        texts = [str(m) for m in request._messages]
        assert any('No samples were selected' in t for t in texts), texts

    def test_no_sample_cap_is_reinstated(self):
        """#469 removed the `len(samples) > 1000` refusal on purpose, per #348.

        Batches over a thousand samples are meant to work, delivered as an
        emailed link.  A cap here would be a regression against that issue, so
        this fails if one comes back.
        """
        import inspect
        from caper import views

        source = inspect.getsource(views.batch_sample_download)
        stripped = '\n'.join(line for line in source.splitlines()
                              if not line.lstrip().startswith('#'))
        assert 'len(samples) >' not in stripped, (
            "a sample-count cap was reinstated; see #348 and #637")


class TestBulkSampleFetch:
    """``fetch_sample_rows_by_name`` replaced a per-sample ``get_one_sample``.

    ``get_one_sample`` scans every run in the project server-side to find one
    sample, so calling it once per selected sample made a batch download cost
    O(selected x total).  Measured on dev 2026-09-06 against HMF (4,170
    samples, 12.6 MB document): 0.97s per sample, 4,050s for the project.  One
    filtered scan covering 1,000 names took 0.76s.
    """

    @staticmethod
    def _project(mongo_collection, test_user, runs, name='BulkSampleFetch'):
        return mongo_collection.insert_one({
            'project_name': name,
            'creator': test_user.username,
            'project_members': [test_user.username],
            'private': 'public',
            'delete': False, 'current': True, 'FINISHED?': True,
            'sample_count': len(runs),
            'runs': runs,
        })

    def test_returns_only_the_requested_samples(self, mongo_collection, test_user):
        from caper.utils import fetch_sample_rows_by_name

        runs = {f'run_{c}': [_row(f'S{c}')] for c in 'ABCDE'}
        inserted = self._project(mongo_collection, test_user, runs)
        try:
            got = fetch_sample_rows_by_name(inserted.inserted_id,
                                            ['SA', 'SC', 'NOSUCHSAMPLE'])
            assert sorted(got) == ['SA', 'SC']
            assert got['SA'][0]['Sample_name'] == 'SA'
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_chunking_does_not_drop_names(self, mongo_collection, test_user):
        """The chunk boundary is where a bulk fetch silently loses samples."""
        from caper.utils import fetch_sample_rows_by_name

        runs = {f'run_{i:02d}': [_row(f'S{i:02d}')] for i in range(7)}
        inserted = self._project(mongo_collection, test_user, runs)
        try:
            wanted = [f'S{i:02d}' for i in range(7)]
            got = fetch_sample_rows_by_name(inserted.inserted_id, wanted,
                                            chunk_size=2)
            assert sorted(got) == wanted
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_all_feature_rows_of_a_sample_come_back(self, mongo_collection, test_user):
        from caper.utils import fetch_sample_rows_by_name

        runs = {'run_A': [_row('SA'), _row('SA', classification='BFB')]}
        inserted = self._project(mongo_collection, test_user, runs)
        try:
            got = fetch_sample_rows_by_name(inserted.inserted_id, ['SA'])
            assert len(got['SA']) == 2
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_space_containing_keys_are_normalised(self, mongo_collection, test_user):
        """``get_one_sample`` normalised these, and the download path relies on it."""
        from caper.utils import fetch_sample_rows_by_name

        row = _row('SA')
        row['AA amplicon number'] = 1
        inserted = self._project(mongo_collection, test_user, {'run_A': [row]})
        try:
            got = fetch_sample_rows_by_name(inserted.inserted_id, ['SA'])
            assert 'AA_amplicon_number' in got['SA'][0]
            assert 'AA amplicon number' not in got['SA'][0]
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_matches_get_one_sample_for_a_duplicated_name(
            self, mongo_collection, test_user):
        """Two runs can carry the same Sample_name; the lower run key wins."""
        from caper.utils import fetch_sample_rows_by_name

        runs = {'run_b': [_row('SA', classification='BFB')],
                'run_a': [_row('SA', classification='ecDNA')]}
        inserted = self._project(mongo_collection, test_user, runs)
        try:
            got = fetch_sample_rows_by_name(inserted.inserted_id, ['SA'])
            assert got['SA'][0]['Classification'] == 'ecDNA'
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_batch_download_fetches_once_per_project(
            self, request_factory, test_user, mongo_collection, monkeypatch):
        from caper import views

        runs = {f'run_{c}': [_row(f'S{c}')] for c in 'ABCDE'}
        inserted = self._project(mongo_collection, test_user, runs,
                                 name='BulkSampleFetchView')
        project_id = str(inserted.inserted_id)

        calls = []
        original = views.fetch_sample_rows_by_name
        monkeypatch.setattr(views, 'fetch_sample_rows_by_name',
                            lambda pid, names, **kw: calls.append(list(names))
                            or original(pid, names, **kw))

        def _no_per_sample_lookup(*args, **kwargs):
            raise AssertionError(
                "batch download fell back to a per-sample project scan")
        monkeypatch.setattr(views, 'get_one_sample', _no_per_sample_lookup)

        try:
            request = request_factory.post(
                '/batch-sample-download/',
                {'samples': [f'{project_id}:S{c}' for c in 'ABCDE']})
            request.user = test_user
            try:
                _close(views.batch_sample_download(request))
            except Exception:
                # The zip tail of the view is not what this test pins down.
                pass
            assert len(calls) == 1, (
                f"fetched sample rows {len(calls)} times for one project")
            assert sorted(calls[0]) == ['SA', 'SB', 'SC', 'SD', 'SE']
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})


class TestDownloadCounterWrites:
    """The ``sample_downloads`` counter was written once per sample.

    DocumentDB rewrites the whole document for any update, so the cost is set
    by the size of the project rather than of the field: 537ms per write
    against HMF's 12.62 MB document and 10.7ms against a 0.07 MB one, measured
    on dev 2026-09-06.  Under cProfile that single ``update_one`` was 90% of
    ``process_sample_data``, and 2,281s of a whole-project HMF batch.
    """

    def test_absent_counter_starts_at_the_batch_size(
            self, mongo_collection, test_user, monkeypatch):
        from caper import views
        from caper.utils import get_date_short

        inserted = mongo_collection.insert_one(
            {'project_name': 'CounterAbsent', 'creator': test_user.username,
             'delete': False, 'current': True})
        try:
            monkeypatch.setattr(views, 'collection_handle', mongo_collection)
            project = mongo_collection.find_one({'_id': inserted.inserted_id})
            views.record_sample_downloads(project, 12)
            stored = mongo_collection.find_one(
                {'_id': inserted.inserted_id})['sample_downloads']
            assert stored == {get_date_short(): 12}
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_existing_day_accumulates(self, mongo_collection, test_user, monkeypatch):
        from caper import views
        from caper.utils import get_date_short

        today = get_date_short()
        inserted = mongo_collection.insert_one(
            {'project_name': 'CounterToday', 'creator': test_user.username,
             'delete': False, 'current': True,
             'sample_downloads': {'2020-01-01': 5, today: 3}})
        try:
            monkeypatch.setattr(views, 'collection_handle', mongo_collection)
            project = mongo_collection.find_one({'_id': inserted.inserted_id})
            views.record_sample_downloads(project, 4)
            stored = mongo_collection.find_one(
                {'_id': inserted.inserted_id})['sample_downloads']
            assert stored == {'2020-01-01': 5, today: 7}
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_legacy_integer_counter_is_migrated_and_counted(
            self, mongo_collection, test_user, monkeypatch):
        """3 of 243 production projects still hold an int here (2026-09-06).

        The migration branch used to drop the download that triggered it, which
        was invisible at one download per call and would have lost a whole
        batch.
        """
        from caper import views
        from caper.utils import get_date_short

        inserted = mongo_collection.insert_one(
            {'project_name': 'CounterLegacyInt', 'creator': test_user.username,
             'delete': False, 'current': True, 'sample_downloads': 9})
        try:
            monkeypatch.setattr(views, 'collection_handle', mongo_collection)
            project = mongo_collection.find_one({'_id': inserted.inserted_id})
            views.record_sample_downloads(project, 6)
            stored = mongo_collection.find_one(
                {'_id': inserted.inserted_id})['sample_downloads']
            assert stored == {get_date_short(): 15}
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_repeated_calls_accumulate_in_the_passed_project(
            self, mongo_collection, test_user, monkeypatch):
        """The batch loop holds one project document across the whole run."""
        from caper import views
        from caper.utils import get_date_short

        inserted = mongo_collection.insert_one(
            {'project_name': 'CounterRepeat', 'creator': test_user.username,
             'delete': False, 'current': True})
        try:
            monkeypatch.setattr(views, 'collection_handle', mongo_collection)
            project = mongo_collection.find_one({'_id': inserted.inserted_id})
            views.record_sample_downloads(project, 2)
            views.record_sample_downloads(project, 3)
            stored = mongo_collection.find_one(
                {'_id': inserted.inserted_id})['sample_downloads']
            assert stored == {get_date_short(): 5}
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})

    def test_batch_download_writes_the_counter_once_per_project(
            self, request_factory, test_user, mongo_collection, monkeypatch):
        from caper import views

        # process_sample_data() has to run to completion for the sample to be
        # counted, and preprocess_sample_data() requires Location and
        # AA_amplicon_number.
        runs = {f'run_{c}': [_row(f'S{c}', Location=["'chr1:1-2'"],
                                  AA_amplicon_number=1,
                                  Feature_BED_file='Not Provided')]
                for c in 'ABCDE'}
        inserted = mongo_collection.insert_one({
            'project_name': 'CounterBatch', 'creator': test_user.username,
            'project_members': [test_user.username], 'private': 'public',
            'delete': False, 'current': True, 'FINISHED?': True,
            'sample_count': len(runs), 'runs': runs,
        })
        project_id = str(inserted.inserted_id)

        counted = []
        monkeypatch.setattr(views, 'record_sample_downloads',
                            lambda project, count=1: counted.append(count))
        try:
            request = request_factory.post(
                '/batch-sample-download/',
                {'samples': [f'{project_id}:S{c}' for c in 'ABCDE']})
            request.user = test_user
            try:
                response = views.batch_sample_download(request)
                # Closing is what deletes the archive; the WSGI server does it
                # in production.
                if response is not None:
                    response.close()
            except Exception:
                pass
            assert counted == [5], (
                f"the counter was written {len(counted)} times for "
                f"{len(runs)} samples of one project: {counted}")
        finally:
            mongo_collection.delete_one({'_id': inserted.inserted_id})
