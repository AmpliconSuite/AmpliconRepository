"""Replacing a project's results with a new archive, from a script, for site admins.

``amprepo_reclassify`` moves projects to a new AmpliconClassifier release from an
operator's machine. The edit form needs a browser session, so this gives a token the
form's "replace" with nothing else changed. Metadata, description, alias, publication
link and subscribers carry forward because this calls the code the form calls
(``form_dict_for_edit``, ``edit_project_into_new_version``). So do the members: an admin
refreshing someone's project doesn't join it, as with the form. The admin is the new
version's creator, and the audit log also records who did it. A new version's view and download counts start at zero, and the project's total is the sum
across its versions.

A real replacement refuses a project that has moved on since the archive was built.
A dry run answers what a replacement would do, for review before anything is written.
It only reads the project and fs.files. Its files and result are in ``tmp/<job id>/``,
where the temp directory cleanup removes them about an hour later, and the task
tracker's own row is its one database write.

A dry run reads the uploaded archive, though the site aggregates that archive again
during a real replacement. Re-aggregating with the same aggregator leaves the files
byte-identical (measured 2026-11-02, see version_file_sharing), so a difference from
the prediction after an upload means that assumption failed.
"""

import json
import logging
import os
import shutil
import tarfile

import gridfs
from bson.objectid import ObjectId
from django.http import HttpResponseRedirect, QueryDict

from . import lineage
from .extra_metadata import get_extra_metadata_from_project
from .gridfs_backlinks import iter_backlinks
from .project_record_size import LIMIT_BYTES, size_of_new_version
from .project_status import LIVE, STATUS_FLAG_FIELDS, classify
from .project_version_cleanup import FEATURE_FILE_KEYS
from .tar_safety import safe_extractall
from .tar_utils import pack_directory_targz
from .utils import (collection_handle_primary, db_handle_primary, get_one_project_sans_runs,
                    normalize_visibility_field, validate_project)
from .version_file_sharing import CHUNK, PredecessorFiles, file_digest

RUNNING, FINISHED, FAILED = 'running', 'finished', 'failed'
_RESULT_FILE = 'dry_run.json'
_MISSING_SHOWN = 20
_ARCHIVE_KEY = 'tarfile'

# What the aggregator writes for an artifact a run didn't produce. Ingest treats these as
# nothing to store.
_NOT_A_FILE = ('Not Provided', 'not provided', '', None)

_HEAD_PROJECTION = dict(lineage.POINTER_PROJECTION, **{field: 1 for field in STATUS_FLAG_FIELDS})


class Refusal(Exception):
    """A request that can't go ahead. ``code`` is what a program branches on."""

    def __init__(self, message, code, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


def current_head(project_id):
    """The whole current version of this project's chain, read from the primary, or None.

    From the primary because the check it serves is whether someone uploaded moments ago.
    """
    found = get_one_project_sans_runs(project_id, _HEAD_PROJECTION)
    if not found:
        return None
    members = lineage.chain_members(collection_handle_primary, found, _HEAD_PROJECTION)
    head_id = lineage.head(members)['_id'] if members else found['_id']
    head = collection_handle_primary.find_one({'_id': head_id})
    if head is not None:
        head['linkid'] = str(head['_id'])
    return head


def refusal_for(head, expected_latest_id):
    """Why this version can't be replaced now, or None."""
    latest_id = str(head['_id'])
    if head.get('aggregation_in_progress') or head.get('FINISHED?') is False:
        return Refusal('The current version is still being processed.', 'in_progress',
                       latest_id=latest_id)
    if classify(head) != LIVE:
        return Refusal('This project has been deleted.', 'not_live', latest_id=latest_id)
    if expected_latest_id != latest_id:
        return Refusal('The project has a newer version than the one this archive was built '
                       'from. Plan and build it again.', 'stale_version', latest_id=latest_id)
    return None


def _form_fields(project):
    """What the edit page would post to replace results and change nothing else."""
    return {
        'project_name': project.get('project_name', ''),
        'description': project.get('description') or '',
        'publication_link': project.get('publication_link') or '',
        'private': normalize_visibility_field(project.get('private', 'private')),
        'project_members': ','.join(project.get('project_members') or []),
        'alias': '',
        'accept_license': 'on',
        'project_mode': 'replace',
        'retain_old_metadata': 'true',
        # An unticked checkbox posts nothing, so this has to be said either way.
        'keep_current': 'true' if project.get('keep_current') else 'false',
    }


def form_for(project):
    """The edit form filled in from the project itself. Check ``is_valid()`` before using it."""
    from .forms import UpdateForm

    post = QueryDict(mutable=True)
    post.update(_form_fields(project))
    return UpdateForm(post), post


def start_replacement(django_request, user, project):
    """Start the new version, in the background as the form does, and return its id.

    *django_request* must hold the uploaded ``document``, and ``metadataFile`` if there is one.
    """
    from .views import edit_project_into_new_version, form_dict_for_edit

    form, post = form_for(project)
    if not form.is_valid():
        raise Refusal('The stored project does not pass the edit form: %s' % form.errors.as_json(),
                      'invalid_project')
    django_request.POST = post
    django_request.user = user
    form_dict = form_dict_for_edit(django_request, project, form)

    new_id = str(ObjectId())
    result = edit_project_into_new_version(
        django_request, str(project['_id']), project, form_dict, form,
        django_request.FILES.get('metadataFile'), [], False, [], new_id, f'tmp/{new_id}',
        keep_members=project.get('project_members') or [])
    if not isinstance(result, HttpResponseRedirect):
        # The form's own failure paths have put the old version back by this point.
        raise Refusal('The new version could not be started. The project is unchanged.',
                      'not_started')
    return new_id


# ---- Dry run ----

def _job_dir(job_id):
    return os.path.join('tmp', job_id)


def _write_result(job_id, result):
    path = os.path.join(_job_dir(job_id), _RESULT_FILE)
    with open(path + '.part', 'w') as handle:
        json.dump(result, handle)
    os.replace(path + '.part', path)


def _save(upload, directory, name):
    path = os.path.join(directory, name)
    with open(path, 'wb') as destination:
        for chunk in upload.chunks():
            destination.write(chunk)
    return path


def start_dry_run(archive, metadata_file, project):
    """Save the upload where a background task can read it, and return the job's id."""
    from .background_tasks import _thread_executor

    form, _post = form_for(project)
    if not form.is_valid():
        raise Refusal('The stored project does not pass the edit form: %s' % form.errors.as_json(),
                      'invalid_project')
    job_id = str(ObjectId())
    work_dir = os.path.join(_job_dir(job_id), 'work')
    os.makedirs(work_dir)
    # The request's temporary upload is deleted when the response is sent.
    archive_path = _save(archive, work_dir, 'upload.tar.gz')
    metadata_path = (_save(metadata_file, work_dir, os.path.basename(metadata_file.name))
                     if metadata_file else None)
    _write_result(job_id, {'state': RUNNING, 'dry_run': True, 'project_id': str(project['_id'])})
    _thread_executor.submit(_run_dry_run, job_id, str(project['_id']), archive_path, metadata_path,
                            task_label=f'Replace dry run: {job_id}')
    return job_id


def _run_dry_run(job_id, project_id, archive_path, metadata_path):
    base = {'dry_run': True, 'project_id': project_id}
    work_dir = os.path.dirname(archive_path)
    try:
        project = collection_handle_primary.find_one({'_id': ObjectId(project_id)})
        report = predict(project, archive_path, metadata_path, os.path.join(work_dir, 'unpacked'))
        _write_result(job_id, {**base, 'state': FINISHED, **report})
    except Exception as error:
        logging.exception('Replace dry run %s failed', job_id)
        _write_result(job_id, {**base, 'state': FAILED, 'error': str(error)})
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def predict(project, archive_path, metadata_path, unpack_dir):
    """What replacing *project* with this archive would store. Writes nothing to the database."""
    from .views import get_tool_versions, samples_to_dict

    os.makedirs(unpack_dir)
    with tarfile.open(archive_path, 'r:gz') as archive:
        safe_extractall(archive, unpack_dir, description='replace dry run')
    run_path = os.path.join(unpack_dir, 'results', 'run.json')
    if not os.path.exists(run_path):
        raise ValueError('The archive has no results/run.json.')
    with open(run_path) as handle:
        runs = samples_to_dict(handle)

    old_metadata = None if metadata_path else get_extra_metadata_from_project(project)
    record_bytes = size_of_new_version(archive_path, metadata_path, old_metadata)
    versions = {}
    get_tool_versions(versions, runs)
    return {
        'current': {'id': str(project['_id']), 'samples': project.get('sample_count'),
                    'AC_version': project.get('AC_version')},
        'samples': len(runs),
        'features': sum(len(features) for features in runs.values()),
        'versions': versions,
        'record': {'bytes': record_bytes, 'limit_bytes': LIMIT_BYTES,
                   'fits': record_bytes is None or record_bytes <= LIMIT_BYTES},
        'metadata': 'new sheet' if metadata_path else 'retained' if old_metadata else 'none',
        'files': {**_predict_files(project, runs, os.path.join(unpack_dir, 'results')),
                  # Always new, because it holds the version's id. The site's aggregation
                  # makes it about this size: 4 bytes apart in the test on 2026-11-06.
                  'archive_bytes': os.path.getsize(archive_path)},
    }


def _predict_files(project, runs, results_dir):
    """Which files would be shared with *project* and which written.

    Follows the storing loop in views.extract_project_files, so a change there needs one
    here. tests/test_project_replace.py compares this with what a real replacement stored.
    """
    predecessor = PredecessorFiles.of_version(db_handle_primary['fs.files'], project)
    shared_ids, seen, missing = set(), set(), []
    shared_bytes, new_files, new_bytes = 0, 0, 0

    def count(fileobj, sample_name, key, filename=None):
        nonlocal shared_bytes, new_files, new_bytes
        sha256, length = file_digest(fileobj)
        reused = predecessor.would_reuse(sha256, length, filename, sample_name, key)
        if reused is None:
            new_files += 1
            new_bytes += length
        elif reused not in shared_ids:
            shared_ids.add(reused)
            shared_bytes += length

    for sample, features in runs.items():
        for feature in features:
            if not feature:
                continue
            sample_name = feature.get('Sample name') or sample
            for key in FEATURE_FILE_KEYS:
                path = feature.get(key)
                if key not in feature or (isinstance(path, (str, type(None))) and path in _NOT_A_FILE):
                    continue
                if (sample, key, path) in seen:
                    continue
                seen.add((sample, key, path))
                try:
                    with open(os.path.join(results_dir, path), 'rb') as handle:
                        count(handle, sample_name, key)
                except OSError:
                    missing.append('%s: %s' % (sample_name, key))

            key = 'Reconstruction directory' if 'Reconstruction directory' in feature else 'AA directory'
            path = feature.get(key)
            if path is None or (sample, key, path) in seen:
                continue
            seen.add((sample, key, path))
            full_path = os.path.join(results_dir, path)
            try:
                if os.path.isdir(full_path):
                    name = os.path.basename(path.rstrip('/'))
                    count(pack_directory_targz(full_path, name), sample_name, key,
                          filename=f'{name}.tar.gz')
                else:
                    with open(full_path, 'rb') as handle:
                        count(handle, sample_name, key)
            except OSError:
                missing.append('%s: %s' % (sample_name, key))

    return {'shared': len(shared_ids), 'shared_bytes': shared_bytes,
            'new': new_files, 'new_bytes': new_bytes,
            'missing': len(missing), 'missing_examples': missing[:_MISSING_SHOWN]}


# ---- Status of either kind of job ----

def job_status(job_id):
    """What a dry run or a replacement has got to, or None for an id that isn't one."""
    if not ObjectId.is_valid(job_id):
        return None
    result_path = os.path.join(_job_dir(str(ObjectId(job_id))), _RESULT_FILE)
    if os.path.exists(result_path):
        with open(result_path) as handle:
            return json.load(handle)
    return _replacement_status(ObjectId(job_id))


def _replacement_status(version_id):
    summary = collection_handle_primary.find_one(
        {'_id': version_id},
        {'FINISHED?': 1, 'aggregation_failed': 1, 'error_message': 1, 'rollback_project_id': 1})
    if summary is None:
        return None
    base = {'dry_run': False, 'project_id': str(version_id)}
    if summary.get('aggregation_failed'):
        return {**base, 'state': FAILED, 'error': summary.get('error_message') or 'Aggregation failed.',
                'restored_project_id': summary.get('rollback_project_id')}
    if not summary.get('FINISHED?'):
        return {**base, 'state': RUNNING}

    version = collection_handle_primary.find_one({'_id': version_id})
    return {**base, 'state': FINISHED,
            'page_ready': 'metadata_stored' in version,
            'page_fields': {'sample_data': len(version.get('sample_data') or []),
                            'reference_genome': version.get('reference_genome'),
                            'ecDNA_context': len(version.get('ecDNA_context') or {})},
            'samples': version.get('sample_count'),
            'features': sum(len(features) for features in (version.get('runs') or {}).values()),
            'versions': {key: version.get(key) for key in (
                'AA_version', 'AC_version', 'ASP_version', 'Reconstruction_tools', 'CoRAL_version')},
            'files': _stored_files(version)}


def prepare_page(version_id):
    """Start storing what this finished version's page needs. True if that's already done,
    False if it has started, None for an id that isn't a version."""
    from .background_tasks import _thread_executor

    if not ObjectId.is_valid(version_id):
        return None
    version_id = ObjectId(version_id)
    summary = collection_handle_primary.find_one(
        {'_id': version_id}, {'FINISHED?': 1, 'aggregation_failed': 1, 'metadata_stored': 1})
    if summary is None:
        return None
    if summary.get('aggregation_failed') or not summary.get('FINISHED?'):
        raise Refusal('This version has not finished successfully.', 'not_finished')
    if 'metadata_stored' in summary:
        return True
    _thread_executor.submit(_store_page_fields, version_id,
                            task_label=f'Replace page fields: {version_id}')
    return False


def _store_page_fields(version_id):
    from .views import store_page_fields

    project = collection_handle_primary.find_one({'_id': version_id})
    project = validate_project(project, str(version_id))
    # From the primary: this can run moments after the archive was written, and a read that
    # fails stores an empty ecDNA context for good.
    store_page_fields(project, fs=gridfs.GridFS(db_handle_primary))


def _stored_files(version):
    """Which of this version's files it shares with the version before it, and which it wrote.

    The project's whole archive is counted apart. It's always new, because it holds the
    version's id.
    """
    file_ids, archive_ids = set(), set()
    for file_id, _sample, key in iter_backlinks(version):
        (archive_ids if key == _ARCHIVE_KEY else file_ids).add(file_id)
    previous_id = version.get('previous_version_id')
    previous = collection_handle_primary.find_one({'_id': previous_id}) if previous_id else None
    shared = file_ids & {file_id for file_id, _sample, _key in iter_backlinks(previous or {})}

    lengths = {}
    ordered = list(file_ids | archive_ids)
    for start in range(0, len(ordered), CHUNK):
        for row in db_handle_primary['fs.files'].find(
                {'_id': {'$in': ordered[start:start + CHUNK]}}, {'length': 1}):
            lengths[row['_id']] = row.get('length') or 0
    return {'shared': len(shared), 'shared_bytes': sum(lengths.get(i, 0) for i in shared),
            'new': len(file_ids - shared),
            'new_bytes': sum(lengths.get(i, 0) for i in file_ids - shared),
            'archive_bytes': sum(lengths.get(i, 0) for i in archive_ids)}
