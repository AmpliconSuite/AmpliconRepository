#!/usr/bin/env python3
"""Per-worker memory sampler for the gunicorn web tier.

Why this exists
---------------
Both servers restart themselves daily (dev 00:15 UTC, prod 07:12 UTC) because
"the web tier leaks memory" -- a claim that has never been attached to a
measurement. The restart is what makes it unfalsifiable: the process image
never gets old enough to show a trend, so there is nothing to plot. This
records the trend so the claim can be settled either way.

What it records, and why each column is here
--------------------------------------------
*Per process*, not summed. A summed RSS cannot tell a leak from a worker
recycle: gunicorn recycles a worker after ``max_requests`` (2000 + jitter), and
the recycle drops that worker's RSS back to the fork baseline. In a sum that
looks like memory being released. Per process, with a stable identity, a
recycle is visible as one worker disappearing and a new one appearing.

*PSS and USS*, not just RSS. ``preload_app = True`` means the nine workers are
forks sharing most of the interpreter and Django image copy-on-write, and every
one of them counts those shared pages in full in its own RSS. Measured on dev
2026-09-02: nine workers at ~255 MB RSS each summed to 2.31 GB while the
container cgroup -- the number the 8 GiB cap is enforced against -- read
1.65 GiB. Summed RSS overstated the truth by 40%.

  - ``rss``    what the process maps, shared pages counted in full
  - ``pss``    shared pages divided by the number of sharers; PSS sums to
               something close to the real total
  - ``uss``    private pages only; this is what growing *in this worker* looks
               like, and what would actually be freed by recycling it
  - ``cgroup`` the container total, which is what the 8 GiB cap acts on

A leak in request handling shows up as USS rising in the workers while the
master stays flat. Rising RSS with flat USS is shared-page accounting, not a
leak.

*Process identity* is ``(pid, started_utc)``. A pid alone is not an identity:
the container restarts into a fresh pid namespace, so tomorrow's pid 66 is
unrelated to today's. ``started_utc`` also gives the age, which is what makes
"grew 40 MB" mean something.

No third-party imports, on purpose: it must run inside the app container, on
either server's host, and on a laptop, without a pip install standing between
someone and a measurement.

Usage
-----
One sample and exit -- this is the cron form, and it is restart-proof because
each sample is its own process::

    */1 * * * * docker exec amplicon-dev /opt/venv/bin/python /srv/memory_probe.py \
        --once --output /srv/logs/memory_probe.csv

Watch it live, e.g. while driving a suspected leak by hand::

    python3 memory_probe.py --interval 10 --duration 600 --output /tmp/probe.csv

Read back what was collected::

    python3 memory_probe.py --report /srv/logs/memory_probe.csv

Writing to ``/srv/logs`` matters: that directory is bind-mounted from the host
checkout, so the series outlives the container restart it exists to measure.
About 1.5 MB per day at one sample a minute with nine workers. Keep it out of
logrotate -- rotation by copytruncate would silently cut the history in half.
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

# Which processes to sample. proc_name in gunicorn_config.py is inert
# (setproctitle is not installed), so the processes appear under the default
# name and have to be matched on their command line, same as py-spy does.
DEFAULT_PATTERN = r"gunicorn|manage\.py|celery"

CLK_TCK = os.sysconf("SC_CLK_TCK")

# A freshly forked worker is a copy-on-write image of the master; it touches
# pages as it serves its first requests, so its USS climbs steeply for a while
# before flattening. That ramp is not a leak, and extrapolating it is how this
# tool reported "the 8.00 GiB cap is 1.9 hours away" on 2026-09-03 from a prod
# worker six minutes old. Below this age a worker's growth rate is warm-up.
WARMUP_S = int(os.getenv("MEMORY_PROBE_WARMUP_S", "3600"))

FIELDS = [
    "ts_utc",           # sample time, ISO 8601 UTC
    "sample_id",        # epoch seconds; rows sharing it were taken together
    "pid",
    "ppid",
    "role",             # master | worker | other
    "started_utc",      # process start; with pid, a stable identity
    "age_s",
    "rss_kb",
    "pss_kb",
    "uss_kb",           # Private_Clean + Private_Dirty
    "shared_kb",
    "swap_kb",
    "threads",
    "fds",
    "cpu_s",            # utime + stime, to separate a busy worker from an idle one
    "cgroup_current_kb",
    "cgroup_max_kb",
    "cgroup_anon_kb",   # the part that actually approaches the cap
    "cgroup_file_kb",   # page cache: counted in current, but reclaimable
    "cmd",
]


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except (OSError, IOError):
        return None


def boot_epoch():
    """Epoch seconds at boot, for turning /proc/<pid>/stat starttime into a date.

    Inside a container /proc/uptime is still the host's, so this agrees with
    the host's clock -- which is what we want, since the whole point is to
    compare samples across a container restart.
    """
    uptime = _read("/proc/uptime")
    if not uptime:
        return None
    return time.time() - float(uptime.split()[0])


def cgroup_memory():
    """(current_kb, max_kb, anon_kb, file_kb) for this cgroup. v2 first, then v1.

    ``current`` is the number the container's --memory cap is enforced against
    and the one `docker stats` shows -- but it includes page cache, which is
    reclaimable and harmless. Reading it alone invites a false alarm: a
    container sitting at 6 GiB of mostly file cache is fine, while one at 6 GiB
    of anonymous memory is nearly out of room. ``anon`` is the figure that
    actually approaches the cap. Measured on dev 2026-09-03 mid-analysis:
    current 4.00 GB, of which anon 3.95 GB and file 23 MB -- so on this workload
    they nearly coincide, but that is a fact about the workload, not a rule.
    """
    v2_cur, v2_max = "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"
    v1_cur = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    v1_max = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

    cur = _read(v2_cur) or _read(v1_cur)
    mx = _read(v2_max) or _read(v1_max)
    stat = _read("/sys/fs/cgroup/memory.stat") or _read(
        "/sys/fs/cgroup/memory/memory.stat") or ""
    parts = {}
    for line in stat.splitlines():
        bits = line.split()
        if len(bits) == 2 and bits[1].isdigit():
            parts[bits[0]] = int(bits[1])
    # cgroup v2 spells these "anon"/"file"; v1 uses "rss"/"cache".
    anon = parts.get("anon", parts.get("rss"))
    fil = parts.get("file", parts.get("cache"))

    def kb(raw):
        if not raw:
            return ""
        raw = raw.strip()
        if raw == "max":
            return ""
        try:
            return int(raw) // 1024
        except ValueError:
            return ""

    return (kb(cur), kb(mx),
            "" if anon is None else anon // 1024,
            "" if fil is None else fil // 1024)


def smaps_rollup(pid):
    """Rss/Pss/Uss/Shared/Swap in kB from smaps_rollup.

    smaps_rollup is one pre-summed read per process; walking /proc/<pid>/smaps
    instead would be thousands of lines each and is not worth it at this
    cadence. Falls back to statm's RSS when the kernel is too old to have it,
    in which case pss/uss come back empty rather than wrong.
    """
    out = {"rss_kb": "", "pss_kb": "", "uss_kb": "", "shared_kb": "", "swap_kb": ""}
    raw = _read("/proc/%d/smaps_rollup" % pid)
    if raw:
        vals = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, rest = line.partition(":")
                num = rest.strip().split()
                if num and num[0].isdigit():
                    vals[key] = int(num[0])
        private = vals.get("Private_Clean", 0) + vals.get("Private_Dirty", 0)
        shared = vals.get("Shared_Clean", 0) + vals.get("Shared_Dirty", 0)
        out["rss_kb"] = vals.get("Rss", "")
        out["pss_kb"] = vals.get("Pss", "")
        out["uss_kb"] = private
        out["shared_kb"] = shared
        out["swap_kb"] = vals.get("Swap", 0)
        return out

    statm = _read("/proc/%d/statm" % pid)
    if statm:
        out["rss_kb"] = int(statm.split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    return out


def proc_stat(pid):
    """ppid, starttime jiffies, threads, cpu seconds from /proc/<pid>/stat.

    The comm field can contain spaces and parentheses, so the tail is parsed
    from the last ')' rather than by splitting the whole line.
    """
    raw = _read("/proc/%d/stat" % pid)
    if not raw:
        return None
    try:
        tail = raw[raw.rindex(")") + 2:].split()
        return {
            "ppid": int(tail[1]),
            "utime": int(tail[11]) / CLK_TCK,
            "stime": int(tail[12]) / CLK_TCK,
            "threads": int(tail[17]),
            "starttime": int(tail[19]) / CLK_TCK,
        }
    except (ValueError, IndexError):
        return None


def cmdline(pid):
    raw = _read("/proc/%d/cmdline" % pid)
    if not raw:
        return ""
    return " ".join(raw.split("\0")).strip()


def num_fds(pid):
    """Descriptor count -- a leak of open GridFS/tar handles shows here first."""
    try:
        return len(os.listdir("/proc/%d/fd" % pid))
    except OSError:
        return ""


def collect(pattern=DEFAULT_PATTERN):
    """One sample: a list of row dicts, one per matched process."""
    now = time.time()
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    base = boot_epoch()
    cg_cur, cg_max, cg_anon, cg_file = cgroup_memory()
    rx = re.compile(pattern)

    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmd = cmdline(pid)
        if not cmd or not rx.search(cmd):
            continue
        # Skip the probe itself and any shell wrapping it, which would
        # otherwise match "memory_probe.py" against a pattern like manage.py.
        if "memory_probe.py" in cmd:
            continue
        st = proc_stat(pid)
        if not st:
            continue
        found[pid] = (cmd, st)

    # The master is the matched process that is the parent of other matched
    # processes; workers are its children. Anything else that matched the
    # pattern is recorded as "other" rather than silently dropped -- an
    # aggregation subprocess holding memory is exactly what we are looking for.
    parents = {st["ppid"] for _, st in found.values()}
    rows = []
    for pid, (cmd, st) in sorted(found.items()):
        if pid in parents and st["ppid"] not in found:
            role = "master"
        elif st["ppid"] in found:
            role = "worker"
        else:
            role = "other"

        started = base + st["starttime"] if base else None
        row = {
            "ts_utc": ts,
            "sample_id": int(now),
            "pid": pid,
            "ppid": st["ppid"],
            "role": role,
            "started_utc": (
                datetime.fromtimestamp(started, timezone.utc)
                .replace(microsecond=0).isoformat() if started else ""),
            "age_s": int(now - started) if started else "",
            "threads": st["threads"],
            "fds": num_fds(pid),
            "cpu_s": round(st["utime"] + st["stime"], 1),
            "cgroup_current_kb": cg_cur,
            "cgroup_max_kb": cg_max,
            "cgroup_anon_kb": cg_anon,
            "cgroup_file_kb": cg_file,
            "cmd": cmd[:120],
        }
        row.update(smaps_rollup(pid))
        rows.append(row)
    return rows


def write_rows(rows, output):
    """Append rows, writing a header only when creating the file.

    Opened and closed per sample and flushed on close, so a sample is never
    half-written and the series survives the process being killed at any
    moment -- including by the nightly restart this is here to measure.
    """
    if not rows:
        return
    if not output:
        w = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
        return
    exists = os.path.exists(output) and os.path.getsize(output) > 0

    # If the file was written by a version with a different column set, appending
    # to it would silently produce a CSV whose rows do not match its own header.
    # Rotate the old series aside instead of corrupting or discarding it: the
    # history stays readable with the reader of its own era, and a long soak is
    # not lost because someone added a column halfway through.
    if exists:
        with open(output, newline="") as fh:
            header = fh.readline().strip().split(",")
        if header != FIELDS:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rotated = "%s.%s" % (output, stamp)
            os.rename(output, rotated)
            sys.stderr.write(
                "memory_probe: column set changed; previous series kept at %s\n"
                % rotated)
            exists = False

    with open(output, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def human(kb):
    if kb == "" or kb is None:
        return "-"
    kb = float(kb)
    if kb >= 1024 * 1024:
        return "%.2f GiB" % (kb / 1024 / 1024)
    return "%.0f MiB" % (kb / 1024)


def report(path):
    """Summarise a collected series: growth per worker, recycles, restarts.

    Reports USS growth per worker per hour, because that is the figure a
    decision about the nightly restart actually turns on: at N MiB/hour per
    worker, the 8 GiB cap is either days away or months away, and that
    difference is the whole argument.
    """
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("no samples in %s" % path)
        return 1

    def i(row, key):
        try:
            return int(float(row[key]))
        except (ValueError, KeyError, TypeError):
            return None

    print("=" * 78)
    print("memory_probe report: %s" % path)
    print("%d rows, %s .. %s" % (len(rows), rows[0]["ts_utc"], rows[-1]["ts_utc"]))

    # Container generations: a new master start means the container restarted.
    gens = []
    for row in rows:
        if row["role"] != "master":
            continue
        if not gens or gens[-1][0] != row["started_utc"]:
            gens.append([row["started_utc"], row["ts_utc"], row["ts_utc"]])
        else:
            gens[-1][2] = row["ts_utc"]
    if gens:
        print("\nContainer generations (a new master = a restart)")
        for start, first, last in gens:
            print("  started %s   observed %s .. %s" % (start, first, last))

    # Per process, keyed by identity, first and last sample.
    procs = {}
    for row in rows:
        key = (row["pid"], row["started_utc"])
        procs.setdefault(key, {"first": row, "last": row, "mature": None, "n": 0})
        procs[key]["last"] = row
        procs[key]["n"] += 1
        # The earliest sample at which this process was past fork warm-up.
        # Rates are measured from here, so a worker watched from birth still
        # yields a usable rate once it has been alive long enough.
        if procs[key]["mature"] is None:
            try:
                if int(float(row["age_s"])) >= WARMUP_S:
                    procs[key]["mature"] = row
            except (ValueError, KeyError, TypeError):
                pass

    print("\nPer process (USS is the leak signal; RSS includes shared pages)")
    header = "%-8s %-7s %-9s %10s %10s %10s %10s %8s"
    print(header % ("pid", "role", "age", "uss first", "uss last",
                    "uss delta", "per hour", "samples"))
    print("-" * 78)

    worst = None
    master_rate = None
    skipped_young = 0
    for (pid, _), rec in sorted(procs.items(), key=lambda kv: int(kv[0][0])):
        first, last = rec["first"], rec["last"]
        u0, u1 = i(first, "uss_kb"), i(last, "uss_kb")
        age = i(last, "age_s")
        if u0 is None or u1 is None:
            continue
        hours = (i(last, "sample_id") - i(first, "sample_id")) / 3600.0
        rate = (u1 - u0) / hours if hours > 0.05 else None
        print(header % (
            pid, last["role"],
            "%.1fh" % (age / 3600.0) if age is not None else "-",
            human(u0), human(u1), human(u1 - u0),
            (human(rate) if rate is not None else "-"), rec["n"]))
        if last["role"] == "master" and rate is not None:
            master_rate = (pid, rate, hours)
        elif last["role"] == "worker":
            # Measure the worker from the first sample past warm-up, not from
            # its birth, and only if an hour of it was seen. See WARMUP_S.
            mature = rec["mature"]
            m_hours = ((i(last, "sample_id") - i(mature, "sample_id")) / 3600.0
                       if mature is not None else 0.0)
            m0 = i(mature, "uss_kb") if mature is not None else None
            if m0 is not None and m_hours >= 1.0:
                m_rate = (u1 - m0) / m_hours
                if worst is None or m_rate > worst[1]:
                    worst = (pid, m_rate)
            else:
                skipped_young += 1

    cg = [i(r, "cgroup_current_kb") for r in rows if i(r, "cgroup_current_kb")]
    cap = next((i(r, "cgroup_max_kb") for r in reversed(rows)
                if i(r, "cgroup_max_kb")), None)
    anon = [i(r, "cgroup_anon_kb") for r in rows if i(r, "cgroup_anon_kb")]
    # A CSV written by a probe older than the anon/file split has no such
    # column. Say which line to read rather than naming one that is not there.
    container_line = ("anonymous-memory" if anon else "cgroup-total")
    if anon:
        print("\nContainer anonymous memory (the part that approaches the cap): "
              "first %s, last %s, peak %s" % (human(anon[0]), human(anon[-1]),
                                              human(max(anon))))
    if cg:
        print("Container cgroup total (includes reclaimable page cache): "
              "first %s, last %s, peak %s%s" % (
            human(cg[0]), human(cg[-1]), human(max(cg)),
            ", cap %s" % human(cap) if cap else ""))

    # How long the series actually spans. Everything below is a rate, and a
    # rate off a short window is mostly noise: a worker that happened to serve
    # one request during a ten-minute window extrapolates to something absurd
    # per hour. Projections are withheld rather than qualified, because a
    # printed number gets quoted and a caveat does not.
    span_h = (i(rows[-1], "sample_id") - i(rows[0], "sample_id")) / 3600.0
    print("\nObserved window: %.1f hours" % span_h)

    if span_h < 1.0:
        print("\nToo short to state a growth rate. Let it run for at least an "
              "hour;\nfor the restart question, a full day between restarts is "
              "the measurement\nthat matters.")
        return 0

    # The master is the process the restart uniquely replaces: max_requests
    # recycles workers but never the master, so its drift is the figure the
    # "can the nightly restart go?" question actually turns on.
    if master_rate:
        print("Master (the only process a restart uniquely replaces): "
              "%s/hour over %.1fh." % (human(master_rate[1]), master_rate[2]))

    if worst and worst[1] > 0:
        print("\nFastest-growing worker past warm-up: pid %s at %s/hour of "
              "private memory." % (worst[0], human(worst[1])))
        if cap and cg:
            # Headroom against anonymous memory where we have it: page cache
            # sits in the cgroup total but is reclaimed under pressure rather
            # than pushing the container into the OOM killer.
            headroom = cap - (anon[-1] if anon else cg[-1])
            # Nine workers all growing at that rate is the pessimistic case;
            # it is the one that decides whether a restart is load-bearing.
            hours = headroom / (worst[1] * 9) if worst[1] else None
            if hours and hours > 0:
                print("If all 9 workers grew at that rate, the %s cap is "
                      "%.1f hours (%.1f days) away." % (human(cap), hours, hours / 24))
    elif skipped_young:
        # Not a gap in the data. It is what worker recycling means: no worker
        # survives long enough past warm-up for a per-worker rate to exist, so
        # the container-level drift above is the signal, not this line.
        print("\nNo worker lived long enough past warm-up (%dh) to carry a "
              "growth rate;\n%d were too young or too briefly seen. That is "
              "max_requests recycling\nworking as intended -- read the "
              "container %s drift above instead."
              % (WARMUP_S // 3600, skipped_young, container_line))
    else:
        print("\nNo worker shows positive USS growth over the observed window.")
    print("\nWindow length is the caveat on all of the above -- a leak that "
          "needs\na day to become visible cannot be ruled out by an hour of "
          "samples.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Sample per-worker memory of the gunicorn web tier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----")[-1])
    p.add_argument("--once", action="store_true",
                   help="take one sample and exit (the cron form)")
    p.add_argument("--interval", type=float, default=60,
                   help="seconds between samples in loop mode (default 60)")
    p.add_argument("--duration", type=float,
                   help="stop after this many seconds (default: run until killed)")
    p.add_argument("--output", help="CSV to append to (default stdout)")
    p.add_argument("--pattern", default=DEFAULT_PATTERN,
                   help="regex matched against process command lines")
    p.add_argument("--report", metavar="CSV",
                   help="summarise a collected series instead of sampling")
    args = p.parse_args()

    if args.report:
        return report(args.report)

    if args.once:
        write_rows(collect(args.pattern), args.output)
        return 0

    deadline = time.time() + args.duration if args.duration else None
    try:
        while True:
            write_rows(collect(args.pattern), args.output)
            if deadline and time.time() >= deadline:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
