# API Roadmap (developer)

Internal planning doc for the AmpliconRepository REST API (`/api/v1/`). Not part of the
public documentation. For the user-facing reference, see the docs site's `api.md`.

Code lives in `caper/caper/views_apis.py`; routes in `caper/caper/urls.py`.

## Vision

Expose the website's core capabilities — **site-wide search** and **project/sample
downloads** — to scripts, notebooks, a client library, and AI agents.

Design principles:

- **Browser-like access.** Public data is reachable without auth, as in the UI. A token
  is required only for private projects the caller already has access to.
- **Read-only and safe.** Public API is `GET`-only plus a batch `POST` that only resolves
  IDs to URLs. No mutation; never exposes more than the UI would to the same user.
- **Sane rate limits, not identity gates.** Control load with request-rate limits
  (target ~1 search/s per client), not by blocking non-browser clients.
- **Stable and self-describing.** Stable IDs, documented JSON fields, predictable errors,
  and a machine-readable spec.

## Current capabilities (v1)

| Capability | Endpoint | Status |
| --- | --- | --- |
| List / filter projects | `GET /api/v1/projects/` | ✅ Available |
| Project metadata | `GET /api/v1/projects/<id>/` | ✅ Available |
| Sample metadata | `GET /api/v1/projects/<id>/samples/` | ✅ Available |
| Download a project archive | `GET /api/v1/projects/<id>/download/` | ✅ Available |
| Batch-resolve download URLs | `POST /api/v1/projects/download/` | ✅ Available |
| Personal API token | Profile page → Developer API Token | ✅ Available |
| One sample's rows | `GET /api/v1/projects/<id>/samples/<name>/` | ✅ Available |
| **Search every amplicon** | `GET /api/v1/features/` | ✅ Available |
| Search, one entry per sample | `GET /api/v1/features/samples/` | ✅ Available |
| Filter vocabulary, with counts | `GET /api/v1/features/facets/` | ✅ Available |
| OpenAPI 3 document | `GET /api/v1/openapi.json` | ✅ Available |
| Agent-facing summary | `GET /llms.txt` | ✅ Available |
| API index (spec, llms.txt, every endpoint) | `GET /api/v1/` | ✅ In `main`; not yet deployed |
| Feature copy number, complexity, interval length on search rows | `GET /api/v1/features/` | ✅ In `main`; not yet deployed (index schema 5 — needs a rebuild) |

Status verified against production 2026-09-14: every row above answers, the
OpenAPI document lists all ten public routes, and two tests keep the document
and `llms.txt` from drifting from the parameter allowlist the views enforce
(`tests/test_api_openapi.py`). The index row was added 2026-09-16 after an
agent's first request was the bare prefix and got a 404; the same day a
live check was added for the *examples* in `llms.txt` (a sample name the
file cited did not exist in the corpus) -- run it with
`LLMS_TXT_LIVE_URL=https://ampliconrepository.org pytest tests/test_api_openapi.py -k corpus_holds`.

The four feature numbers were added 2026-09-16 after a chat assistant, asked
to plot EGFR ecDNA copy number, read `llms.txt` and concluded the API does not
expose copy number. It did -- on `/projects/<id>/samples/<name>/`, one call
per sample, under undocumented column names -- but not on the search rows
the file steers everything to. The four (`Feature_maximum_copy_number`,
`Feature_median_copy_number`, `Complexity_score`, `Captured_interval_length`)
are the per-feature keys that are present on every row of prod's live corpus
(39,114 rows; float on 27,040 of the 27,045 real amplicons, measured
2026-09-16), which is the same completeness class as `classification` and
`genes`; the file-path and tool-version keys range from 0.2% to 87.6%
coverage and stay on the sample endpoint. What the API still does not carry
-- per-gene copy number, the reconstruction itself -- lives only in the
project archive, so `llms.txt` and the download endpoint's description now
say which files hold it and how they key to `feature_id`. A second live
check downloads the smallest public archive and verifies those claims:
`-k archive_holds`. Carrying per-gene copy number on search rows would need
the aggregator to lift `gene_cn` out of `gene_list.tsv` into each row, and
would then fill in only as projects are re-aggregated -- a separate change.

## Known issues & status

### 1. Load-balancer blocks non-browser User-Agents — **fixed for `/api/v1/`, 2026-09-04**

Read from the live WebACL, not inferred. `Amplicon_WAF` (attached to both the prod
and dev ALBs) carries `AllowApiV1` at priority 3 — a terminating `Allow` on URIs
starting `/api/v1/`, ordered *after* `KnownBadInputs` (2) and *before* Bot Control
(4), exactly as proposed below. Measured the same day: `curl` default UA, empty UA,
`Wget/1.21.4` and `python-requests/2.32.3` all get `200` from
`/api/v1/projects/`, `404` from a nonexistent project and `401` from `/token/` —
application status codes, so the requests reach Django. `RateLimitApiV1` (priority 1)
backs it: 600 requests / 300 s per IP, `429` + `Retry-After: 60`.

**Still true outside that prefix.** Every other path — `/`, `/robots.txt`,
`/sitemap.xml`, `/healthz`, `/api/background-task-status/` — still 403s to
non-browser UAs. That is a *discovery* problem, not an API problem, and it is
entangled with a finding that changes the plan: see "AI-agent enablement" below.

The original text follows.

### 1a. Original diagnosis (superseded)
The AWS load balancer / WAF returns `403` to requests without a browser-style
`User-Agent` (default `curl`, `requests`, `wget`, empty UA all blocked; a full browser
UA passes). This blocks every default programmatic client and is the main reason the API
"doesn't work."

- **Band-aid shipped:** docs and the profile-page examples now set a browser `-A "$UA"`
  header, and the future Python client will set one under the hood.
- **Real fix (recommended):** a *scoped* WAF exception on the offending rule for
  `/api/v1/*` only (keep SQLi / bad-input rules active), plus a WAF **rate-based rule**.
  UA filtering is trivially spoofed and provides little real protection while blocking
  legitimate clients; rate limits are the correct load control.
- **Why it matters most for agents:** AI agents' HTTP tooling sends library UAs and
  cannot easily impersonate a browser, so this gate must go for smooth agent access.

### 2. `GET /samples/` returned 500 — **fixed; on prod (200, verified 2026-09-14)**
`_sample_to_dict` returned bson `ObjectId` GridFS refs (and occasional `NaN`/`Inf`
floats) that DRF's JSON renderer cannot serialize. Fix (`views_apis.py`): drop
ObjectId-valued fields, sanitize non-finite floats via `_json_safe`. Regression tests in
`tests/test_api_v1.py::TestSampleToDict`.

### 3. Batch `download_url` used `http://` — **fixed; on prod (`https://`, verified 2026-09-14)**
`ProjectBatchDownloadView` built URLs from the WSGI scheme, which is `http` behind the
TLS-terminating ELB. Fix: honor `X-Forwarded-Proto`. Regression test
`test_download_url_honors_x_forwarded_proto`.

### 4. Docs used `curl -O` — **fixed in docs**
The download URL ends in `/download/`, so `-O` derives an empty filename and fails
(`curl: (23)`). Docs and profile-page examples now use `-o "<id>.tar.gz"`.

## Planned work

### Phase 1 — Harden the current API
Land fixes #2/#3, get the scoped WAF exception + rate-based rule for #1, and add
per-endpoint DRF throttling (`ScopedRateThrottle`, ~1 search/s) with standard
rate-limit response headers.

**Throttling — done** (`caper/caper/throttles.py`, `REST_FRAMEWORK` in `settings.py`).
Per-endpoint scopes with a higher limit for token-authenticated callers, 429 +
`Retry-After` on the way out, counters in a dedicated cache alias. Tests in
`tests/test_api_throttling.py`. Two things worth knowing before changing it:

- `NUM_PROXIES = 1` is load-bearing. Without it DRF keys throttling on the whole
  client-supplied `X-Forwarded-For` chain, and any caller can mint unlimited
  identities by prepending junk. The ALB appends the real client IP last.
- The limits are **per client**. They stop one runaway script and give clients a
  documented back-off signal. They do nothing against a distributed crawl —
  that is what the read-amplification work is for.

**WAF exception — applied.** Confirmed in the live ACL 2026-09-04; the paragraph
below described the plan and is kept because it explains the ordering.

**Original proposal:** Rate rule + `/api/v1/*` prefix allow,
ordered so `KnownBadInputs` still inspects API traffic (`Allow` is terminating in
WAF, so a naive priority-0 allow would disable it). Bot Control stays enforcing for
the rest of the site. Making the *prefix* the boundary rather than enumerating
endpoints means Phases 2–4 need no further WAF edits.

### Phase 2 — Site-wide search endpoint — **done, as `/api/v1/features/`**
The plan was to wrap `search.perform_search()` as `GET /api/v1/search/?q=...`. What
shipped instead reads the feature index directly (`caper/api_features.py`): one
row per amplicon, filterable by gene, classification, project, sample name and
the three metadata fields, with `/features/samples/` for per-sample answers and
`/features/facets/` for the filter vocabulary. Not the UI's query language: an
unknown parameter or value is a 400 rather than silence, because a filter that
is silently dropped answers a narrowed question with the whole corpus.

### Phase 3 — Python client library (retired, 2026-09-15)
Not built, and not planned. The reasoning: `/features/` answers most questions
in one `requests.get`, the OpenAPI document is complete, errors are uniform, and
the clients the API was hardened for — agents — do not install packages. A
wrapper would be a second place for the contract to live, which is the defect
this repository keeps producing. If a human user asks for one, that request is
the evidence this paragraph lacks; until then it stays an idea. The original
sketch follows for reference.

A thin, `pip`-installable client over the REST API: handles auth, redirects, filenames,
retries, client-side rate limiting, and sets a proper User-Agent under the hood. Reads
results straight into pandas.

```python
# Aspirational interface
from ampliconrepository import Client
client = Client(token="...")            # token optional for public data
hits = client.search("MYC ecDNA")
df = client.read_results(hits[0].id)    # results/aggregated_results.csv -> DataFrame
client.download_project(hits[0].id)     # full .tar.gz archive
```

### Phase 4 — Machine-readable interface & AI-agent enablement

**Before doing the `robots.txt` / `llms.txt` work, read this.** On 2026-09-04,
`get-sampled-requests` over a 3-hour window returned 500 sampled Bot Control
blocks. Of the ~108 carrying AI-crawler User-Agents (`GPTBot`, `ClaudeBot`,
`ChatGPT-User`, `OAI-SearchBot`, `PerplexityBot`, `Google-Extended`, `Applebot`),
essentially all came from **one GCP host, 34.62.98.30**, rotating through those
names while probing for `/actuator/env`, `/home/node/.aws/credentials`,
`/jenkins/credentials.xml` and SSRF against `169.254.169.254`. Two requests in the
window looked like genuine crawlers.

Two consequences:

* **Never allowlist AI crawlers by User-Agent.** It is an unauthenticated claim,
  and on this site it is already being worn by an attacker; a UA allowlist would
  be a direct bypass around Bot Control. The only safe mechanism is Bot Control's
  *verified*-bot label (reverse-DNS validated), matched in a rule evaluated after
  the managed group runs in `Count` mode.
* **The premise that we are turning away lots of agent traffic is unmeasured.**
  It may also be circular — crawlers may not come *because* `/robots.txt` 403s
  them and `Disallow: /api/` tells the ones that get through to stay out. WAF
  logging was enabled to `aws-waf-logs-amplicon` on 2026-09-04 (BLOCK-only,
  30-day retention, `authorization`/`cookie` redacted) to settle it with data
  rather than a 3-hour sample. Let it accumulate before changing Bot Control.
Goal: **AI agents that know about AmpliconRepository can discover and use the API to
answer user questions** (e.g. "does AmpRepo have ecDNA calls for MYC in gastric cancer —
pull the sample table"). Deliverables:

- **`llms.txt` at the site root** naming the API and linking the docs + OpenAPI spec, with
  a minimal task recipe. This is the convention agents increasingly look for.
- **OpenAPI/Swagger spec — done, 2026-09-04.** `drf-spectacular` 0.27.2 generates an
  OpenAPI 3.0.3 document served at `/api/v1/openapi.json`. Inside the `/api/v1/`
  prefix on purpose: that prefix is what `AllowApiV1` lets through, so the spec is
  reachable by the same clients as the endpoints it describes. A `PREPROCESSING_HOOKS`
  entry restricts it to `/api/v1/`, keeping the write endpoints
  (`/upload_api/`, `/add_samples_to_project_api/`) out — their contract is with
  released AmpliconSuiteAggregator versions, not with this document.

  Writing it surfaced three defects, all fixed in the same change:

  1. **401 where 403 was meant.** An *authenticated* caller who was not a project
     member got `401 Authentication required` from all three project endpoints.
     For an unattended client the two codes mean opposite things — 401 says retry
     with credentials, 403 says stop — so the old behaviour invited an infinite
     retry loop. Now 401 for anonymous, 403 for authenticated non-members.
  2. **A silent no-op on the batch endpoint.** `POST /api/v1/projects/download/`
     read `ids` from the body and defaulted to `[]`, so a caller that misspelled
     the key got `200 {"downloads": [], "skipped": []}` — indistinguishable from a
     truthful "none of these are downloadable". Found by making that exact mistake
     against production. A missing or non-list `ids` is now a `400` with a code.
  3. **Two error shapes.** The v1 views returned `{"error": ...}`; DRF's own
     failures — throttling above all — returned `{"detail": ...}`.
     `caper/api_errors.py` is installed as `EXCEPTION_HANDLER` and restates
     framework errors in the v1 shape, adding a stable `code` and, on 429, a
     `retry_after`. **Scoped to `/api/v1/` by URL prefix** so it cannot reach the
     upload endpoints.

  Tests: `tests/test_api_openapi.py` (16). The load-bearing one is
  `test_every_v1_route_is_documented`, which fails if a route is added under
  `/api/v1/` without an annotation — a generated spec's whole value is that it
  cannot silently under-describe the API, and nothing else enforces that.
- **Reachable by default clients** — depends on the #1 WAF fix (agents send library UAs).
- **Predictable, self-describing responses** — stable field names, strict valid JSON
  (no `NaN`/`Inf`), consistent error bodies/status codes, stable IDs across calls.
- **Task-oriented quickstart** — "answer a question in three calls": search → pick `id` →
  fetch `samples/` or download and read `results/aggregated_results.csv`.
- **Clear capability boundaries** — document public vs. token-gated and read-only nature.

## Deployment note

Everything above is on production as of 2026-09-15, and #600 is closed. The
WAF's second half (#630, change B) was applied the same day: Bot Control's
`CategoryAI` rule is overridden to Count and a `BlockUnverifiedAI` rule keyed
on the verified labels re-blocks the impostors, so a verified AI agent can now
read the HTML pages as well as the API. Phase 3 is retired (see above).
</content>
