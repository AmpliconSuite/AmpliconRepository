#!/usr/bin/env python3
"""Drive repeated co-amplification analyses at a running site, over HTTP.

`leak_repro.py` runs the analysis in its own process; this makes the real
server do it in a gunicorn worker, which is where the nightly restart's premise
lives. Pair it with `memory_probe.py` sampling that same server, and the two
together give the per-worker curve for a known sequence of real requests.

What this measured on dev, 2026-09-02: three analyses of a 2095-sample project
(260 s, 164 s, 164 s) driven through one worker took it from an idle 257 MiB
RSS to a peak of 1.99 GiB, after which it settled at 609 MiB and stayed there.
The container went 1.78 -> 4.72 -> 2.49 GiB against an 8 GiB cap, with only one
worker analysing at a time. The gunicorn master did not move.

The Neo4j cache is cleared before each iteration on purpose -- the cache exists
to skip exactly the work being measured, so without clearing it every iteration
after the first is a no-op. Pass --no-clear to measure the cached path instead.

Credentials come from AMPREPO_USER / AMPREPO_PASS in the environment; there is
a dev account for this, and deliberately no prod one.

    AMPREPO_USER=... AMPREPO_PASS=... python3 coamp_drive.py \
        --project <id> --iterations 3

Read the result with:

    memory_probe.py --report /srv/logs/memory_probe.csv
"""

import argparse
import os
import sys
import time

import requests

# A bare curl-style UA gets a 403 from Bot Control on page URLs; the block is
# the WAF, not the app.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/136.0.0.0 Safari/537.36")


def login(base, user, password):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    url = base + "/accounts/login/"
    r = s.get(url, timeout=60)
    r.raise_for_status()
    token = s.cookies.get("csrftoken")
    r = s.post(url, data={
        "login": user, "password": password,
        "csrfmiddlewaretoken": token,
    }, headers={"Referer": url}, timeout=60)
    r.raise_for_status()
    who = s.get(base + "/accounts/profile/", timeout=60)
    if user not in who.text:
        print("WARNING: profile page does not mention %s -- login may have failed"
              % user)
    return s


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="https://dev.ampliconrepository.org")
    p.add_argument("--project", required=True, action="append",
                   help="project id to select; repeat for several")
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--gene", default="MYC")
    p.add_argument("--no-clear", action="store_true",
                   help="do not clear the Neo4j cache between iterations "
                        "(measures the cached path instead)")
    args = p.parse_args()

    user = os.environ.get("AMPREPO_USER")
    password = os.environ.get("AMPREPO_PASS")
    if not user or not password:
        sys.exit("set AMPREPO_USER and AMPREPO_PASS")

    base = args.base.rstrip("/")
    s = login(base, user, password)
    print("logged in as %s at %s" % (user, base))

    for i in range(1, args.iterations + 1):
        t0 = time.time()
        if not args.no_clear:
            s.get(base + "/admin-clear-cache/?clear_graphs=true", timeout=600)
        r = s.post(base + "/coamplification-graph/",
                   data={"selected_projects": args.project,
                         "csrfmiddlewaretoken": s.cookies.get("csrftoken")},
                   headers={"Referer": base + "/coamplification-graph/"},
                   timeout=1800)
        vis = s.get(base + "/coamplification-graph/visualizer/", timeout=1800)
        cached = "cached" if "Cache hit" in vis.text else "?"
        sub = s.get(base + "/coamplification-graph/visualizer/%s/" % args.gene,
                    timeout=1800)
        n = 0
        try:
            n = len(sub.json().get("nodes", []))
        except Exception:
            pass
        print("iter %d: %.1fs  post=%s visualizer=%s (%d bytes, %s) "
              "subgraph=%s nodes=%d"
              % (i, time.time() - t0, r.status_code, vis.status_code,
                 len(vis.content), cached, sub.status_code, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
