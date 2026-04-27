"""Tests for the chrome_debug.log scanner inside solo_test.py.

The scanner is the heart of the per-extension solo-test feature —
it parses Chrome's verbose log to surface "extension load failed"
errors. Regex patterns drift across Chrome versions, so wide-ish
coverage is essential."""

from __future__ import annotations

import pytest


@pytest.fixture
def scanner():
    from ghost_shell.extensions import solo_test
    return solo_test


def test_scan_empty_log(scanner):
    errors, warnings = scanner._scan_log("")
    assert errors == []
    assert warnings == []


def test_scan_none(scanner):
    errors, warnings = scanner._scan_log(None)
    assert errors == []
    assert warnings == []


def test_scan_detects_extension_load_failed(scanner):
    log = """
[ERROR:something:42] Extension load failed: bad manifest
[INFO:other:1] regular log line
"""
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1
    assert "extension load" in errors[0].lower()


def test_scan_detects_could_not_load(scanner):
    log = "Could not load extension: missing service_worker.js"
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1


def test_scan_detects_default_locale_error(scanner):
    log = "Default locale file not found for ja"
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1


def test_scan_detects_service_worker_failure(scanner):
    log = "Service worker registration failed: 'sw.js' not found"
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1


def test_scan_detects_invalid_match_pattern(scanner):
    log = "Invalid value for 'matches': bad pattern"
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1


def test_scan_warning_pattern(scanner):
    log = "Permission 'foo' is unknown or URL pattern is malformed"
    errors, warnings = scanner._scan_log(log)
    assert len(warnings) == 1


def test_scan_dedupes_repeated_errors(scanner):
    log = """
Extension load failed: same error
Extension load failed: same error
Extension load failed: same error
"""
    errors, _ = scanner._scan_log(log)
    assert len(errors) == 1   # de-duped


def test_scan_caps_at_10_errors(scanner):
    """Defensive cap — log can be huge."""
    log = "\n".join(f"Extension load failed: error #{i}" for i in range(50))
    errors, _ = scanner._scan_log(log)
    assert len(errors) <= 10


def test_scan_truncates_long_lines(scanner):
    long_line = "Extension load failed: " + ("X" * 10000)
    errors, _ = scanner._scan_log(long_line)
    assert len(errors) == 1
    assert len(errors[0]) <= 300


def test_scan_error_takes_precedence_over_warning(scanner):
    """A line matching both buckets should land in errors only."""
    # construct a line that matches BOTH an error pattern AND a
    # warning pattern (unlikely in practice but ensures no double-
    # counting)
    log = "Extension load failed: Permission 'foo' is unknown"
    errors, warnings = scanner._scan_log(log)
    assert len(errors) == 1
    # The same string should NOT also be in warnings
    assert all(errors[0] != w for w in warnings)


def test_tail_returns_last_lines(scanner):
    text = "\n".join(f"line{i}" for i in range(50))
    result = scanner._tail(text, n_lines=10)
    lines = result.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "line49"


def test_tail_empty_input(scanner):
    assert scanner._tail("", 10) == ""
    assert scanner._tail(None, 10) == ""


# ── _result helper ─────────────────────────────────────────

def test_result_loads_status_is_ok(scanner):
    r = scanner._result("loads", duration=3.5, errors=[], warnings=[])
    assert r["ok"] is True
    assert r["status"] == "loads"
    assert r["duration"] == 3.5


def test_result_other_statuses_not_ok(scanner):
    for status in ["fails", "warnings", "no_chrome", "no_pool", "not_found", "error"]:
        r = scanner._result(status)
        assert r["ok"] is False
        assert r["status"] == status


def test_result_default_fields_always_present(scanner):
    r = scanner._result("error")
    # All schema fields must be present even when caller skipped them
    for k in ("ok", "status", "duration", "exit_code", "errors",
              "warnings", "log_excerpt", "reason"):
        assert k in r
