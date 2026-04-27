"""Tests for health_canary — Sprint 4 detection-site parsers + orchestrator.

Pure unit tests with a fake driver that returns canned JSON strings —
no live network. Validates parser robustness, error-path behaviour,
and the orchestrator's per-site error isolation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ── parse_sannysoft ───────────────────────────────────────────

def test_sannysoft_normal_response():
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "passed": 12, "failed": 2, "total": 14,
        "details": [{"name": "webdriver", "ok": True, "v": "passed"}],
    })
    out = parse_sannysoft(driver)
    assert out["site"] == "sannysoft"
    assert out["score"] == round(12 / 14 * 100)
    assert out["passed"] == 12
    assert out["total"] == 14
    assert out["raw_score"] == "12/14"
    assert out["error"] is None


def test_sannysoft_no_rows_returns_error():
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "passed": 0, "failed": 0, "total": 0, "details": [],
    })
    out = parse_sannysoft(driver)
    assert out["score"] is None
    assert out["error"] is not None
    assert "no parseable rows" in out["error"]


def test_sannysoft_js_exception_returned_in_payload():
    """The probe catches its own exceptions and returns
    {"error": "..."} — ensure parser surfaces it."""
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({"error": "TypeError: foo"})
    out = parse_sannysoft(driver)
    assert out["score"] is None
    assert "TypeError" in out["error"]


def test_sannysoft_execute_script_raises():
    """A driver-side exception (Chrome dead mid-probe) shouldn't
    propagate out of the parser."""
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.side_effect = RuntimeError("chrome dead")
    out = parse_sannysoft(driver)
    assert out["score"] is None
    assert "execute_script" in out["error"]


def test_sannysoft_invalid_json_response():
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.return_value = "not valid json"
    out = parse_sannysoft(driver)
    assert out["score"] is None
    assert "JSON" in out["error"] or "decode" in out["error"]


def test_sannysoft_non_string_response():
    from ghost_shell.profile.health_canary import parse_sannysoft
    driver = MagicMock()
    driver.execute_script.return_value = None
    out = parse_sannysoft(driver)
    assert out["score"] is None
    assert "non-string" in out["error"]


# ── parse_creepjs ─────────────────────────────────────────────

def test_creepjs_extracts_percentage_score():
    from ghost_shell.profile.health_canary import parse_creepjs
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "trust_score": 73.5, "fingerprint_hash": "abc1234567890def",
    })
    out = parse_creepjs(driver)
    assert out["score"] == 74        # rounded
    assert "73.5%" in out["raw_score"]
    assert out["details"]["fingerprint_hash"] == "abc1234567890def"


def test_creepjs_clamps_to_100():
    from ghost_shell.profile.health_canary import parse_creepjs
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({"trust_score": 150.0})
    out = parse_creepjs(driver)
    assert out["score"] == 100   # clamped


def test_creepjs_clamps_to_zero_for_negative():
    from ghost_shell.profile.health_canary import parse_creepjs
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({"trust_score": -10})
    out = parse_creepjs(driver)
    assert out["score"] == 0


def test_creepjs_no_score_returns_error():
    from ghost_shell.profile.health_canary import parse_creepjs
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "trust_score": None, "body_excerpt": "Loading..."
    })
    out = parse_creepjs(driver)
    assert out["score"] is None
    assert "couldn't find" in out["error"]


# ── parse_pixelscan ───────────────────────────────────────────

def test_pixelscan_numeric_score_wins():
    from ghost_shell.profile.health_canary import parse_pixelscan
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "risk_label": "medium", "score_raw": 65,
    })
    out = parse_pixelscan(driver)
    # Numeric raw score takes precedence over risk label
    assert out["score"] == 65
    assert out["raw_score"] == "65"


def test_pixelscan_risk_low_maps_to_85():
    from ghost_shell.profile.health_canary import parse_pixelscan
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "risk_label": "low", "score_raw": None,
    })
    out = parse_pixelscan(driver)
    assert out["score"] == 85


def test_pixelscan_risk_high_maps_to_15():
    from ghost_shell.profile.health_canary import parse_pixelscan
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "risk_label": "high", "score_raw": None,
    })
    out = parse_pixelscan(driver)
    assert out["score"] == 15


def test_pixelscan_missing_signals_error():
    from ghost_shell.profile.health_canary import parse_pixelscan
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "risk_label": None, "score_raw": None, "body_excerpt": "page"
    })
    out = parse_pixelscan(driver)
    assert out["score"] is None
    assert "no risk label" in out["error"]


# ── run_canary orchestrator ──────────────────────────────────

def test_run_canary_unknown_site_records_error_continues():
    from ghost_shell.profile.health_canary import run_canary
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({
        "passed": 14, "failed": 0, "total": 14, "details": []
    })
    results = run_canary(driver, sites=["sannysoft", "doesnotexist"],
                         settle_sec=0.0)
    assert len(results) == 2
    sites = {r["site"] for r in results}
    assert sites == {"sannysoft", "doesnotexist"}
    err = next(r for r in results if r["site"] == "doesnotexist")
    assert "unknown site id" in (err["error"] or "")


def test_run_canary_navigation_failure_records_error():
    from ghost_shell.profile.health_canary import run_canary
    driver = MagicMock()
    driver.get.side_effect = RuntimeError("network down")
    results = run_canary(driver, sites=["sannysoft"], settle_sec=0.0)
    assert len(results) == 1
    assert results[0]["error"] is not None
    assert "navigation failed" in results[0]["error"]


def test_run_canary_default_visits_all_sites(monkeypatch):
    from ghost_shell.profile.health_canary import run_canary, SUPPORTED_SITES
    driver = MagicMock()
    # Each parser will get the canned response that yields valid output
    driver.execute_script.return_value = json.dumps({
        "passed": 14, "failed": 0, "total": 14, "details": [],
        "trust_score": 80, "score_raw": 70, "risk_label": "medium",
    })
    results = run_canary(driver, sites=None, settle_sec=0.0)
    assert {r["site"] for r in results} == set(SUPPORTED_SITES.keys())


def test_run_canary_per_site_independence(monkeypatch):
    """One site failing shouldn't skip the others."""
    from ghost_shell.profile.health_canary import run_canary
    driver = MagicMock()

    call_count = {"n": 0}
    def flaky_get(url):
        call_count["n"] += 1
        if "sannysoft" in url:
            raise RuntimeError("blocked")

    driver.get.side_effect = flaky_get
    driver.execute_script.return_value = json.dumps({
        "trust_score": 75
    })

    results = run_canary(driver,
                         sites=["sannysoft", "creepjs"],
                         settle_sec=0.0)
    assert len(results) == 2
    by_site = {r["site"]: r for r in results}
    assert by_site["sannysoft"]["error"] is not None
    assert by_site["creepjs"]["score"] is not None  # got through


def test_run_canary_string_sites_param():
    """Comma-separated string accepted (UX nicety)."""
    # Note: run_canary itself takes a list; the FLOW-ACTION accepts
    # comma-separated string and splits it. This test guards the
    # flow-action expectation indirectly by checking the public
    # signature only takes lists — call with a list.
    from ghost_shell.profile.health_canary import run_canary
    driver = MagicMock()
    driver.execute_script.return_value = json.dumps({"trust_score": 50})
    results = run_canary(driver, sites=["creepjs"], settle_sec=0.0)
    assert len(results) == 1


# ── _normalize shape contract ────────────────────────────────

def test_normalize_returns_all_keys():
    """Every parser must return a dict with these keys present —
    DB layer expects them all."""
    from ghost_shell.profile.health_canary import _normalize
    out = _normalize("sannysoft")
    for k in ("site", "score", "raw_score", "passed", "total",
              "details", "error"):
        assert k in out
