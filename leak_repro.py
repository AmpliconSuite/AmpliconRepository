#!/usr/bin/env python3
"""Run one suspected-leaky operation repeatedly and measure what it leaves behind.

Why this exists
---------------
The nightly restart on both servers is justified by "the web tier leaks memory,
the leak was never found". Two operations have always been the suspects:
re-aggregating a project, and a large co-amplification analysis. This runs
either one in a loop, in process, and measures whether memory that is not
handed back accumulates per iteration.

``memory_probe.py`` answers "is memory growing on the server". This answers
"does *this* operation grow it", which is the question you cannot settle by
watching a live server, because on a live server everything happens at once.

Reading the output
------------------
Judge the **steady-state** per-iteration delta, not the total. Every first
iteration grows: imports resolve lazily, pymongo opens its pool, pandas and
numpy allocate arenas, Django fills its caches. A leak is growth that is still
happening at iteration 10 at the same rate it happened at iteration 3.

  - flat after warmup    -> not a leak; the first iteration's cost is the
                           process paying for its own startup
  - rises and plateaus   -> a bounded cache, or allocator arenas that are held
                           but reused; watch where it plateaus
  - rises linearly       -> a leak, and ``--tracemalloc`` will name the lines

USS is the number to read: private pages, which is what this process is
actually holding. Note the floor -- glibc's allocator frees to its own arenas
and does not always return pages to the kernel, so a genuine Python-level
release can still leave USS flat. That is a false *positive* for leaking, which
is why ``--tracemalloc`` is the tiebreak: it tracks Python allocations rather
than pages, so a flat tracemalloc total with rising USS means fragmentation,
not a leak.

Usage
-----
::

    # a big co-amplification analysis, ten times over, with attribution
    python leak_repro.py --scenario graph --largest 3 --iterations 10 --tracemalloc

    # re-aggregate the same tarball repeatedly (the other prime suspect)
    python leak_repro.py --scenario aggregate --iterations 5 \
        --tarball ~/Dropbox/site_results/some_project.tar.gz

    # a read-path control: whatever this shows is the harness's own baseline
    python leak_repro.py --scenario project_page --largest 5 --iterations 10

Runs against whatever ``caper/config.sh`` points at, so check that first --
locally that is the dockerised mongo, on a server it is the shared cluster.
Every scenario here is read-only except ``aggregate``, which only writes to a
temporary work directory: it calls the aggregator directly and never inserts a
project.
"""

import argparse
import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter

# The app's static-file sync fires from AppConfig.ready() when S3_STATIC_FILES
# is TRUE, which would push this machine's static directory into the bucket
# that every deployment shares. A measurement harness must not do that.
os.environ["S3_STATIC_FILES"] = "FALSE"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "caper"))

# Page-level numbers come from the probe, so both tools report the same figure
# the same way rather than disagreeing by a definition.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_probe import smaps_rollup  # noqa: E402


def setup_django():
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "caper.settings")
    django.setup()


def malloc_trim():
    """Ask glibc to return free arena pages to the kernel; True if it did work.

    Python freeing an object does not mean the process gives the page back:
    glibc keeps freed memory in its own arenas, so a worker that once built a
    large graph keeps that footprint for the rest of its life even though
    nothing is leaked in the Python sense. malloc_trim() is the syscall-level
    "actually give it back", and whether it recovers the memory is the
    difference between a leak (it will not) and fragmentation (it will).

    Not portable: glibc only. Returns None where there is nothing to call.
    """
    try:
        import ctypes
        lib = ctypes.CDLL("libc.so.6")
        return bool(lib.malloc_trim(0))
    except (OSError, AttributeError):
        return None


def mem():
    """Current (rss_kb, uss_kb) for this process, after collecting garbage.

    Two collect() calls, not one: the first can resurrect objects through
    finalizers and leave newly-unreachable cycles behind it, which the second
    then reaps. Anything still held after both is held on purpose or leaked.
    """
    gc.collect()
    gc.collect()
    snap = smaps_rollup(os.getpid())
    return snap.get("rss_kb") or 0, snap.get("uss_kb") or 0


def pick_projects(count):
    """The `count` projects with the most samples: the heaviest realistic input."""
    from caper.utils import collection_handle
    docs = collection_handle.find(
        {"delete": False}, {"_id": 1, "project_name": 1, "runs": 1})
    sized = []
    for doc in docs:
        sized.append((len(doc.get("runs") or {}), str(doc["_id"]),
                      doc.get("project_name", "?")))
    sized.sort(reverse=True)
    chosen = sized[:count]
    for n, pid, name in chosen:
        print("  using %-40s %5d samples  %s" % (name[:40], n, pid))
    return [pid for _, pid, _ in chosen]


# --- scenarios ------------------------------------------------------------
# Each returns a short string describing what it did, for the per-iteration
# line. Each must be callable repeatedly with identical arguments: this
# measures what repetition leaves behind, so an operation whose second call
# does less work than its first would read as a leak that stopped.

def scenario_project_page(project_ids, _args):
    """Control: the ordinary project read path, no analysis."""
    from caper.utils import get_one_project
    total = 0
    for pid in project_ids:
        project = get_one_project(pid)
        total += len(project.get("runs") or {})
    return "read %d projects, %d samples" % (len(project_ids), total)


def scenario_concat(project_ids, _args):
    """The DataFrame build behind the visualizer, without the graph."""
    from caper.views import concat_projects
    df, _info = concat_projects(project_ids)
    rows = len(df)
    del df
    return "concatenated %d rows" % rows


def scenario_graph(project_ids, _args):
    """concat + Graph construction: the expensive half of a co-amp analysis.

    This is the part that runs in the worker process. Neo4j holds the result,
    but the graph is *built* here, so a leak in the build shows up in the web
    tier's memory and not in Neo4j's.
    """
    from caper.views import concat_projects
    from caper.coamp_graph import Graph
    df, _info = concat_projects(project_ids)
    graph = Graph(dataset=df)
    n, e = len(graph.nodes), len(graph.edges)
    del graph, df
    return "graph with %d nodes, %d edges" % (n, e)


def scenario_coamp(project_ids, _args):
    """The full visualizer request: build, load into Neo4j, then query it back.

    Needs a reachable Neo4j. force_reload=True on every iteration, because the
    cache exists precisely to skip the work we are trying to measure.
    """
    from caper.views import concat_projects
    from caper.neo4j_utils import load_graph, fetch_subgraph, generate_cache_key
    df, _info = concat_projects(project_ids)
    key = generate_cache_key(project_ids)
    load_graph(df, project_ids=project_ids, force_reload=True)
    nodes, edges = fetch_subgraph("MYC", None, None, False, False, key)
    del df
    return "loaded and fetched %d nodes, %d edges" % (len(nodes), len(edges))


def scenario_aggregate(_project_ids, args):
    """Re-aggregation: the same Aggregator call the edit path makes.

    Mirrors views._process_and_aggregate_files -- same constructor, same
    arguments -- but stops there. Nothing is written to the database and the
    work directory is removed after each iteration, so what is left in memory
    afterwards is the aggregator's residue and nothing else.
    """
    from AmpliconSuiteAggregator import Aggregator
    work = tempfile.mkdtemp(prefix="leakrepro_agg_")
    try:
        agg = Aggregator(
            input_paths=[os.path.abspath(args.tarball)],
            project_name="leak_repro",
            name_map_file=None,
            work_dir=work,
        )
        done = getattr(agg, "completed", None)
        del agg
        return "aggregated %s (completed=%s)" % (
            os.path.basename(args.tarball), done)
    finally:
        shutil.rmtree(work, ignore_errors=True)


SCENARIOS = {
    "project_page": scenario_project_page,
    "concat": scenario_concat,
    "graph": scenario_graph,
    "coamp": scenario_coamp,
    "aggregate": scenario_aggregate,
}


def type_census():
    """Live object counts by type name, for diffing across iterations."""
    counts = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return counts


def verdict(deltas, warmup):
    """Classify the shape of per-iteration growth after the warmup iterations.

    Compares the first and second halves of the post-warmup deltas. A leak
    keeps growing at roughly the rate it started at; a cache filling up decays
    towards zero. The threshold is deliberately generous -- this reports a
    shape and a number for a human to judge, it does not adjudicate.
    """
    tail = deltas[warmup:]
    if len(tail) < 4:
        return ("inconclusive",
                "fewer than 4 post-warmup iterations; run more with --iterations")
    half = len(tail) // 2
    early = sum(tail[:half]) / half
    late = sum(tail[half:]) / (len(tail) - half)

    if late <= 64:  # under 64 KiB/iteration is noise at page granularity
        return ("flat", "steady-state growth is %.0f KiB/iteration -- nothing "
                        "accumulating" % late)
    if early > 0 and late / early < 0.25:
        return ("plateau", "growth decayed from %.0f to %.0f KiB/iteration -- "
                           "warmup or a bounded cache, not a leak" % (early, late))
    return ("linear", "still growing at %.0f KiB/iteration after %d iterations "
                      "(started at %.0f) -- leak-shaped"
                      % (late, warmup + half, early))


def main():
    p = argparse.ArgumentParser(
        description=__doc__.split("Why this exists")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----")[-1])
    p.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    p.add_argument("--iterations", type=int, default=10)
    p.add_argument("--warmup", type=int, default=2,
                   help="iterations excluded from the verdict (default 2); the "
                        "first call pays for imports, pools and arenas")
    p.add_argument("--projects", nargs="*", default=None,
                   help="project ids to operate on")
    p.add_argument("--largest", type=int, default=3,
                   help="instead of --projects, use the N projects with the "
                        "most samples (default 3)")
    p.add_argument("--tarball", help="input for the aggregate scenario")
    p.add_argument("--tracemalloc", action="store_true",
                   help="attribute growth to source lines (slows each "
                        "iteration, but it is what names the leak)")
    p.add_argument("--trim", action="store_true",
                   help="call malloc_trim() after each iteration and report "
                        "what it recovered -- this is the test that separates "
                        "a leak from allocator retention")
    p.add_argument("--objects", action="store_true",
                   help="also census live objects by type each iteration")
    args = p.parse_args()

    if args.scenario == "aggregate" and not args.tarball:
        p.error("--scenario aggregate needs --tarball")

    # Line-buffered: an iteration of this can take minutes, and a redirected
    # run that shows nothing until it exits is indistinguishable from a hung one.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    print("=" * 78)
    print("leak_repro: scenario=%s iterations=%d" % (args.scenario, args.iterations))
    print("database: %s" % os.environ.get("DB_NAME", "(unset -- source caper/config.sh)"))
    print("=" * 78)

    setup_django()

    if args.scenario == "aggregate":
        project_ids = []
    elif args.projects:
        project_ids = args.projects
    else:
        print("\nSelecting the %d largest projects:" % args.largest)
        project_ids = pick_projects(args.largest)
        if not project_ids:
            print("No projects found in %s" % os.environ.get("DB_NAME"))
            return 1

    run = SCENARIOS[args.scenario]

    if args.tracemalloc:
        tracemalloc.start(25)

    # Baseline after Django is up and the handles are open, so process startup
    # is not counted against the first iteration.
    rss0, uss0 = mem()
    threads0 = threading.active_count()
    print("\nbaseline: RSS %.1f MiB, USS %.1f MiB, %d threads\n"
          % (rss0 / 1024, uss0 / 1024, threads0))

    header = "%-5s %8s %10s %11s %7s %7s  %s"
    print(header % ("iter", "secs", "USS", "delta", "threads", "objs", "detail"))
    print("-" * 78)

    deltas = []
    prev_uss = uss0
    baseline_snapshot = None
    census0 = None

    for i in range(1, args.iterations + 1):
        t0 = time.time()
        try:
            detail = run(project_ids, args)
        except Exception as exc:      # keep the series; a failing iteration
            detail = "FAILED: %s" % exc   # is data, not a reason to stop
        elapsed = time.time() - t0

        rss, uss = mem()
        if args.trim:
            # After the measurement of what the iteration left behind, so the
            # trim column shows what was recoverable rather than hiding it.
            uss_untrimmed = uss
            malloc_trim()
            rss, uss = mem()
            trimmed = uss_untrimmed - uss
        delta = uss - prev_uss
        deltas.append(delta)
        prev_uss = uss

        objs = len(gc.get_objects()) if args.objects else 0
        if args.trim:
            detail = "trimmed %.0f MiB | %s" % (trimmed / 1024, detail)
        print(header % (i, "%.1f" % elapsed, "%.1f MiB" % (uss / 1024),
                        "%+.1f MiB" % (delta / 1024), threading.active_count(),
                        objs or "-", detail[:40]))

        if args.tracemalloc and i == args.warmup:
            baseline_snapshot = tracemalloc.take_snapshot()
            if args.objects:
                census0 = type_census()

    rss, uss = mem()
    print("-" * 78)
    print("total: USS %.1f -> %.1f MiB (%+.1f MiB), RSS %.1f -> %.1f MiB"
          % (uss0 / 1024, uss / 1024, (uss - uss0) / 1024,
             rss0 / 1024, rss / 1024))
    print("threads: %d -> %d%s" % (
        threads0, threading.active_count(),
        "   <-- threads are not being reclaimed"
        if threading.active_count() > threads0 else ""))

    shape, why = verdict(deltas, args.warmup)
    print("\nVERDICT: %s\n  %s" % (shape.upper(), why))

    if args.tracemalloc and baseline_snapshot:
        current = tracemalloc.take_snapshot()
        stats = current.compare_to(baseline_snapshot, "lineno")
        grew = [s for s in stats if s.size_diff > 0][:15]
        print("\nPython allocations held since iteration %d (top %d):"
              % (args.warmup, len(grew)))
        total = sum(s.size_diff for s in stats if s.size_diff > 0)
        print("  tracked growth: %.1f MiB across %d sites"
              % (total / 1024 / 1024, len(stats)))
        for s in grew:
            frame = s.traceback[0]
            path = frame.filename
            for marker in ("/caper/", "/site-packages/"):
                if marker in path:
                    path = path.split(marker)[-1]
                    break
            print("  %+8.2f MiB  %+7d blocks  %s:%d"
                  % (s.size_diff / 1024 / 1024, s.count_diff, path, frame.lineno))
        print("\n  A flat tracked total with rising USS above means allocator\n"
              "  fragmentation, not a leak -- Python released it, glibc kept it.")

        if census0:
            census1 = type_census()
            diffs = sorted(((census1[k] - census0.get(k, 0), k) for k in census1),
                           reverse=True)[:10]
            print("\nLive objects gained by type since iteration %d:" % args.warmup)
            for n, name in diffs:
                if n > 0:
                    print("  %+8d  %s" % (n, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
