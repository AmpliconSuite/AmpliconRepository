# Running the tests

The test policy (write a failing test first; when to add a test and when not to)
is in [AGENTS.md](../AGENTS.md). This page is the mechanics.

## Markers

Tests live in `tests/` and are run with `pytest` from the repository root, where `pytest.ini` defines markers:
- `integration`: live MongoDB, normally fast
- `slow`: real AmpliconSuiteAggregator project creation/reaggregation
- `functional`: depends on `loaded_datasets` and usually a local dev server
- `browser`: Playwright tests against `http://localhost:8000`
- `performance`: benchmarking only

New tests should use the narrowest marker that matches behavior. Use `@pytest.mark.integration` for live-Mongo unit/view behavior, add `@pytest.mark.slow` only when the test runs real aggregation, add `@pytest.mark.functional` when it depends on the shared `loaded_datasets` flow or local server, and add `@pytest.mark.browser` for Playwright.

```python
import pytest

@pytest.mark.integration
def test_my_feature(client, live_mongo):
    # arrange → act → assert
    ...
```

## Commands

```bash
# Fast integration tests only — MongoDB required, no aggregation/server.
source caper/config.sh && pytest -m "integration and not slow and not functional and not browser" -v

# All integration + functional tests — requires local server and AmpliconSuiteAggregator.
source caper/config.sh && pytest -m "integration and not browser" -v

# Full slow aggregation tests.
source caper/config.sh && pytest -m "slow and integration" -v

# Browser tests; start the server from caper/ in a separate shell first.
source caper/config.sh && pytest -m browser --base-url http://localhost:8000 -v

# Full suite
source caper/config.sh && pytest -v
```

## Test datasets

Additional test data files (tarballs, sample inputs, etc.) are available on Google Drive:
https://drive.google.com/drive/folders/1lp6NUPWg1C-72CQQeywucwX0swnBFDvu?usp=drive_link
