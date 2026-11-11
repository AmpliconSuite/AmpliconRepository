"""Tests for scripts/memory_auditing/memory_probe.py.

The probe reads /proc and the cgroup files, so these tests run it against real processes
started for the purpose rather than against fakes: a parser that works on a hand-written
`/proc/<pid>/stat` line and fails on the kernel's is the failure that matters. It was
verified by hand on dev on 2026-11-10 (ten processes, per-process rss/pss/uss, cgroup
anon/file/cap), and had no test until it moved into scripts/memory_auditing/.

Linux only: the probe is a tool for the Linux hosts and says nothing elsewhere.
"""
import csv
import os
import subprocess
import sys
import time
import uuid

import pytest

import memory_probe

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith('linux') or not os.path.isdir('/proc/self'),
    reason='memory_probe reads /proc')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(REPO_ROOT, 'scripts', 'memory_auditing', 'memory_probe.py')

SLEEPER = 'import time; time.sleep(60)'
# A parent that starts one child, both carrying the marker on their command lines.
PARENT = ('import subprocess, sys, time\n'
          'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", sys.argv[1]])\n'
          'time.sleep(60)')


@pytest.fixture
def marker():
    return 'probe-test-%s' % uuid.uuid4().hex


@pytest.fixture
def spawn():
    started = []

    def start(*args):
        process = subprocess.Popen([sys.executable, *args])
        started.append(process)
        return process

    yield start
    for process in started:
        process.kill()
        process.wait()


def _wait_for(marker, count, deadline=15):
    """Samples until *count* processes carrying *marker* show up."""
    end = time.time() + deadline
    rows = []
    while time.time() < end:
        rows = memory_probe.collect(marker)
        if len(rows) >= count:
            return rows
        time.sleep(0.1)
    raise AssertionError('wanted %d processes matching %s, saw %d' % (count, marker, len(rows)))


def test_smaps_rollup_reports_this_process_in_kilobytes():
    out = memory_probe.smaps_rollup(os.getpid())

    assert out['rss_kb'] > 0
    # Private pages are a subset of resident pages; if uss exceeded rss the fields were misread.
    assert 0 < out['uss_kb'] <= out['rss_kb']
    assert out['pss_kb'] <= out['rss_kb']


def test_proc_stat_survives_a_command_name_with_spaces_and_parentheses(monkeypatch):
    """comm is free text. Splitting the line on spaces misreads every field after it."""
    tail = ['S', '4321', '0', '0', '0', '0', '0', '0', '0', '0', '0',
            '250', '50', '0', '0', '20', '0', '7', '0', '12345']
    line = '99 (a (weird) name) ' + ' '.join(tail) + ' 0 0\n'
    monkeypatch.setattr(memory_probe, '_read', lambda path: line)

    parsed = memory_probe.proc_stat(99)

    assert parsed['ppid'] == 4321
    assert parsed['threads'] == 7
    assert parsed['utime'] == 250 / memory_probe.CLK_TCK
    assert parsed['stime'] == 50 / memory_probe.CLK_TCK
    assert parsed['starttime'] == 12345 / memory_probe.CLK_TCK


def test_proc_stat_of_a_process_that_has_gone_is_none():
    assert memory_probe.proc_stat(2 ** 22 + 12345) is None


def test_collect_finds_a_process_by_command_line(marker, spawn):
    child = spawn('-c', SLEEPER, marker)

    rows = _wait_for(marker, 1)

    assert [row['pid'] for row in rows] == [child.pid]
    row = rows[0]
    assert row['ppid'] == os.getpid()
    assert row['role'] == 'other'          # it matched, but is neither master nor worker
    assert row['rss_kb'] > 0 and row['uss_kb'] > 0
    # The command line is recorded, truncated to 120 characters to keep the CSV narrow.
    assert 0 < len(row['cmd']) <= 120
    assert set(memory_probe.FIELDS) - set(row) == set(), 'a column has no value in the row'


def test_collect_names_the_master_and_its_worker(marker, spawn):
    parent = spawn('-c', PARENT, marker)

    rows = {row['role']: row for row in _wait_for(marker, 2)}

    assert rows['master']['pid'] == parent.pid
    assert rows['worker']['ppid'] == parent.pid


def test_collect_skips_the_probe_itself(marker, spawn):
    """A probe whose pattern matched its own command line would sample itself forever."""
    spawn('-c', SLEEPER, 'memory_probe.py', marker)
    visible = spawn('-c', SLEEPER, marker)

    _wait_for(marker, 1)
    time.sleep(0.5)       # let the probe-named process be fully started before sampling again

    assert [row['pid'] for row in memory_probe.collect(marker)] == [visible.pid]


def _row(**values):
    row = {field: '' for field in memory_probe.FIELDS}
    row.update(values)
    return row


def test_write_rows_writes_the_header_once(tmp_path):
    output = str(tmp_path / 'series.csv')

    memory_probe.write_rows([_row(pid=1)], output)
    memory_probe.write_rows([_row(pid=2)], output)

    with open(output, newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert [row['pid'] for row in rows] == ['1', '2']
    with open(output) as handle:
        assert sum(line.startswith('ts_utc,') for line in handle) == 1


def test_a_changed_column_set_keeps_the_old_series_instead_of_corrupting_it(tmp_path):
    """A soak that runs for days must survive someone adding a column halfway through."""
    output = str(tmp_path / 'series.csv')
    with open(output, 'w') as handle:
        handle.write('ts_utc,pid,an_old_column\n2026-09-01T00:00:00+00:00,7,x\n')

    memory_probe.write_rows([_row(pid=9)], output)

    with open(output, newline='') as handle:
        assert list(csv.DictReader(handle))[0]['pid'] == '9'
    kept = [name for name in os.listdir(tmp_path) if name.startswith('series.csv.')]
    assert len(kept) == 1
    with open(tmp_path / kept[0]) as handle:
        assert '2026-09-01T00:00:00+00:00,7,x' in handle.read()


def test_the_cron_form_appends_a_sample_and_exits(marker, spawn, tmp_path):
    """`--once --output FILE`: the form the hosts' crontabs used, one process per sample."""
    child = spawn('-c', SLEEPER, marker)
    _wait_for(marker, 1)
    output = str(tmp_path / 'series.csv')

    for _ in range(2):
        done = subprocess.run(
            [sys.executable, PROBE, '--once', '--output', output, '--pattern', marker],
            capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr

    with open(output, newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert {row['pid'] for row in rows} == {str(child.pid)}


def test_the_report_withholds_a_rate_from_a_short_window(tmp_path, capsys):
    output = str(tmp_path / 'series.csv')
    memory_probe.write_rows(
        [_row(sample_id=1000 + 60 * n, ts_utc='t%d' % n, pid=5, role='worker',
              started_utc='s', age_s=7200, uss_kb=100 * 1024) for n in range(3)], output)

    assert memory_probe.report(output) == 0

    assert 'Too short to state a growth rate' in capsys.readouterr().out


def test_the_report_states_growth_per_hour_past_warm_up(tmp_path, capsys):
    """20 MiB an hour for three hours, on a worker already past warm-up."""
    output = str(tmp_path / 'series.csv')
    memory_probe.write_rows(
        [_row(sample_id=10_000 + 3600 * n, ts_utc='t%d' % n, pid=5, role='worker',
              started_utc='s', age_s=memory_probe.WARMUP_S + 3600 * n,
              uss_kb=(100 + 20 * n) * 1024) for n in range(4)], output)

    assert memory_probe.report(output) == 0

    out = capsys.readouterr().out
    assert 'Fastest-growing worker past warm-up: pid 5 at 20 MiB/hour' in out


def test_the_report_of_an_empty_series_says_so(tmp_path, capsys):
    output = str(tmp_path / 'series.csv')
    with open(output, 'w') as handle:
        handle.write(','.join(memory_probe.FIELDS) + '\n')

    assert memory_probe.report(output) == 1
    assert 'no samples' in capsys.readouterr().out
