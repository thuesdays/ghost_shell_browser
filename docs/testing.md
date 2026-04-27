# Running the test suite

```powershell
# install dev deps once
pip install -r requirements-dev.txt

# run the unit suite (skips integration tests by default)
pytest

# with coverage
pytest --cov=ghost_shell --cov-report=term-missing

# run a single file
pytest tests/test_lock_helpers.py -v

# run integration tests too (needs chrome_win64 + a working proxy)
GHOST_SHELL_INTEGRATION=1 pytest -m integration
```

## What's covered

The pure-unit suite (~80+ test cases across 7 files) exercises the
modules that gained complexity during Sprint 1 + Sprint 2:

| File | Coverage |
|---|---|
| `test_lock_helpers.py` | `.ghost_shell.lock` JSON read/write/heartbeat + live/stale detection (RC-33) |
| `test_process_reaper.py` | `kill_chrome_for_user_data_dir` cmdline matching, `is_profile_actually_running` DB-level check, ready-to-launch helper |
| `test_solo_test_scanner.py` | chrome_debug.log error/warning regex patterns + tail/result helpers |
| `test_version_check.py` | Chrome / chromedriver version regex, mismatch verdict, cache TTL |
| `test_jobs_queue.py` | Background ThreadPoolExecutor: enqueue, get_status, error handling, cancel, queue-full |
| `test_fingerprint_domains.py` | Sprint 1.3 `by_domain` breakdown — webdriver-collapses-automation property |
| `test_db_profile_cascade.py` | `profile_mark_ready`, `profile_is_ready`, `profile_delete_cascade` |

## What's NOT covered (yet)

* The `runtime.py` launch state machine — full integration (mock
  selenium + filesystem) would need ~2 days of fixture work.
  Today's unit tests cover the helpers it depends on.
* `ext` manifest gate — covered indirectly via `test_solo_test_scanner.py`.
* Frontend — no JS test framework wired. Manual smoke test list in
  `docs/audit/sprint-2-profile-workflow-audit.md` covers the click
  paths that broke today (rotating proxy persistence, etc).

## Why this exists

Three Edit-tool truncations hit production today (`runtime.py:close()`,
`profile-detail.js:_wireProfileExtModalCards`, `database.py` mid-method)
and each one was a silent regression — file parsed, function "ran", but
did nothing. `python -m pytest` would catch this kind of corruption
because the test imports the function and calls it; truncation =
`AttributeError` or `SyntaxError`.

Wire `pytest` into your pre-push hook:

```bash
# .git/hooks/pre-push
#!/bin/bash
cd "$(git rev-parse --show-toplevel)"
pytest -q || exit 1
```

10 seconds of CI is cheaper than a regressed `close()` shipping for
4 commits.
