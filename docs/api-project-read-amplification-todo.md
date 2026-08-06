# Read amplification in the v1 project API views — DONE

Same class of problem as the `get_one_sample()` fix in v2.18.6_080526, in a
different place. Found while verifying that release did not damage the API
downloads. Implemented and measured 2026-08-06.

## The test that matters

Whether bytes read from DocumentDB are proportional to bytes returned. Reading
a large document to *return* a large document is fine; reading it to return a
few KB is not.

Every view below called `get_one_project(project_id)`, which fetches the whole
project document with **no projection at all** — including the full `runs` dict
of every sample and feature row.

## Measured, before and after

Method: drive the real view via `APIRequestFactory`, count bytes over the
loopback interface to MongoDB across 10 requests after a warm-up. Local dev DB.

**Public project — 2.77 MB document, 329 samples**

| endpoint | response | read before | read after | amplification |
|---|---|---|---|---|
| `/api/v1/projects/<id>/` | 5.0 KB | 2,838 KB | **9.3 KB** | 572x → 1.9x |
| `/api/v1/projects/<id>/download/` | 302, 0 KB | 2,838 KB | **11.6 KB** | 244x fewer bytes |
| `/api/v1/projects/<id>/samples/` | 2,203 KB | 2,836 KB | 2,729 KB | 1.3x → 1.2x (correctly unchanged) |

**Private project, anonymous caller — 8.56 MB document, 2,471 samples**

All three endpoints return a 35-byte 401. Reads before / after:

| endpoint | before | after |
|---|---|---|
| detail | 9,113 KB | **12.1 KB** |
| download | 8,783 KB | **12.1 KB** |
| samples | 8,896 KB | **12.1 KB** |

The 401 case was the worst of it and was **not** anticipated when this note was
first written: the document load happened *before* the access check, so any
unauthenticated caller could force a ~9 MB database read on a private project
and get back 35 bytes. That is a remotely triggerable read amplifier needing no
credentials — the same shape as the traffic that took production down.

## What changed

`caper/caper/views_apis.py`

* `ProjectDetailView`, `ProjectDownloadView`, `ProjectBatchDownloadView` now
  call `get_one_project_sans_runs(project_id, _PROJECT_METADATA_PROJECTION)`.
* `ProjectSamplesView` authorizes on the metadata document first, then fetches
  `runs` in a second query. It still returns every run — that *is* its payload —
  but an unauthorized caller no longer triggers the read.
* `_PROJECT_METADATA_PROJECTION` excludes `runs`, `sample_data`, `ecDNA_context`
  and `aggregate_df`. All four scale with sample count and none is read by
  `_project_to_dict()`, `_user_can_access_project()`, or the download views.
  On the 2,471-sample project those four were 8.5 MB of the read.
  **Keep it in sync with `_project_to_dict()` if that gains a field.**

`caper/caper/utils.py`

* `get_one_project_sans_runs()` takes an optional `projection`, defaulting to
  the previous `{'runs': 0}`. Existing callers are unaffected. The projection is
  parameterized here rather than in a new loader so the lookup chain (ObjectId,
  alias, project name, `current: False` versions, deleted-version redirect
  tombstones) stays in one place — it is easy to get wrong, and a regression in
  it was caught during the v2.18.6 work.

Two `get_one_project()` calls were deliberately left alone: the authenticated
`ProjectFileAddView` upload path, and the subscriber-notification lookup that
reads `runs` for a sample count. Neither is crawler-reachable.

## Verification

* `tests/test_api_read_amplification.py` — 6 new tests. Written failing first;
  4 of them failed against the old code for the stated reason. They spy on the
  real loaders (MongoDB is never mocked) and assert no `runs` document is
  pulled, including for anonymous callers, plus a counter-case asserting
  `ProjectSamplesView` still returns all samples with their full payload.
* `tests/test_api_v1.py` — 70 pre-existing tests still pass. 24 patch targets in
  sections D–G were retargeted from `get_one_project` to
  `get_one_project_sans_runs`; the samples tests additionally stub the separate
  runs fetch. These were fixture updates, not behaviour changes.
* Responses are **byte-identical** before and after: SHA-256 of the rendered
  body matched on 3 real projects across detail / samples / batch-download and
  both 401 paths (2.26 MB samples payload included).
* `test_sample_page_read_amplification.py`, `test_api.py`, `test_downloads.py`,
  `test_project_version_cleanup.py`, `test_robots_txt.py`,
  `test_project_page_templates.py`, `test_project_feature_count.py`,
  `test_project_lifecycle.py` all pass unchanged.

## Still worth doing

Re-run the measurement on prod against its largest project. Prod documents are
larger than dev's, so the absolute saving should be bigger; the shape should be
the same.
