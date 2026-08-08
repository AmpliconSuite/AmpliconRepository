# Test-suite audit

Date: 2026-08-07

This audit uses the current development worktree as the baseline, including the
new `test_sample_plot_cnv_formats.py`. It does not recommend deleting coverage
merely to reduce line count, and no file moves or test deletions were made as
part of the audit.

## Executive recommendation

The suite is large because the application has accumulated real regression
boundaries: ingestion, MongoDB/GridFS, version history, search semantics,
request amplification, throttling, load shedding, and tar extraction safety.
The newest large modules are generally justified. The useful correctness
cleanup was much smaller than a rewrite and is now complete:

1. Tests backed by `loaded_datasets` now assert that its projects remain in
   MongoDB instead of turning a broken fixture contract into a skip.
2. Browser setup, session injection, and submitted-login failures now fail an
   explicitly requested browser run.
3. The two genuinely duplicated tests were removed.
4. API tests that do not start aggregation no longer carry the `slow` marker.
5. The coding-agent test-addition policy was added to `AGENTS.md`.

The two add-samples authorization tests intentionally continue to use an
existing non-deleted MongoDB project. This keeps their authorization checks
grounded in realistic development data; an empty database remains an unmet
precondition for those two cases.

Do not broadly merge the API, amplification, lifecycle, or search families.
Their similar names conceal different failure boundaries, and combining them
would mostly create larger files rather than a more understandable suite.

## Measurements

| Measure | Current result |
|---|---:|
| Test modules | 30, plus `conftest.py` |
| Python lines under `tests/` | 11,099, including `conftest.py` |
| Syntactic test functions | 388 |
| Collected cases, including parametrization | 431 |
| Default full run | 416 passed, 15 skipped in 137.58 s |
| Browser run against `127.0.0.1:8000` | 10 passed in 19.37 s |

The 15 default-run skips are the 10 browser cases (no `--base-url`) and five
optional archive cases. With the browser suite run separately, all available
tests passed and there were no xfails.

Runtime remains highly concentrated. The shared `loaded_datasets` setup took
15.11 seconds; the five reaggregation calls took 58.75 seconds; and the eight
roughly six-second create/search/lifecycle calls took another 47.29 seconds.
Those 14 events account for about 121 seconds, or 88% of the default run. Most
of the remaining 400-plus cases are cheap. Runtime should therefore be improved
by avoiding unnecessary aggregation setup and duplicate end-to-end paths, not
by deleting broad unit coverage.

## Genuine duplication

The two pairs found at the same or effectively the same boundary were removed:

1. `test_browser.py::test_homepage_has_project_rows` was subsumed by
   `test_homepage_loads`. Both wait for `#unifiedProjectTable`; the former then
   asserted `count >= 0`, which was tautologically true.
2. `test_error_handling.py::test_private_project_unauthenticated_redirect`
   duplicated the initial private-project phase of
   `test_project_lifecycle.py::test_visibility_cycle`. Both call
   `project_page()` directly and assert a login redirect. Keep the lifecycle
   test, which additionally verifies the public/featured/private transitions.
   Keep the Playwright private-project test because that is a different
   boundary (URL routing, middleware, and rendered login page).
After those two removals, the suite has 388 test functions. The following
apparent overlaps should remain:

- `test_reaggregation_does_not_double_count_stats` exercises the real
  create/edit pipeline, while
  `test_replacement_updates_sample_counts_without_double_counting_project`
  exercises statistics bookkeeping directly. They fail at different layers.
- The read-amplification response assertions deliberately overlap ordinary API
  response tests. They prove that adding a MongoDB projection did not preserve
  performance by silently dropping the payload.
- Search helper tests and search view tests overlap in semantics but validate
  helper correctness and request-to-query wiring separately.
- Keep both API v1 project-detail 404 tests. One isolates the view's `None`
  handling; the other exercises the real MongoDB lookup. The integration test
  should stop requesting the unused `loaded_datasets` fixture and lose its
  `slow`/`functional` marks, but its database boundary is still distinct.

## File consolidation

An organization-only pass can make these moves without changing any tests:

| Before | After | Count after |
|---|---|---:|
| `test_admin_stats_projects.py` (2) + `test_site_stats.py` (11) | `test_site_stats.py` | 13 |
| `test_project_feature_count.py` (1) + `test_project_page_templates.py` (6) | `test_project_page_templates.py` | 7 |
| `test_coamp_graph.py` (1) + `test_classification_charts.py` (8) | `test_focal_amplification.py` | 9 |

This would change 30 modules to 27 while retaining all 388 functions. The third
output gets a broader name because graph construction and chart/template
compatibility are all focal-amplification compatibility, but one is not a
chart test.

Recommendations for the other candidate families:

- Keep `test_api.py` separate from `test_api_v1.py`. The former tests the
  legacy upload/add-samples endpoints; the latter tests the versioned read,
  download, and token API. If touched later, rename `test_api.py` to
  `test_upload_api.py`; do not merge 9 tests into the already 70-test v1 file.
- Keep `test_api_throttling.py` separate. Its module-wide `throttled` marker and
  real-cache setup are a genuine execution boundary.
- Keep `test_api_read_amplification.py`,
  `test_sample_page_read_amplification.py`, and `test_page_weight.py` separate.
  They cover MongoDB projections in API views, MongoDB projection in the sample
  helper, and Plotly response bytes respectively. They share an outage theme,
  not an implementation or fixture boundary.
- Keep the four project lifecycle/version/cleanup modules separate.
  `test_create_edit_project.py` is primarily metadata and ingestion,
  `test_project_lifecycle.py` is end-to-end state transition coverage,
  `test_project_version_cleanup.py` is tombstone/GridFS behavior, and
  `test_cleanup_orphaned_projects.py` covers a standalone maintenance script.
- Keep `test_search.py` together for now. Its 76 functions are organized into
  helper and integration sections under one coherent subject. Splitting it
  would increase file count without eliminating duplication. Reconsider only
  if marker selection or ownership becomes difficult.

## Marker audit

The raw decorator counts are 132 `integration`, 30 `slow`, 21 `functional`,
10 `browser`, zero `performance`, and one `skipif`. These counts understate
module-wide marks: all 11 account-password tests and all 10 tar-safety tests are
also `integration`, and all 13 throttling tests receive `throttled` at module
scope.

The markers mostly support the documented workflows, with these corrections:

- All 11 `test_site_stats.py` tests are marked `slow`, but the measured calls
  were at most about 0.35 seconds and do not run aggregation. Remove `slow` and
  retain `integration`.
- The five API v1 tests in the functional section no longer carry `slow`. Four
  consume the session-scoped `loaded_datasets` fixture; the 404 test no longer
  requests that unused fixture or carries `functional`. `slow` now means the
  test starts additional aggregation, while `functional` means it consumes the
  shared aggregated datasets.
- The two `test_api.py` add-samples authorization checks now carry only
  `integration`. They still select an existing MongoDB project, by design, but
  reject before aggregation.
- `performance` is unused, and its description points to the nonexistent
  `tools/performance_test.py`; the script is actually `performance_test.py` at
  repository root. Remove the marker declaration and document the standalone
  script, or correct the path if a future pytest benchmark is planned.
- `throttled`, `browser`, and the single `skipif` are doing useful work and
  should remain.

The existing fast command excludes both `slow` and `functional`, so it remains
safe while the remaining optional marker cleanup is pending.

## Escape hatches and nondeterminism

The five remaining body-level `pytest.xfail()` calls were real escape hatches.
They have been replaced by assertions in the accompanying focused change. No
`pytest.xfail()` calls remain.

Additional cases resolved during the audit follow-up:

- The tautological `test_homepage_has_project_rows` was removed.
- Three API v1 functional tests now assert when a project promised by
  `loaded_datasets` cannot be read.
- Once a browser run is explicitly requested with `--base-url`, failures to
  import Django models, create a test user, inject a session, or complete a
  submitted login are now failures rather than skips.
- The two add-samples authorization tests intentionally select an existing
  project and skip when the development database is empty. This is retained as
  a realistic-data precondition rather than replaced with a synthetic fixture.
- Missing `--base-url`, missing optional ingestion archives, absent OAuth
  `SocialApp` configuration, and absent browser seed projects are legitimate
  preconditions. The OAuth check would report more clearly as its own test so
  the preceding password-form assertions do not end with a SKIP status.
- The `except Exception: pass` blocks in `test_browser.py` are teardown only and
  cannot make assertions pass. Logging teardown failures would help diagnose
  leaked users/sessions, but this is not a correctness escape hatch.
- The `Http404` exception branches in `test_error_handling.py` explicitly
  accept either a raised `Http404` or a 404 response and still reject unexpected
  exception types. They are not silent passes.

## Coding-agent guidance

The following policy was added to the tracked `AGENTS.md`:

> ### Test addition policy
>
> Do not add a new test automatically for every code edit. First search for the
> nearest existing test that owns the behavior.
>
> Add or strengthen a test when the change reproduces a bug, closes a numbered
> issue, protects an outage/security/data-integrity invariant, or introduces a
> materially new branch that existing assertions do not exercise. For a
> refactor, text/style-only change, or behavior already covered at the same
> boundary, run the existing test and add nothing.
>
> Prefer extending the nearest existing test and file. Create a new test file
> only for a distinct subsystem or when it requires a different marker,
> fixture, or execution environment. A one-test file needs a short reason why
> it does not belong in an existing subject file.
>
> Test at the lowest layer that proves the regression. Do not repeat the same
> outcome with a mock, a direct view call, and a browser test unless each test
> names the different failure boundary it protects.
>
> `skip` and `xfail` may guard an unmet precondition checked before the system
> under test runs. Never skip or xfail because the tested action failed. Assert
> that fixtures created by the suite remain available.
>
> Before adding a test file, state in the change summary which existing file was
> considered and why it was not the right home.

This policy addresses the growth rate without imposing a test quota or
discouraging regression coverage.

## Optional remaining cleanup

The correctness work is complete. If further organization is worthwhile:

1. Remove the inaccurate `slow` marks from `test_site_stats.py` and resolve the
   stale performance-script marker/path.
2. Perform the three file consolidations as pure moves, then run the same full
   and browser commands.

Neither item is urgent. Keeping marker edits separate from file moves will make
review and `git blame` substantially easier.
