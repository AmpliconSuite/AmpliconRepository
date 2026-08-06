"""
Regression tests for read amplification in the v1 project API views.

Same class of problem as the ``get_one_sample()`` fix in v2.18.6_080526, in a
different place.  ``ProjectDetailView`` and ``ProjectDownloadView`` both called
``get_one_project()``, which fetches the whole project document with no
projection — including the entire ``runs`` dict holding every sample and every
feature row — in order to return a few KB of metadata or a 302 redirect.

Measured on a 2.77 MB / 329-sample project before the fix:

    ProjectDetailView    response     5.0 KB   mongo read  2838 KB    572x
    ProjectDownloadView  response 302/0 KB     mongo read  2838 KB      inf
    ProjectSamplesView   response  2203.5 KB   mongo read  2836 KB      1.3x

The read also happens *before* the access check, so an unauthenticated caller
could force a multi-megabyte database read and receive only a 401.

The contract these tests pin down:
  * the metadata/redirect endpoints never pull ``runs`` over the wire
  * that holds for anonymous callers too, who must not be able to trigger the
    large read at all
  * ``ProjectSamplesView`` still fetches runs — they *are* its payload
  * the responses themselves are unchanged
"""

import pytest
from bson.objectid import ObjectId
from rest_framework.test import APIRequestFactory


def _feature_row(sample_name, feature_id, bulk_size=0):
    """A feature row shaped like the real ones, optionally padded."""
    row = {
        'Sample_name': sample_name,
        'Feature_ID': feature_id,
        'AA_amplicon_number': 1,
        'Classification': 'ecDNA',
        'Location': "['chr1:1000-2000']",
        'Reference_version': 'hg38',
    }
    if bulk_size:
        # Stand-in for the real per-feature payload, so a full-document fetch
        # is measurably more expensive than fetching the metadata alone.
        row['_bulk_payload'] = 'x' * bulk_size
    return row


@pytest.fixture
def bulky_project(mongo_collection, test_user):
    """A public project whose ``runs`` payload dwarfs its metadata."""
    runs = {
        f'SAMPLE_{c}': [_feature_row(f'SAMPLE_{c}', f'{c}_amplicon1',
                                     bulk_size=200_000)]
        for c in 'ABC'
    }
    result = mongo_collection.insert_one({
        'project_name': 'ApiReadAmplificationTest',
        'description': 'fixture project',
        'creator': test_user.username,
        'project_members': [test_user.username],
        'private': 'public',
        'delete': False,
        'current': True,
        'FINISHED?': True,
        'runs': runs,
        'sample_count': len(runs),
        'tarfile': str(ObjectId()),
    })
    project_id = str(result.inserted_id)
    mongo_collection.update_one({'_id': result.inserted_id},
                                {'$set': {'linkid': project_id}})
    try:
        yield project_id
    finally:
        mongo_collection.delete_one({'_id': ObjectId(project_id)})


@pytest.fixture
def loaded_docs(monkeypatch):
    """Record every project document the view under test loads from MongoDB.

    Wraps the real loaders rather than replacing them — MongoDB is never mocked,
    the genuine query still runs, we only observe what came back.  Both loaders
    are wrapped so the assertion holds regardless of which one a view chooses.
    """
    from caper import views_apis

    seen = []

    def _spy(name):
        original = getattr(views_apis, name)

        def wrapper(*args, **kwargs):
            doc = original(*args, **kwargs)
            seen.append((name, doc))
            return doc

        return wrapper

    for fn in ('get_one_project', 'get_one_project_sans_runs'):
        if hasattr(views_apis, fn):
            monkeypatch.setattr(views_apis, fn, _spy(fn))
    return seen


def _assert_no_runs_loaded(loaded_docs):
    assert loaded_docs, "the view did not load a project document at all"
    for name, doc in loaded_docs:
        if doc is None:
            continue
        assert 'runs' not in doc, (
            f"{name}() returned a document carrying 'runs' "
            f"({len(doc.get('runs') or {})} samples). The whole feature-row "
            "payload was pulled from the database to serve a response that "
            "does not contain it — this is the read amplification that took "
            "production down."
        )


@pytest.fixture
def listed_docs(monkeypatch):
    """Record every document ProjectListView pulls via collection_handle.find().

    ProjectListView queries the collection directly rather than going through
    get_one_project*(), so the loaded_docs spy above cannot see it.  MongoDB is
    still never mocked — the real query runs, we only observe the results.
    """
    from caper import views_apis

    seen = []
    real = views_apis.collection_handle

    class _RecordingCollection:
        def __getattr__(self, name):
            return getattr(real, name)

        def find(self, *args, **kwargs):
            docs = list(real.find(*args, **kwargs))
            seen.extend(docs)
            return docs

    monkeypatch.setattr(views_apis, 'collection_handle', _RecordingCollection())
    return seen


@pytest.mark.integration
def test_project_list_does_not_fetch_runs(bulky_project, listed_docs):
    """
    The listing spans every public project, so leaving the projection off here
    pulls every feature row on the site to build a few KB of metadata — and it
    is the first call any client, crawler or agent makes.
    """
    from caper.views_apis import ProjectListView

    rf = APIRequestFactory()
    resp = ProjectListView.as_view()(rf.get('/api/v1/projects/'))

    assert resp.status_code == 200
    assert bulky_project in [p['id'] for p in resp.data], \
        "the listing dropped the project"
    assert listed_docs, "the view did not load any project documents"
    for doc in listed_docs:
        assert 'runs' not in doc, (
            f"ProjectListView loaded a project carrying 'runs' "
            f"({len(doc.get('runs') or {})} samples). Listing every public "
            "project this way multiplies the amplification by the number of "
            "projects on the site."
        )


@pytest.mark.integration
def test_project_list_response_fields_survive_the_projection(bulky_project, listed_docs):
    """The projection must not strip anything _project_to_dict() reports."""
    from caper.views_apis import ProjectListView

    rf = APIRequestFactory()
    resp = ProjectListView.as_view()(rf.get('/api/v1/projects/'))

    entry = next(p for p in resp.data if p['id'] == bulky_project)
    assert entry['project_name'] == 'ApiReadAmplificationTest'
    assert entry['sample_count'] == 3
    assert entry['visibility'] == 'public'
    assert entry['creator']


@pytest.mark.integration
def test_project_detail_does_not_fetch_runs(bulky_project, loaded_docs):
    """Project metadata is a few KB; it must not drag every feature row along."""
    from caper.views_apis import ProjectDetailView

    req = APIRequestFactory().get(f'/api/v1/projects/{bulky_project}/')
    resp = ProjectDetailView.as_view()(req, project_id=bulky_project)

    assert resp.status_code == 200
    assert resp.data['id'] == bulky_project
    assert resp.data['project_name'] == 'ApiReadAmplificationTest'
    assert resp.data['sample_count'] == 3
    _assert_no_runs_loaded(loaded_docs)


@pytest.mark.integration
def test_project_download_does_not_fetch_runs(bulky_project, loaded_docs):
    """The download endpoint reads one tarfile id — not the entire project."""
    from caper.views_apis import ProjectDownloadView

    req = APIRequestFactory().get(f'/api/v1/projects/{bulky_project}/download/')
    resp = ProjectDownloadView.as_view()(req, project_id=bulky_project)

    # Either a redirect/stream (archive resolvable) or a graceful error — what
    # matters is that resolving it did not require the runs payload.
    assert resp.status_code in (200, 302, 404, 503)
    _assert_no_runs_loaded(loaded_docs)


@pytest.mark.integration
def test_batch_download_does_not_fetch_runs(bulky_project, loaded_docs):
    """Batch resolution multiplies the amplification by the number of ids."""
    from caper.views_apis import ProjectBatchDownloadView

    req = APIRequestFactory().post('/api/v1/projects/download/',
                                   {'ids': [bulky_project]}, format='json')
    resp = ProjectBatchDownloadView.as_view()(req)

    assert resp.status_code == 200
    assert bulky_project in [d['id'] for d in resp.data['downloads']]
    _assert_no_runs_loaded(loaded_docs)


@pytest.mark.integration
def test_private_project_denies_anonymous_without_fetching_runs(
        mongo_collection, bulky_project, loaded_docs):
    """An anonymous caller must not be able to trigger the large read.

    The access check happens after the document load, so before the fix a
    rejected request still cost a full-document read — remotely triggerable
    with no credentials at all.
    """
    from caper.views_apis import ProjectDetailView

    mongo_collection.update_one({'_id': ObjectId(bulky_project)},
                                {'$set': {'private': 'private'}})

    req = APIRequestFactory().get(f'/api/v1/projects/{bulky_project}/')
    resp = ProjectDetailView.as_view()(req, project_id=bulky_project)

    assert resp.status_code == 401
    _assert_no_runs_loaded(loaded_docs)


@pytest.mark.integration
def test_project_samples_still_returns_every_sample(bulky_project):
    """The counter-case: samples ARE the payload here, so they must survive.

    Guards against over-applying the projection and silently emptying this
    endpoint — the failure mode that would make the fix worse than the bug.
    """
    from caper.views_apis import ProjectSamplesView

    req = APIRequestFactory().get(f'/api/v1/projects/{bulky_project}/samples/')
    resp = ProjectSamplesView.as_view()(req, project_id=bulky_project)

    assert resp.status_code == 200
    assert len(resp.data) == 3, "samples endpoint lost its payload"
    assert {s['Sample_name'] for s in resp.data} == {
        'SAMPLE_A', 'SAMPLE_B', 'SAMPLE_C'}
    # the bulk payload proves the real feature rows came through, not stubs
    assert all(len(s['_bulk_payload']) == 200_000 for s in resp.data)


@pytest.mark.integration
def test_project_samples_denies_anonymous_without_fetching_runs(
        mongo_collection, bulky_project, loaded_docs):
    """Authorization must precede the expensive read, even where it is legitimate.

    An anonymous caller could otherwise force the full runs read on any private
    project and receive only a 401 — a remotely triggerable read amplifier.
    """
    from caper.views_apis import ProjectSamplesView

    mongo_collection.update_one({'_id': ObjectId(bulky_project)},
                                {'$set': {'private': 'private'}})

    req = APIRequestFactory().get(f'/api/v1/projects/{bulky_project}/samples/')
    resp = ProjectSamplesView.as_view()(req, project_id=bulky_project)

    assert resp.status_code == 401
    _assert_no_runs_loaded(loaded_docs)
