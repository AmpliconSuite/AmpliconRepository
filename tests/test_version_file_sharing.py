"""
A new version reuses its predecessor's identical files, and deleting a version
keeps what another version still names (#624).

Against the real local database and GridFS, through extract_project_files and
delete_project_version: the safety property here is about what the database
ends up holding, and a fake would only agree with itself.
"""

import json
import tarfile

import pytest
from bson.objectid import ObjectId

import caper.views as views
from caper import lineage, utils, version_purge
from caper.gridfs_backlinks import iter_backlinks
from caper.project_status import LIVE, SOFT_DELETED, SUPERSEDED, TOMBSTONE, classify, status_flags
from caper.project_version_cleanup import PENDING_PAYLOAD_KEY, iter_gridfs_file_ids
from caper.version_file_sharing import (
    REUSED_BY, SHA256, files_still_needed,
)

pytestmark = pytest.mark.integration

SAMPLES = ('S1', 'S2')


def _feature(sample, n, aggregator_7_names=False):
    feature = {
        'Sample name': sample,
        'Feature ID': f'{sample}_amplicon1_ecDNA_{n}',
        'Classification': 'ecDNA',
        'Oncogenes': [],
        'Feature BED file': f'{sample}/feature_{n}.bed',
        'AA PNG file': f'{sample}/amplicon1.png',
        'CNV BED file': f'{sample}/cnv.bed',
        'Sample metadata JSON': f'{sample}/sample_meta.json',
        'Reconstruction directory': f'{sample}/reconstruction',
    }
    if aggregator_7_names:
        feature['Graph PNG file'] = feature.pop('AA PNG file')
    return feature


def _build_tar(where, changed=(), samples=SAMPLES, aggregator_7_names=False):
    """Two samples of two features. Every file of a *changed* sample differs.

    Runs are keyed by position, as the aggregator keys them: removing a sample
    renumbers the ones after it.
    """
    src = where / 'src' / 'results'
    runs = {}
    for position, sample in enumerate(samples, 1):
        tag = 'changed' if sample in changed else 'same'
        features = runs[f'sample_{position}'] = [_feature(sample, n, aggregator_7_names) for n in (1, 2)]
        for path in {p for f in features for k, p in f.items() if k.endswith(('file', 'JSON'))}:
            (src / path).parent.mkdir(parents=True, exist_ok=True)
            (src / path).write_text(f'{path} {tag}\n')
        recon = src / sample / 'reconstruction'
        recon.mkdir(exist_ok=True)
        (recon / 'amplicon1_graph.txt').write_text(f'graph {tag}\n')
    (src / 'run.json').write_text(json.dumps({'runs': runs}))
    tar_path = where / 'upload.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(src, arcname='results')
    return str(tar_path)


@pytest.fixture
def versions(tmp_path, monkeypatch, test_user):
    """make(changed=()) -> a new version of one project, stored for real."""
    monkeypatch.setattr(views, 'project_content_changed', lambda project_id: None)
    monkeypatch.setattr(views, 'project_removed', lambda *a, **kw: None)
    monkeypatch.setattr(views, 'invalidate_project_coamp_graphs', lambda project_id: None)
    monkeypatch.setattr(views, 'get_tool_versions', lambda project, runs: None)
    monkeypatch.setattr(views, 'process_metadata_no_request', lambda runs, **kw: runs)
    monkeypatch.setattr(version_purge._thread_executor, 'submit',
                        lambda fn, *a, task_label=None, **kw: fn(*a, **kw))
    from django.conf import settings
    monkeypatch.setattr(settings, 'USE_S3_DOWNLOADS', False, raising=False)

    projects = utils.collection_handle_primary
    made = []

    def make(changed=(), samples=SAMPLES, aggregator_7_names=False):
        previous = [{'date': d['date'], 'linkid': str(d['_id'])} for d in made]
        doc = {'project_name': 'pytest version sharing', 'description': 'pytest',
               'private': 'private', 'project_members': [test_user.username],
               'date': f'2026-11-0{len(made) + 1}', 'runs': {}, 'FINISHED?': False,
               'previous_versions': previous, **status_flags(LIVE)}
        new_id = projects.insert_one(doc).inserted_id
        if made:
            projects.update_one({'_id': made[-1]['_id']}, {'$set': status_flags(SUPERSEDED)})
        lineage.link_new_version(projects, new_id, previous)
        where = tmp_path / str(new_id)
        (where / 'dest').mkdir(parents=True)
        views.extract_project_files(tarfile, _build_tar(where, changed, samples, aggregator_7_names), str(where / 'dest'),
                                    str(new_id), None, None, [])
        made.append(projects.find_one({'_id': new_id}))
        return made[-1]

    yield make

    from conftest import _cleanup_project
    for doc in reversed(made):
        _cleanup_project(projects, str(doc['_id']))


def _rows(ids):
    return {row['_id']: row for row in
            utils.db_handle_primary['fs.files'].find({'_id': {'$in': list(ids)}})}


def _slots(doc):
    return {(feature['Sample_name'], n, key): value
            for features in doc['runs'].values()
            for n, feature in enumerate(features)
            for key, value in feature.items() if isinstance(value, ObjectId)}


def _ids(doc):
    return set(iter_gridfs_file_ids(doc))


def test_an_unchanged_version_names_its_predecessors_files(versions):
    v1 = versions()
    v2 = versions()

    assert _slots(v1) and _slots(v2) == _slots(v1)
    rows = _rows(_ids(v1))
    assert len(rows) == len(_ids(v1))
    for row in rows.values():
        assert row['metadata']['project_id'] == v1['_id'], 'provenance stays the writer'
        assert row['metadata'][REUSED_BY] == [v2['_id']]
        assert row['metadata'][SHA256]
    assert utils.db_handle_primary['fs.files'].count_documents(
        {'metadata.project_id': v2['_id']}) == 0


def test_a_changed_sample_is_stored_again_and_the_rest_is_shared(versions):
    v1 = versions()
    v2 = versions(changed={'S2'})

    before, after = _slots(v1), _slots(v2)
    for slot, file_id in after.items():
        if slot[0] == 'S1':
            assert file_id == before[slot], slot
        else:
            assert file_id != before[slot], slot


def test_deleting_an_old_version_keeps_what_the_current_one_names(versions, request_factory, test_user):
    v1 = versions()
    v2 = versions(changed={'S2'})
    only_v1 = _ids(v1) - _ids(v2)
    assert only_v1 and _ids(v1) & _ids(v2)

    request = request_factory.post(f'/project/{v2["_id"]}/delete_version/{v1["_id"]}')
    request.user = test_user
    response = views.delete_project_version(request, str(v2['_id']), str(v1['_id']))

    assert response.status_code == 200
    assert classify(utils.collection_handle_primary.find_one({'_id': v1['_id']})) == TOMBSTONE
    assert set(_rows(_ids(v2))) == _ids(v2), 'the current version lost a file'
    assert not _rows(only_v1), 'files only the deleted version named should be gone'


def test_deleting_a_middle_version_keeps_what_an_older_one_names(versions, request_factory, test_user):
    """The case the view's own protection missed: it kept only the head's files.

    S1 is unchanged from v1 to v2 and changed in v3, so v1 and v2 share S1's
    files and the head names none of them.
    """
    v1 = versions()
    v2 = versions(changed={'S2'})
    v3 = versions(changed={'S1', 'S2'})
    shared_old = (_ids(v1) & _ids(v2)) - _ids(v3)
    assert shared_old

    request = request_factory.post(f'/project/{v3["_id"]}/delete_version/{v2["_id"]}')
    request.user = test_user
    response = views.delete_project_version(request, str(v3['_id']), str(v2['_id']))

    assert response.status_code == 200
    assert set(_rows(_ids(v1))) == _ids(v1), 'the oldest version lost a file'
    assert set(_rows(_ids(v3))) == _ids(v3)
    assert not _rows(_ids(v2) - _ids(v1) - _ids(v3))


def test_deleting_the_current_version_keeps_what_the_promoted_one_names(versions, request_factory, test_user):
    v1 = versions()
    v2 = versions(changed={'S2'})
    only_v2 = _ids(v2) - _ids(v1)

    request = request_factory.post(f'/project/{v2["_id"]}/delete_version/{v2["_id"]}')
    request.user = test_user
    response = views.delete_project_version(request, str(v2['_id']), str(v2['_id']))

    assert response.status_code == 200
    assert set(_rows(_ids(v1))) == _ids(v1), 'the promoted version lost a file'
    assert not _rows(only_v2)


def test_deleting_every_version_leaves_nothing_behind(versions, request_factory, test_user):
    v1 = versions()
    v2 = versions(changed={'S2'})
    everything = _ids(v1) | _ids(v2)

    for victim in (v1, v2):
        request = request_factory.post(f'/project/{v2["_id"]}/delete_version/{victim["_id"]}')
        request.user = test_user
        assert views.delete_project_version(
            request, str(v2['_id']), str(victim['_id'])).status_code == 200

    assert not _rows(everything)


def test_a_purge_asks_again_before_it_deletes(versions):
    """A pending list written before a later version reused the files."""
    v1 = versions()
    v2 = versions(changed={'S2'})
    pending = sorted(_ids(v1), key=str)
    utils.collection_handle_primary.update_one(
        {'_id': v1['_id']}, {'$set': {**status_flags(TOMBSTONE), PENDING_PAYLOAD_KEY: pending},
                             '$unset': {'runs': ''}})

    version_purge._run(str(v1['_id']), pending, None, None, {})

    assert set(_rows(_ids(v2))) == _ids(v2)
    assert not _rows(set(pending) - _ids(v2))
    tombstone = utils.collection_handle_primary.find_one({'_id': v1['_id']})
    assert tombstone.get(PENDING_PAYLOAD_KEY) == []


def test_a_version_still_being_written_keeps_what_it_claimed(versions):
    v1 = versions()
    projects, fs_files = utils.collection_handle_primary, utils.db_handle_primary['fs.files']
    claimed = next(iter(_ids(v1)))
    writing = projects.insert_one({'project_name': 'pytest version sharing writing',
                                   'FINISHED?': False, 'runs': {}, **status_flags(LIVE)}).inserted_id
    try:
        fs_files.update_one({'_id': claimed}, {'$addToSet': {f'metadata.{REUSED_BY}': writing}})
        assert files_still_needed(projects, fs_files, v1, _ids(v1)) == {claimed}

        projects.update_one({'_id': writing}, {'$set': {'aggregation_failed': True}})
        assert files_still_needed(projects, fs_files, v1, _ids(v1)) == set()
    finally:
        projects.delete_one({'_id': writing})


def test_a_reuser_outside_the_chain_is_still_asked(versions):
    """A failed edit is unlinked from its chain but can still name what it reused."""
    v1 = versions()
    v2 = versions()
    projects, fs_files = utils.collection_handle_primary, utils.db_handle_primary['fs.files']
    lineage.unlink_new_version(projects, v2['_id'])
    projects.update_one({'_id': v1['_id']}, {'$unset': {'previous_versions': ''}})

    assert files_still_needed(projects, fs_files, v1, _ids(v1)) == _ids(v1)


def test_a_failed_edit_does_not_delete_what_it_reused(versions, monkeypatch):
    """Both failure clean-ups take only what the failed upload itself wrote."""
    v1 = versions()

    class FailingSave:
        def update_one(self, query, update):
            raise RuntimeError('document save failed')

    real = views.collection_handle
    monkeypatch.setattr(views, 'collection_handle', FailingSave())
    v2 = versions(changed={'S2'})
    monkeypatch.setattr(views, 'collection_handle', real)
    views._discard_failed_upload(str(v2['_id']), str(v1['_id']))

    assert set(_rows(_ids(v1))) == _ids(v1), 'a failed edit deleted its predecessor\'s files'
    assert utils.db_handle_primary['fs.files'].count_documents(
        {'metadata.project_id': v2['_id']}) == 0, 'what the failed edit wrote should be gone'


def test_admin_permanent_delete_keeps_what_another_version_names(versions):
    from caper.views_admin import admin_permanent_delete_project
    v1 = versions()
    v2 = versions(changed={'S2'})

    admin_permanent_delete_project(str(v1['_id']), v1, v1['project_name'])

    assert utils.collection_handle_primary.find_one({'_id': v1['_id']}) is None
    assert set(_rows(_ids(v2))) == _ids(v2)
    assert not _rows(_ids(v1) - _ids(v2))


def _length(file_ids):
    return sum(row.get('length', 0) for row in _rows(file_ids).values())


def test_the_delete_page_promises_what_the_button_frees(versions):
    """v1 and v2 go together and v3 stays. What v3 shares with them is not freed;
    what v1 and v2 share only with each other is freed, and once."""
    from caper.views_admin import permanent_delete_size, permanently_delete_with_history
    projects = utils.collection_handle_primary
    v1 = versions()
    v2 = versions(changed={'S2'})
    v3 = versions(changed={'S1', 'S2'})
    for doc in (v1, v2):
        projects.update_one({'_id': doc['_id']}, {'$set': status_flags(SOFT_DELETED)})
    v2 = projects.find_one({'_id': v2['_id']})
    freed = (_ids(v1) | _ids(v2)) - _ids(v3)
    assert _ids(v1) & _ids(v2) - _ids(v3), 'the case of a file only the victims share'
    assert _ids(v2) & _ids(v3), 'the case of a file the survivor keeps'

    promised = permanent_delete_size(v2)

    assert promised['versions'] == 2
    assert promised['files'] == len(freed)
    assert promised['bytes'] == _length(freed)

    before = _rows(_ids(v1) | _ids(v2) | _ids(v3))
    permanently_delete_with_history(str(v2['_id']), v2, v2['project_name'])
    after = _rows(before)
    gone = set(before) - set(after)

    assert gone == freed, 'the page and the button disagree about which files go'
    assert sum(before[f].get('length', 0) for f in gone) == promised['bytes']
    assert _ids(v3) <= set(after)


def test_a_delete_page_for_a_version_with_a_survivor_is_not_overstated(versions):
    from caper.views_admin import permanent_delete_size
    v1 = versions()
    v2 = versions(changed={'S2'})

    promised = permanent_delete_size(v1)

    assert promised['files'] == len(_ids(v1) - _ids(v2))
    assert promised['bytes'] == _length(_ids(v1) - _ids(v2))


# --- Files stored before uploads recorded a hash ---

def _as_stored_before_the_hash(doc, form):
    """Make *doc*'s rows look like prod's files stored before #718.

    No hash, and the metadata of one of the two writers measured on prod
    2026-11-03: the August backlink backfill wrote the document's spelling of
    the field ('Feature_BED_file') and the sample name; uploads since wrote the
    run.json spelling and the run key ('sample_1').
    """
    run_key = {value: key for key, features in doc['runs'].items()
               for feature in features for value in feature.values()
               if isinstance(value, ObjectId)}
    fs_files = utils.db_handle_primary['fs.files']
    first = {}
    for file_id, sample_name, key in iter_backlinks(doc):
        first.setdefault(file_id, (sample_name, key))     # both writers: first slot wins
    for file_id, (sample_name, key) in first.items():
        if form == 'august backfill':
            metadata = {'feature_key': key, 'sample_name': sample_name}
        else:
            metadata = {'feature_key': key.replace('_', ' '), 'sample_name': run_key[file_id]}
        fs_files.update_one({'_id': file_id},
                            {'$set': {f'metadata.{k}': v for k, v in metadata.items()},
                             '$unset': {f'metadata.{SHA256}': ''}})


def test_an_aggregator_6_field_is_shared_under_its_7_0_name(versions):
    """'AA PNG file' became 'Graph PNG file'; the bytes did not change."""
    v1 = versions()
    v2 = versions(aggregator_7_names=True)

    for (sample, n, key), file_id in _slots(v1).items():
        if key == 'AA_PNG_file':
            assert _slots(v2)[(sample, n, 'Graph_PNG_file')] == file_id


def test_files_with_no_recorded_hash_are_not_shared(versions):
    v1 = versions()
    _as_stored_before_the_hash(v1, 'august backfill')

    v2 = versions()

    assert not set(_slots(v2).values()) & set(_slots(v1).values())


def test_removing_a_sample_still_shares_the_ones_after_it(versions):
    """The run keys renumber, so S2 is sample_2 in v1 and sample_1 in v2."""
    v1 = versions(samples=('S1', 'S2'))
    v2 = versions(samples=('S2',))

    s2 = {slot: file_id for slot, file_id in _slots(v1).items() if slot[0] == 'S2'}
    assert s2 and _slots(v2) == s2
