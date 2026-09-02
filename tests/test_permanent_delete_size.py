"""What the permanent-delete page promises must be what the button removes.

The page showed the head version's tarfile and called it the size. Permanent
delete removes the whole chain -- the versions the history names and the
tombstoned ancestors the array cannot name -- and on this site superseded
versions are 64% of production, so for anything with history the figure shown
was a fraction of what the button did. It is the number an operator reads
before an irreversible action.
"""

import inspect

import pytest
from bson import ObjectId

from caper import views_admin


class _FakeFiles:
    def __init__(self, lengths):
        self.lengths = lengths
        self.queries = 0

    def find(self, query, projection=None):
        self.queries += 1
        wanted = query['_id']['$in']
        return [{'_id': i, 'length': self.lengths[i]}
                for i in wanted if i in self.lengths]


@pytest.fixture
def wired(monkeypatch):
    """Chain of three versions, each holding one GridFS file."""
    ids = [ObjectId() for _ in range(3)]
    files = [ObjectId() for _ in range(3)]
    docs = {
        str(ids[0]): {'_id': ids[0], 'project_name': 'v1', 'tarfile': files[0]},
        str(ids[1]): {'_id': ids[1], 'project_name': 'v2', 'tarfile': files[1]},
    }
    head = {'_id': ids[2], 'project_name': 'v3', 'tarfile': files[2],
            'previous_versions': [{'linkid': str(ids[0])},
                                  {'linkid': str(ids[1])}]}

    fake_files = _FakeFiles({files[0]: 100, files[1]: 200, files[2]: 400})

    monkeypatch.setattr(views_admin, 'get_one_deleted_project',
                        lambda linkid: docs.get(str(linkid)))
    monkeypatch.setattr(views_admin, '_tombstoned_ancestors', lambda project: [])
    monkeypatch.setattr(views_admin, 'db_handle', {'fs.files': fake_files})
    return head, fake_files


def test_the_total_covers_the_history_not_just_the_head(wired):
    head, _files = wired

    removal = views_admin.permanent_delete_size(head)

    assert removal['bytes'] == 700, (
        'the head tarfile alone is 400; the two older versions are the other 300')
    assert removal['versions'] == 3
    assert removal['files'] == 3
    assert removal['unresolved'] == []


def test_a_project_with_no_history_still_counts_itself():
    """One version, not zero."""
    project = {'_id': ObjectId(), 'project_name': 'solo'}
    targets, unresolved = views_admin._permanent_delete_targets(project)
    assert len(targets) == 1
    assert targets[0][1] is project
    assert unresolved == []


def test_a_dangling_history_entry_is_reported_and_adds_nothing(monkeypatch):
    """The entry names a version that is already gone, so there is nothing to free."""
    project = {'_id': ObjectId(), 'project_name': 'p',
               'previous_versions': [{'linkid': 'gone'}]}
    monkeypatch.setattr(views_admin, 'get_one_deleted_project', lambda linkid: None)
    monkeypatch.setattr(views_admin, '_tombstoned_ancestors', lambda p: [])
    monkeypatch.setattr(views_admin, 'db_handle', {'fs.files': _FakeFiles({})})

    removal = views_admin.permanent_delete_size(project)

    assert removal['unresolved'] == ['gone']
    assert removal['versions'] == 1
    assert removal['bytes'] == 0


def test_a_file_named_twice_is_counted_once(monkeypatch):
    """Two versions naming the same blob free it once, not twice."""
    shared = ObjectId()
    older_id = ObjectId()
    older = {'_id': older_id, 'project_name': 'v1', 'tarfile': shared}
    head = {'_id': ObjectId(), 'project_name': 'v2', 'tarfile': shared,
            'previous_versions': [{'linkid': str(older_id)}]}

    monkeypatch.setattr(views_admin, 'get_one_deleted_project',
                        lambda linkid: older if str(linkid) == str(older_id) else None)
    monkeypatch.setattr(views_admin, '_tombstoned_ancestors', lambda p: [])
    monkeypatch.setattr(views_admin, 'db_handle',
                        {'fs.files': _FakeFiles({shared: 500})})

    removal = views_admin.permanent_delete_size(head)

    assert removal['bytes'] == 500
    assert removal['files'] == 1


def test_the_size_and_the_delete_walk_the_same_documents():
    """One definition, not two.

    If these ever diverge, the page tells an operator one thing and the button
    does another -- and the action cannot be undone.
    """
    source = inspect.getsource(views_admin.permanently_delete_with_history)
    assert '_permanent_delete_targets(project)' in source, (
        'the delete must build its target list from the shared walk')
    assert 'iter_previous_versions(project)' not in source, (
        'the delete has grown a second traversal; permanent_delete_size would '
        'no longer describe what it removes')

    size_source = inspect.getsource(views_admin.permanent_delete_size)
    assert '_permanent_delete_targets(project)' in size_source


def test_the_page_carries_byte_counts_for_sorting():
    """A formatted size sorts lexicographically: 9.4 MB above 40.2 GB."""
    from django.conf import settings
    import os

    path = os.path.join(settings.BASE_DIR, 'templates', 'pages',
                        'admin_delete_project.html')
    with open(path) as handle:
        markup = handle.read()

    assert 'data-order="{{ project.tar_file_bytes }}"' in markup
    assert 'data-order="{{ project.delete_bytes }}"' in markup
