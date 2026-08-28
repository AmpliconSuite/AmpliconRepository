"""Every page, against every status a project document can be in.

This file exists because of a specific mistake.  An emptied project's page was
reported as rendering correctly on the strength of evaluating
``is_empty_project(doc)`` and finding it True.  The branch that reads is at
``views.py:882``; ``validate_project()`` runs at 871 and subscripted
``project['runs']``, which a tombstone does not have.  Every emptied project's
page was a 500, and the check that was supposed to catch it never rendered
anything.

Evaluating a predicate is not exercising a code path.  So: render the page.

The rule each case asserts is deliberately weak, because it is the one that
actually matters and the one that holds for every combination.  **No view may
raise.**  A 404, a 403, a redirect, an error page -- all fine, all decisions
the view is entitled to make.  An unhandled exception is not a decision; it is
a 500 in front of a user, and it is what every defect this file was written
after looked like.

The status codes themselves are printed as a table rather than asserted, since
what they *should* be is a product question per cell and pinning them here
would turn every deliberate change into a failure.  A human reading the table
can see whether a cell moved.
"""

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

from caper.project_status import (
    DETACHED, LIVE, SOFT_DELETED, SUPERSEDED, TOMBSTONE, status_flags,
)

# delete/current for the statuses that are a flag pair. DETACHED is not in
# status_flags() on purpose -- the module refuses to write it -- so its flags
# are spelled here, which is the only place in the tree that should.
FLAGS_BY_STATUS = {
    LIVE: status_flags(LIVE),
    SUPERSEDED: status_flags(SUPERSEDED),
    SOFT_DELETED: status_flags(SOFT_DELETED),
    DETACHED: {'delete': False, 'current': False, 'status': DETACHED},
}


def _views_under_test():
    """(label, callable) for every view that takes a project by name."""
    from caper import views
    return [
        ('project_page', lambda r, pid, sample: views.project_page(r, project_name=pid)),
        ('edit_page', lambda r, pid, sample: views.edit_project_page(r, project_name=pid)),
        ('download', lambda r, pid, sample: views.project_download(r, project_name=pid)),
        ('metadata_dl', lambda r, pid, sample: views.project_metadata_download(r, project_name=pid)),
        ('summary_dl', lambda r, pid, sample: views.project_summary_download(r, project_name=pid)),
        ('sample_page', lambda r, pid, sample: views.sample_page(
            r, project_name=pid, sample_name=sample)),
    ]


def _render(view, request_factory, user, project_id, sample_name):
    """Call one view and classify what came back. Never re-raises."""
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware

    # HTTP_HOST because RequestFactory defaults to 'testserver', which is not
    # in ALLOWED_HOSTS -- any view that builds an absolute URL then raises
    # DisallowedHost, which is the harness's fault and not the view's.
    request = request_factory.get(f'/project/{project_id}', HTTP_HOST='localhost')
    request.user = user
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)

    try:
        response = view(request, project_id, sample_name)
    except Exception as exc:                      # noqa: BLE001 -- classifying
        name = type(exc).__name__
        if 'Http404' in name:
            return 'Http404', None                # a decision, not a crash
        if 'TokenRetrieval' in name or 'NoCredentials' in name:
            return 'no-aws', None                 # environment, not the view
        return f'RAISED {name}', f'{name}: {exc}'
    if hasattr(response, 'render'):
        try:
            response.render()
        except Exception as exc:                  # noqa: BLE001
            return f'RAISED {type(exc).__name__} (template)', f'{exc}'
    return f'HTTP {response.status_code}', None


@pytest.mark.slow
@pytest.mark.integration
def test_no_page_raises_for_any_project_status(
        request_factory, test_user, mongo_collection, capsys):
    """The matrix. One real project, walked through each status in place.

    In place rather than as copies, because copies would share the original's
    GridFS ids and any cleanup that deleted one would strand the other. The
    flags are restored to LIVE after each case and the project is deleted at
    the end.
    """
    from caper.views import create_project
    from caper.project_version_cleanup import build_deleted_version_tombstone

    request, handles = _build_create_request(
        request_factory, test_user, 'PageRenderMatrix',
        tar_path=DATASET_SMALL_TAR, xlsx_path=DATASET_SMALL_XLSX)
    try:
        response = create_project(request)
    finally:
        for handle in handles:
            handle.close()
    project_id = _project_id_from_redirect(response)

    tombstone_id = None
    try:
        doc = _poll_until_finished(mongo_collection, project_id)
        assert doc and not doc.get('aggregation_failed'), \
            f"create failed: {doc.get('error_message') if doc else 'timed out'}"

        runs = doc.get('runs') or {}
        sample_name = next(
            (features[0]['Sample_name'] for features in runs.values()
             if features and features[0].get('Sample_name')), 'no_such_sample')

        # A tombstone cannot be made by setting flags: it is a different
        # document shape, with no runs and no GridFS ids. It gets its own _id
        # so the original keeps the payload both of them would otherwise name.
        tombstone = build_deleted_version_tombstone(
            doc, None, 'matrix', '2026-08-28T00:00:00.000000')
        tombstone['_id'] = ObjectId()
        mongo_collection.insert_one(tombstone)
        tombstone_id = tombstone['_id']

        cases = [(name, project_id, flags) for name, flags in FLAGS_BY_STATUS.items()]
        cases.append((TOMBSTONE, str(tombstone_id), None))

        views = _views_under_test()
        table = {}
        crashes = []
        for status, target, flags in cases:
            if flags is not None:
                mongo_collection.update_one({'_id': ObjectId(target)},
                                            {'$set': flags})
            for label, view in views:
                outcome, detail = _render(view, request_factory, test_user,
                                          target, sample_name)
                table[(status, label)] = outcome
                if outcome.startswith('RAISED'):
                    crashes.append(f'{status} / {label}: {detail}')

        mongo_collection.update_one({'_id': ObjectId(project_id)},
                                    {'$set': status_flags(LIVE)})

        width = max(len(label) for label, _ in views) + 2
        header = 'status'.ljust(14) + ''.join(
            label.ljust(width) for label, _ in views)
        lines = ['', header, '-' * len(header)]
        for status, _target, _flags in cases:
            lines.append(status.ljust(14) + ''.join(
                table[(status, label)].ljust(width) for label, _ in views))
        with capsys.disabled():
            print('\n'.join(lines) + '\n')

        assert not crashes, (
            'a view raised rather than deciding what to do:\n  '
            + '\n  '.join(crashes))

    finally:
        if tombstone_id is not None:
            # Safe to remove directly: a tombstone names no GridFS files, so
            # there is nothing of the original's for this to take with it.
            mongo_collection.delete_one({'_id': tombstone_id})
        _cleanup_project(mongo_collection, project_id)
