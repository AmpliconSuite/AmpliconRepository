"""How a batch download is delivered, and who waits for it.

Two things were decided by the page rather than by the server:

* whether a batch is emailed or returned in the response.  The gene search page
  switches to email above 100 samples, but the switch lived only in its
  JavaScript, so a request that did not come from the page could ask for any
  number of samples in one response.  That is how a 4,170-sample direct
  download was made while testing on 2026-09-06.
* whether the work happens on the request thread.  Gathering every public
  sample took ~596s of the 900s gunicorn timeout on dev on 2026-09-06, against
  a corpus smaller than production's.

Both now hold server-side.  See #637.
"""
import os
import shutil

import pytest


def _with_messages(request):
    """RequestFactory makes no session, and messages.error() needs storage."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.cache import SessionStore
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request


def _row(sample_name, **over):
    row = {
        'Sample_name': sample_name,
        'Location': ["'chr1:1-2'"],
        'Oncogenes': [],
        'AA_amplicon_number': 1,
        'Feature_BED_file': 'Not Provided',
        'Reference_version': 'GRCh37',
        'Classification': 'ecDNA',
    }
    row.update(over)
    return row


@pytest.fixture
def project(mongo_collection, test_user):
    """A small public project the test user may download from."""
    runs = {f'run_{i}': [_row(f'S{i}')] for i in range(5)}
    inserted = mongo_collection.insert_one({
        'project_name': 'BatchDelivery', 'creator': test_user.username,
        'project_members': [test_user.username], 'private': 'public',
        'delete': False, 'current': True, 'FINISHED?': True,
        'sample_count': len(runs), 'runs': runs,
    })
    yield str(inserted.inserted_id)
    mongo_collection.delete_one({'_id': inserted.inserted_id})


class _RecordingExecutor:
    """Stands in for the shared BackgroundTaskTracker; records, runs nothing."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, task_label=None, temp_dir=None, **kwargs):
        self.calls.append({'fn': fn, 'args': args, 'task_label': task_label,
                           'temp_dir': temp_dir, 'kwargs': kwargs})
        return None


@pytest.mark.integration
class TestDeliveryIsDecidedServerSide:

    def test_a_direct_download_over_the_cap_is_refused(
            self, request_factory, test_user, project, monkeypatch):
        from caper import views

        recorder = _RecordingExecutor()
        monkeypatch.setattr(views, '_thread_executor', recorder)
        monkeypatch.setattr(views, '_gather_batch_samples',
                            lambda *a, **k: pytest.fail(
                                'an over-cap direct download must not gather'))
        # One name over the cap.  They need not all exist: the refusal is on the
        # size of the selection, before anything is read.
        n = views.BATCH_DIRECT_DOWNLOAD_MAX_SAMPLES + 1
        request = request_factory.post(
            '/batch-sample-download/',
            {'samples': [f'{project}:S{i}' for i in range(n)]})
        request.user = test_user
        _with_messages(request)

        response = views.batch_sample_download(request)

        assert response.status_code == 302
        assert recorder.calls == [], 'a refusal must not queue work either'

    def test_emailed_results_go_to_the_background(
            self, request_factory, test_user, project, monkeypatch):
        from caper import views

        recorder = _RecordingExecutor()
        monkeypatch.setattr(views, '_thread_executor', recorder)
        monkeypatch.setattr(views, '_gather_batch_samples',
                            lambda *a, **k: pytest.fail(
                                'the request thread must not gather'))
        monkeypatch.setattr(views.settings, 'USE_S3_DOWNLOADS', True)

        request = request_factory.post(
            '/batch-sample-download/',
            {'samples': [f'{project}:S{i}' for i in range(5)],
             'emailResults': 'true'})
        request.user = test_user
        _with_messages(request)

        response = views.batch_sample_download(request)

        assert response.status_code == 302
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call['fn'] is views._run_batch_download_and_email
        assert call['task_label'] == 'batch_sample_download'
        # The directory must be registered, or cleanup_stale_temp_dirs() can
        # remove it while the task is still writing into it.
        assert call['temp_dir'] and call['temp_dir'].startswith('tmp/batch_')
        shutil.rmtree(call['temp_dir'], ignore_errors=True)

    def test_a_small_direct_download_still_answers_in_the_request(
            self, request_factory, test_user, project, monkeypatch):
        from caper import views

        recorder = _RecordingExecutor()
        monkeypatch.setattr(views, '_thread_executor', recorder)

        request = request_factory.post(
            '/batch-sample-download/',
            {'samples': [f'{project}:S{i}' for i in range(5)]})
        request.user = test_user
        _with_messages(request)

        response = views.batch_sample_download(request)
        try:
            assert response.status_code == 200
            assert recorder.calls == [], 'a small batch needs no background task'
            assert getattr(response, 'streaming', False), (
                'the archive must stream; HttpResponse holds all of it in memory')
            assert b''.join(response.streaming_content)[:2] == b'PK'
        finally:
            response.close()


@pytest.mark.integration
class TestZipResponseCleansUpAfterStreaming:
    """The archive cannot be deleted before the response, because with a
    streaming response nothing has been sent when the view returns."""

    def _source(self, tmp_path):
        src = tmp_path / 'src'
        (src / 'sample').mkdir(parents=True)
        (src / 'sample' / 'a.txt').write_text('hello')
        return str(src)

    def test_the_source_directory_goes_immediately(self, tmp_path):
        from caper import views

        src = self._source(tmp_path)
        base = str(tmp_path / 'archive')
        response = views.create_zip_response(src, base)
        try:
            assert not os.path.exists(src), (
                'the gathered files are dead weight once the archive exists')
        finally:
            response.close()

    def test_the_archive_goes_once_it_has_been_streamed(self, tmp_path):
        from caper import views

        src = self._source(tmp_path)
        base = str(tmp_path / 'archive')
        zip_path = base + '.zip'
        response = views.create_zip_response(src, base)

        assert os.path.exists(zip_path), 'still needed: nothing has been sent yet'
        body = b''.join(response.streaming_content)
        response.close()

        assert body[:2] == b'PK'
        assert not os.path.exists(zip_path)

    def test_a_client_that_disconnects_does_not_strand_the_archive(self, tmp_path):
        from caper import views

        src = self._source(tmp_path)
        base = str(tmp_path / 'archive')
        zip_path = base + '.zip'
        response = views.create_zip_response(src, base)

        # close() without reading the body is what a dropped connection looks
        # like from here.
        response.close()

        assert not os.path.exists(zip_path)
