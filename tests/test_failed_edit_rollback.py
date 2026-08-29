"""What a failed edit leaves behind.

When an edit's aggregation fails, the old version is restored and the
placeholder that was going to become the new version is marked failed. That
placeholder is a real document with a real URL: project_page() reads
``rollback_project_id`` off it to send the user back to the version that was
restored, so it has to stay reachable.

Until 2026-08-29 the rollback set ``current: False`` and left ``status`` at the
``LIVE`` the placeholder was inserted with. classify() reads the flags and says
DETACHED, so the stored field disagreed with the document it describes -- which
is invariant I2, and it came back as a finding every time an edit failed.

Measured 2026-08-29: 0 such documents on prod, 2 on caper-dev. What a failed
placeholder *ought* to be is still open; this only stops the record lying about
what it currently is.
"""

import io
import tarfile

import pytest
from bson.objectid import ObjectId

from conftest import (
    _build_create_request,
    _cleanup_project,
    _poll_until_finished,
    _project_id_from_redirect,
    DATASET_SMALL_TAR,
    DATASET_SMALL_XLSX,
)


def _unaggregatable_archive():
    """A real .tar.gz holding nothing the aggregator can use.

    Not a corrupt file: it has to get past extraction and fail in the
    aggregator, which is the path that rolls back. A file that failed to open
    would exercise a different branch.
    """
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as tar:
        note = b'not an AmpliconSuite result\n'
        info = tarfile.TarInfo('results/readme.txt')
        info.size = len(note)
        tar.addfile(info, io.BytesIO(note))
    payload.seek(0)
    payload.name = 'broken.tar.gz'
    return payload


@pytest.mark.slow
@pytest.mark.integration
def test_a_failed_edit_records_the_status_it_ends_up_in(
        request_factory, test_user, mongo_collection):
    from caper.views import create_project, edit_project_page
    from caper.project_status import LIVE, classify, is_reachable_by_url

    created = []
    try:
        request, handles = _build_create_request(
            request_factory, test_user, 'FailedEditRollback',
            tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
        try:
            response = create_project(request)
        finally:
            for handle in handles:
                handle.close()
        original = _project_id_from_redirect(response)
        created.append(original)
        assert _poll_until_finished(mongo_collection, original)

        broken = _unaggregatable_archive()
        edit = request_factory.post(
            f'/project/{original}/edit',
            data={
                'project_name': 'FailedEditRollback',
                'description': 'an edit that cannot aggregate',
                'private': 'private',
                'publication_link': '',
                'project_members': '',
                'alias': '',
                'remap_sample_names': 'false',
                # 'replace', not 'append': append downloads the old archive and
                # aggregates it alongside, so its result tables would satisfy the
                # aggregator and the edit would succeed. Replace skips that
                # download, leaving only the archive below -- which is the point.
                'project_mode': 'replace',
                'accept_license': 'on',
                'document': broken,
            },
            format='multipart')
        edit.user = test_user
        edit_response = edit_project_page(edit, project_name=original)

        placeholder_id = _project_id_from_redirect(edit_response)
        assert placeholder_id and placeholder_id != original
        created.append(placeholder_id)

        placeholder = _poll_until_finished(mongo_collection, placeholder_id)
        assert placeholder is not None, 'the failed placeholder never settled'
        if not placeholder.get('aggregation_failed'):
            pytest.skip('this archive aggregated; the rollback path did not run')

        # The point of the change: the stored status describes the document.
        assert placeholder.get('status') == classify(placeholder), (
            f"stored status {placeholder.get('status')!r} but the flags say "
            f"{classify(placeholder)} -- this is invariant I2, and it is what "
            f"every failed edit used to leave behind")

        # And the things that must remain true regardless of what that status is.
        assert is_reachable_by_url(placeholder), (
            'project_page() reads rollback_project_id off this document to '
            'redirect the user to the restored version; it cannot do that if '
            'the resolver will not return it')
        assert placeholder.get('rollback_project_id') == str(original)

        restored = mongo_collection.find_one({'_id': ObjectId(original)})
        assert classify(restored) == LIVE, \
            'a failed edit must give the old version back, live'
        assert restored.get('status') == classify(restored), \
            'the restore writes a status too, and it must not lie either'
        assert restored.get('runs'), 'the restored version lost its samples'

    finally:
        for project_id in created:
            _cleanup_project(mongo_collection, project_id)
