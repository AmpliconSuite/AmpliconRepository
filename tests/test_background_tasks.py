import os
import time

from caper import background_tasks as bt


class _TaskCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.inserted = []

    def find(self, query):
        return self.docs

    def create_index(self, *args, **kwargs):
        return None

    def insert_one(self, doc):
        self.inserted.append(doc)

    def update_one(self, *args, **kwargs):
        return None


def _make_old(path, seconds=600):
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_cleanup_stale_temp_dirs_removes_only_old_orphans(tmp_path, monkeypatch):
    tmp_root = tmp_path / 'tmp'
    tmp_root.mkdir()
    old_orphan = tmp_root / 'old_orphan'
    active = tmp_root / 'active'
    recent = tmp_root / 'recent'
    old_orphan.mkdir()
    active.mkdir()
    recent.mkdir()
    _make_old(old_orphan)
    _make_old(active)

    monkeypatch.setattr(
        bt,
        '_get_tasks_collection',
        lambda: _TaskCollection([{'temp_dir': str(active)}]),
    )

    removed = bt.cleanup_stale_temp_dirs(str(tmp_root), min_age_seconds=300)

    assert removed == 1
    assert not old_orphan.exists()
    assert active.is_dir()
    assert recent.is_dir()


def test_remove_temp_dir_refuses_paths_outside_tmp_root(tmp_path):
    tmp_root = tmp_path / 'tmp'
    outside = tmp_path / 'outside'
    tmp_root.mkdir()
    outside.mkdir()

    removed = bt._remove_temp_dir(str(outside), str(tmp_root))

    assert removed is False
    assert outside.is_dir()


def test_remove_temp_dir_refuses_tmp_root_itself(tmp_path):
    tmp_root = tmp_path / 'tmp'
    tmp_root.mkdir()

    removed = bt._remove_temp_dir(str(tmp_root), str(tmp_root))

    assert removed is False
    assert tmp_root.is_dir()


def test_remove_temp_dir_refuses_symlink_without_following(tmp_path):
    tmp_root = tmp_path / 'tmp'
    outside = tmp_path / 'outside'
    symlink_path = tmp_root / 'linked_outside'
    tmp_root.mkdir()
    outside.mkdir()
    symlink_path.symlink_to(outside, target_is_directory=True)

    removed = bt._remove_temp_dir(str(symlink_path), str(tmp_root))

    assert removed is False
    assert symlink_path.is_symlink()
    assert outside.is_dir()


def test_submit_removes_temp_dir_after_task_completion(tmp_path):
    tmp_root = tmp_path / 'tmp'
    temp_dir = tmp_root / 'task'
    tmp_root.mkdir()
    temp_dir.mkdir()
    tracker = bt.BackgroundTaskTracker(
        max_workers=1,
        cleanup_interval_seconds=60 * 60 * 24,
        tmp_root=str(tmp_root),
    )
    tracker._col = _TaskCollection()

    try:
        future = tracker.submit(lambda: None, task_label='test task', temp_dir=str(temp_dir))
        future.result(timeout=5)
    finally:
        tracker.shutdown(wait=True)

    assert not temp_dir.exists()
