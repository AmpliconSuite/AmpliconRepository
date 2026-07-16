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
| **Site-wide search** | `GET /api/v1/search/` | 🔜 Planned |

## Known issues & status

### 1. Load-balancer blocks non-browser User-Agents — **infra, open**
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

### 2. `GET /samples/` returned 500 — **fixed in working tree, undeployed**
`_sample_to_dict` returned bson `ObjectId` GridFS refs (and occasional `NaN`/`Inf`
floats) that DRF's JSON renderer cannot serialize. Fix (`views_apis.py`): drop
ObjectId-valued fields, sanitize non-finite floats via `_json_safe`. Regression tests in
`tests/test_api_v1.py::TestSampleToDict`.

### 3. Batch `download_url` used `http://` — **fixed in working tree, undeployed**
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

### Phase 2 — Site-wide search endpoint
Wrap the existing `search.perform_search()` as `GET /api/v1/search/?q=...` (genes,
projects, classifications, metadata; same wildcard/logic support as the UI), returning
JSON that links to project and sample endpoints.

### Phase 3 — Python client library
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
Goal: **AI agents that know about AmpliconRepository can discover and use the API to
answer user questions** (e.g. "does AmpRepo have ecDNA calls for MYC in gastric cancer —
pull the sample table"). Deliverables:

- **`llms.txt` at the site root** naming the API and linking the docs + OpenAPI spec, with
  a minimal task recipe. This is the convention agents increasingly look for.
- **OpenAPI/Swagger spec** at a stable URL (e.g. `/api/v1/openapi.json`) so agents and
  codegen enumerate endpoints/params/response shapes without scraping prose. DRF has
  schema generators (`drf-spectacular`) that fit our existing `APIView`s.
- **Reachable by default clients** — depends on the #1 WAF fix (agents send library UAs).
- **Predictable, self-describing responses** — stable field names, strict valid JSON
  (no `NaN`/`Inf`), consistent error bodies/status codes, stable IDs across calls.
- **Task-oriented quickstart** — "answer a question in three calls": search → pick `id` →
  fetch `samples/` or download and read `results/aggregated_results.csv`.
- **Clear capability boundaries** — document public vs. token-gated and read-only nature.

## Deployment note

Fixes #2/#3 (code) and the doc/profile-page changes are complete but must be deployed for
the live site to reflect them. #1 requires an AWS-side WAF change by the account owner.
</content>
