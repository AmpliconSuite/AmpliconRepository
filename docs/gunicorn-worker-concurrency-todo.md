# TODO: evaluate gthread workers for the web tier

Deferred deliberately. Raised while diagnosing the repeated production outages
of 2026-07-29..08-06 (six target-unhealthy episodes, five of them genuine
saturation collapses). Not blocking; revisit only after the read-amplification
fix has been in production long enough to re-measure.

## The proposal

Switch `gunicorn_config.py` from 9 sync workers (1 thread each) to `gthread`,
e.g. 5 workers x 4 threads, raising in-flight request concurrency from 9 to ~20
without a proportional increase in memory (threads share the process image;
`preload_app = True` is already set).

## Why it might help

pymongo releases the GIL while blocked on socket I/O. With sync workers, a
worker waiting on DocumentDB does nothing else; with threads, another request
runs in that gap. DocumentDB itself has headroom — it sat at ~24% CPU baseline
and peaked at ~70% during the worst burst.

## Why it might not — and why we deferred

Measured per-request split, from `[PERF]` log lines before the fix:

| phase              | time     | GIL |
|--------------------|----------|-----|
| `get_one_sample`   | 0.5-0.8s | released during socket I/O — threads help |
| plot generation    | 0.03-0.6s| held for pure-Python work — threads contend |

The read-amplification fix removes most of the `get_one_sample` cost, which is
precisely the portion threads would have helped with. Afterwards the workload
is CPU-dominated on an 8-vCPU box that also runs project aggregation in
`_thread_executor` background threads. Adding request threads there could hurt
latency rather than help it.

So the honest position: **the theory argues for threads mainly in the regime we
are about to leave.** Measure before changing anything.

## How to decide

Re-measure once the read-amplification fix is in production:

1. Get the new distribution:
   `grep -a '\[PERF\] Total sample_page' logs/stdout.txt | grep -oE '[0-9.]+s$' | sort -n`
2. If `get_one_sample` is now a small fraction of total page time, threads will
   not help much — cache the generated plots instead.
3. If DB wait still dominates, load-test locally before touching production.
   `performance_test.py` exists in the repo; otherwise drive a local gunicorn
   with `hey`/`locust` at rising concurrency and compare `sync` x9 against
   `gthread` on p50/p95 and throughput.

Caveat for local testing: local MongoDB is far faster than DocumentDB over the
network, so the I/O share will be understated and the local result will
**underestimate** the benefit of threads. Treat it as a lower bound.
