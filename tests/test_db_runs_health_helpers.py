"""Sprint 8.3 — coverage tests for the runs / health rollup helpers
that the dashboard relies on (captcha sparkline, drift banner, canary
every-N-runs gate).

Helpers under test:
* db.runs_captcha_history(profile_name, days)
* db.runs_captcha_summary(profile_name, days)
* db.health_drift_profiles(days, min_score_drop, min_captcha_rate)
* db.runs_count_for_profile(profile_name, only_finished)
* runner._act_health_check_canary every_n_runs gate behaviour

These were called out as a coverage gap in
``docs/audit/full-session-audit.md`` (FA "coverage gap" line). Each
test is hermetic — uses the in_memory_db fixture from conftest.py
plus direct INSERTs so we don't have to spin up a real run.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _seed_profile(db, name: str = "p1") -> None:
    """Minimal profiles row so FK / sanity-check queries don't trip."""
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO profiles (name, created_at) VALUES (?, datetime('now'))",
        (name,),
    )
    conn.commit()


def _seed_run(db, profile_name: str, started_at: str,
              finished_at: str | None = None,
              captchas: int = 0) -> int:
    """Insert one runs row. Returns the rowid.

    Schema reference (from ghost_shell/db/database.py): the runs
    table has total_ads / total_queries (not ads_processed) and no
    explicit status column — finished vs in-flight is inferred from
    finished_at IS NOT NULL across the codebase. Keeping this in
    sync with the production schema is what these tests are FOR."""
    conn = db._get_conn()
    cur = conn.execute(
        """INSERT INTO runs (profile_name, started_at, finished_at,
                             captchas, total_ads, total_queries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profile_name, started_at, finished_at, captchas, 0, 0),
    )
    conn.commit()
    return cur.lastrowid


def _seed_health(db, profile_name: str, score: int,
                 checked_at: str, site: str = "sannysoft") -> None:
    """Insert one profile_health row for the drift detector tests.

    Column reference (from ghost_shell/db/database.py): the per-row
    JSON payload is stored in ``details``, not ``signals_json``. The
    schema also has raw_score / passed / total / error which we leave
    NULL — they're populated by the real canary action when present."""
    conn = db._get_conn()
    conn.execute(
        """INSERT INTO profile_health
               (profile_name, site, score, checked_at,
                details, run_id)
           VALUES (?, ?, ?, ?, ?, NULL)""",
        (profile_name, site, score, checked_at, "{}"),
    )
    conn.commit()


def _today(offset_days: int = 0) -> str:
    return (datetime.now() + timedelta(days=offset_days))\
        .strftime("%Y-%m-%d %H:%M:%S")


def _today_iso(offset_days: int = 0) -> str:
    return (datetime.now() + timedelta(days=offset_days))\
        .isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────
# runs_captcha_history
# ──────────────────────────────────────────────────────────────

def test_captcha_history_empty_when_no_runs(in_memory_db):
    """No runs → empty list. Dashboard renders empty-state hint."""
    _seed_profile(in_memory_db, "p1")
    out = in_memory_db.runs_captcha_history("p1", days=30)
    assert out == []


def test_captcha_history_aggregates_per_day(in_memory_db):
    """Two runs on the same day collapse into one bucket with summed
    captcha count and a correctly computed rate."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(0), _today(0), captchas=2)
    _seed_run(in_memory_db, "p1", _today(0), _today(0), captchas=3)
    out = in_memory_db.runs_captcha_history("p1", days=30)
    assert len(out) == 1
    row = out[0]
    assert row["run_count"] == 2
    assert row["total_captchas"] == 5
    assert row["captcha_rate"] == 2.5      # 5 captchas / 2 runs
    assert row["date"] == _today(0)[:10]


def test_captcha_history_excludes_inflight_runs(in_memory_db):
    """Runs with finished_at IS NULL are in-flight and shouldn't
    pollute historical aggregates."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(0), None, captchas=99)  # in-flight
    _seed_run(in_memory_db, "p1", _today(0), _today(0), captchas=1)
    out = in_memory_db.runs_captcha_history("p1", days=30)
    assert len(out) == 1
    assert out[0]["run_count"] == 1
    assert out[0]["total_captchas"] == 1


def test_captcha_history_respects_window(in_memory_db):
    """Runs older than ``days`` are excluded."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(-40), _today(-40), captchas=10)
    _seed_run(in_memory_db, "p1", _today(-1),  _today(-1),  captchas=2)
    out = in_memory_db.runs_captcha_history("p1", days=7)
    assert len(out) == 1
    assert out[0]["total_captchas"] == 2


def test_captcha_history_orders_ascending(in_memory_db):
    """Sparkline rendering relies on date-ascending order."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(-3), _today(-3), captchas=1)
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=2)
    _seed_run(in_memory_db, "p1", _today(-2), _today(-2), captchas=3)
    out = in_memory_db.runs_captcha_history("p1", days=30)
    dates = [r["date"] for r in out]
    assert dates == sorted(dates)


def test_captcha_history_zero_captcha_runs_count(in_memory_db):
    """Runs with zero captchas are valid data points (rate=0.0). They
    should show up in the history and contribute to the run_count."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=0)
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=0)
    out = in_memory_db.runs_captcha_history("p1", days=7)
    assert out[0]["run_count"] == 2
    assert out[0]["captcha_rate"] == 0.0


# ──────────────────────────────────────────────────────────────
# runs_captcha_summary
# ──────────────────────────────────────────────────────────────

def test_captcha_summary_empty(in_memory_db):
    """No data → flat trend, zeroed totals."""
    _seed_profile(in_memory_db, "p1")
    out = in_memory_db.runs_captcha_summary("p1", days=7)
    assert out == {"total_runs": 0, "total_captchas": 0,
                   "avg_rate": 0.0, "trend": "flat"}


def test_captcha_summary_totals(in_memory_db):
    """Totals are summed across the window."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=4)
    _seed_run(in_memory_db, "p1", _today(-2), _today(-2), captchas=6)
    out = in_memory_db.runs_captcha_summary("p1", days=7)
    assert out["total_runs"] == 2
    assert out["total_captchas"] == 10
    assert out["avg_rate"] == 5.0


def test_captcha_summary_trend_improving(in_memory_db):
    """Captcha rate dropping in the second half of the window =
    'improving' trend (from operator's POV: fewer captchas = good)."""
    _seed_profile(in_memory_db, "p1")
    # First half: high captcha rate
    for d in range(-7, -4):
        _seed_run(in_memory_db, "p1", _today(d), _today(d), captchas=5)
    # Second half: low captcha rate
    for d in range(-3, 0):
        _seed_run(in_memory_db, "p1", _today(d), _today(d), captchas=0)
    out = in_memory_db.runs_captcha_summary("p1", days=14)
    assert out["trend"] == "improving"


def test_captcha_summary_trend_degrading(in_memory_db):
    """Rising captcha rate = degrading."""
    _seed_profile(in_memory_db, "p1")
    for d in range(-7, -4):
        _seed_run(in_memory_db, "p1", _today(d), _today(d), captchas=0)
    for d in range(-3, 0):
        _seed_run(in_memory_db, "p1", _today(d), _today(d), captchas=5)
    out = in_memory_db.runs_captcha_summary("p1", days=14)
    assert out["trend"] == "degrading"


def test_captcha_summary_trend_flat_for_short_window(in_memory_db):
    """Need at least 4 daily buckets for a meaningful trend; below
    that, return 'flat' regardless of values."""
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=10)
    _seed_run(in_memory_db, "p1", _today(0),  _today(0),  captchas=0)
    out = in_memory_db.runs_captcha_summary("p1", days=14)
    assert out["trend"] == "flat"


# ──────────────────────────────────────────────────────────────
# health_drift_profiles
# ──────────────────────────────────────────────────────────────

def test_drift_returns_empty_when_all_healthy(in_memory_db):
    """Stable scores + low captcha rate → no entries."""
    _seed_profile(in_memory_db, "p1")
    _seed_health(in_memory_db, "p1", 90, _today_iso(-3))
    _seed_health(in_memory_db, "p1", 92, _today_iso(-1))
    _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=0)
    out = in_memory_db.health_drift_profiles(days=7)
    assert out == []


def test_drift_flags_score_drop(in_memory_db):
    """First→last score drop ≥ min_score_drop qualifies."""
    _seed_profile(in_memory_db, "p1")
    _seed_health(in_memory_db, "p1", 90, _today_iso(-3))
    _seed_health(in_memory_db, "p1", 60, _today_iso(-1))
    out = in_memory_db.health_drift_profiles(days=7, min_score_drop=15)
    assert len(out) == 1
    assert out[0]["profile_name"] == "p1"
    assert out[0]["health_score"] == 60
    assert any("dropped" in r for r in out[0]["reasons"])


def test_drift_flags_high_captcha_rate(in_memory_db):
    """Captcha rate ≥ min_captcha_rate qualifies even without health
    data — this is the real-workload signal."""
    _seed_profile(in_memory_db, "p1")
    for _ in range(3):
        _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=2)
    out = in_memory_db.health_drift_profiles(days=7, min_captcha_rate=0.5)
    assert len(out) == 1
    assert out[0]["captcha_rate"] >= 0.5
    assert any("captcha rate" in r for r in out[0]["reasons"])


def test_drift_marks_critical_severity_low_health(in_memory_db):
    """health_score < 50 → severity=critical (not warn)."""
    _seed_profile(in_memory_db, "p1")
    _seed_health(in_memory_db, "p1", 80, _today_iso(-3))
    _seed_health(in_memory_db, "p1", 30, _today_iso(-1))
    out = in_memory_db.health_drift_profiles(days=7)
    assert out[0]["severity"] == "critical"


def test_drift_marks_critical_severity_high_captcha(in_memory_db):
    """Captcha rate ≥ 0.5 → critical even with no health data."""
    _seed_profile(in_memory_db, "p1")
    for _ in range(2):
        _seed_run(in_memory_db, "p1", _today(-1), _today(-1), captchas=1)
    out = in_memory_db.health_drift_profiles(days=7, min_captcha_rate=0.5)
    assert out[0]["severity"] == "critical"


def test_drift_orders_critical_first(in_memory_db):
    """Sort: critical first, then warn. Banner shows worst on top."""
    _seed_profile(in_memory_db, "p_warn")
    _seed_profile(in_memory_db, "p_crit")
    # warn-level: small drop, mid health
    _seed_health(in_memory_db, "p_warn", 80, _today_iso(-3))
    _seed_health(in_memory_db, "p_warn", 60, _today_iso(-1))
    # critical: bigger drop and below 50
    _seed_health(in_memory_db, "p_crit", 80, _today_iso(-3))
    _seed_health(in_memory_db, "p_crit", 30, _today_iso(-1))
    out = in_memory_db.health_drift_profiles(days=7)
    severities = [p["severity"] for p in out]
    assert severities.index("critical") < severities.index("warn")


def test_drift_excludes_old_data(in_memory_db):
    """Health rows older than ``days`` shouldn't influence drift."""
    _seed_profile(in_memory_db, "p1")
    # Old rows that would otherwise look like drift
    _seed_health(in_memory_db, "p1", 95, _today_iso(-30))
    _seed_health(in_memory_db, "p1", 30, _today_iso(-29))
    # No recent data
    out = in_memory_db.health_drift_profiles(days=7)
    assert out == []


# ──────────────────────────────────────────────────────────────
# runs_count_for_profile
# ──────────────────────────────────────────────────────────────

def test_count_excludes_inflight_by_default(in_memory_db):
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(0), _today(0))
    _seed_run(in_memory_db, "p1", _today(0), _today(0))
    _seed_run(in_memory_db, "p1", _today(0), None)
    assert in_memory_db.runs_count_for_profile("p1") == 2


def test_count_includes_inflight_when_requested(in_memory_db):
    _seed_profile(in_memory_db, "p1")
    _seed_run(in_memory_db, "p1", _today(0), _today(0))
    _seed_run(in_memory_db, "p1", _today(0), None)
    assert in_memory_db.runs_count_for_profile("p1", only_finished=False) == 2


def test_count_zero_when_no_runs(in_memory_db):
    _seed_profile(in_memory_db, "p1")
    assert in_memory_db.runs_count_for_profile("p1") == 0


def test_count_per_profile_isolation(in_memory_db):
    """One profile's runs shouldn't bleed into another's count."""
    _seed_profile(in_memory_db, "p1")
    _seed_profile(in_memory_db, "p2")
    _seed_run(in_memory_db, "p1", _today(0), _today(0))
    _seed_run(in_memory_db, "p1", _today(0), _today(0))
    _seed_run(in_memory_db, "p2", _today(0), _today(0))
    assert in_memory_db.runs_count_for_profile("p1") == 2
    assert in_memory_db.runs_count_for_profile("p2") == 1


# ──────────────────────────────────────────────────────────────
# every_n_runs canary gate (Sprint 6 logic in runner._act_*)
# ──────────────────────────────────────────────────────────────

# These tests exercise the gate logic by monkey-patching the
# orchestrator (run_canary) so we don't need an actual webdriver,
# then asserting whether it was called or skipped based on the gate.

class _FakeCtx(dict):
    """ctx is a plain dict in the real runner — keep it that way so
    we exercise the actual code path."""


@pytest.fixture
def fake_canary(monkeypatch):
    """Replace run_canary with a recording stub."""
    import ghost_shell.profile.health_canary as hc_mod
    calls = []
    def _stub(driver, sites, navigation_timeout, settle_sec):
        calls.append({
            "sites": sites,
            "timeout": navigation_timeout,
            "settle_sec": settle_sec,
        })
        return []   # nothing to persist
    monkeypatch.setattr(hc_mod, "run_canary", _stub)
    return calls


def test_canary_gate_runs_on_first_ever_run(in_memory_db, fake_canary):
    """count == 0 → gate passes regardless of every_n_runs."""
    from ghost_shell.actions.runner import _act_health_check_canary
    _seed_profile(in_memory_db, "p1")
    ctx = _FakeCtx(profile_name="p1", run_id=None)
    _act_health_check_canary(
        driver=MagicMock(), action={"every_n_runs": 5}, ctx=ctx,
    )
    assert len(fake_canary) == 1, "first-ever run should pass the gate"


def test_canary_gate_skips_off_cycle(in_memory_db, fake_canary):
    """count not divisible by every_n_runs → skip."""
    from ghost_shell.actions.runner import _act_health_check_canary
    _seed_profile(in_memory_db, "p1")
    # 4 finished runs, every_n_runs=5 → 4 % 5 = 4, not divisible → skip
    for _ in range(4):
        _seed_run(in_memory_db, "p1", _today(0), _today(0))
    ctx = _FakeCtx(profile_name="p1", run_id=None)
    _act_health_check_canary(
        driver=MagicMock(), action={"every_n_runs": 5}, ctx=ctx,
    )
    assert fake_canary == [], "off-cycle run should be skipped"


def test_canary_gate_runs_on_cycle(in_memory_db, fake_canary):
    """count divisible by every_n_runs → run."""
    from ghost_shell.actions.runner import _act_health_check_canary
    _seed_profile(in_memory_db, "p1")
    for _ in range(5):
        _seed_run(in_memory_db, "p1", _today(0), _today(0))
    ctx = _FakeCtx(profile_name="p1", run_id=None)
    _act_health_check_canary(
        driver=MagicMock(), action={"every_n_runs": 5}, ctx=ctx,
    )
    assert len(fake_canary) == 1, "5 runs + every_n_runs=5 should fire"


def test_canary_gate_disabled_when_n_zero(in_memory_db, fake_canary):
    """every_n_runs=0 = no gate. Always run."""
    from ghost_shell.actions.runner import _act_health_check_canary
    _seed_profile(in_memory_db, "p1")
    for _ in range(7):
        _seed_run(in_memory_db, "p1", _today(0), _today(0))
    ctx = _FakeCtx(profile_name="p1", run_id=None)
    _act_health_check_canary(
        driver=MagicMock(), action={"every_n_runs": 0}, ctx=ctx,
    )
    assert len(fake_canary) == 1


def test_canary_gate_falls_back_to_config(in_memory_db, fake_canary):
    """Action doesn't specify every_n_runs → reads
    scheduler.canary_every_n_runs from config."""
    from ghost_shell.actions.runner import _act_health_check_canary
    _seed_profile(in_memory_db, "p1")
    for _ in range(3):
        _seed_run(in_memory_db, "p1", _today(0), _today(0))
    in_memory_db.config_set("scheduler.canary_every_n_runs", 10)
    ctx = _FakeCtx(profile_name="p1", run_id=None)
    _act_health_check_canary(
        driver=MagicMock(), action={}, ctx=ctx,
    )
    # 3 % 10 = 3 → skip
    assert fake_canary == [], "fallback gate should skip when count < N"


def test_canary_gate_handles_missing_profile_field(in_memory_db, fake_canary):
    """ctx without 'profile_name' falls through to 'unknown' — gate
    still works because runs_count_for_profile returns 0 for unknown,
    treated as first-ever run (pass)."""
    from ghost_shell.actions.runner import _act_health_check_canary
    ctx = _FakeCtx(run_id=None)   # no profile_name
    _act_health_check_canary(
        driver=MagicMock(), action={"every_n_runs": 3}, ctx=ctx,
    )
    # count=0 → first-run pass branch → run_canary called
    assert len(fake_canary) == 1
