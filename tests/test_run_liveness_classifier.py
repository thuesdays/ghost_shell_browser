"""Tests for classify_run_liveness — Sprint 3.1 shared helper.

Replaces the duplicated heartbeat-age + PID-check logic that used to
live separately in reap_stale_runs and is_profile_actually_running.
The helper is pure (no DB / no kill) — easy to test exhaustively."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def _row(pid=None, hb_offset_sec=None):
    """Build a runs-row dict. hb_offset_sec=None means no heartbeat
    field at all (mid-creation state)."""
    row = {"id": 1, "profile_name": "p1", "pid": pid}
    if hb_offset_sec is not None:
        row["heartbeat_at"] = (
            datetime.now() + timedelta(seconds=hb_offset_sec)
        ).isoformat(timespec="seconds")
    return row


# ── Status: alive ─────────────────────────────────────────────

def test_classify_alive_fresh_heartbeat_alive_pid():
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        status, reason = classify_run_liveness(_row(pid=12345, hb_offset_sec=-30))
        assert status == "alive"
        assert "fresh" in reason


def test_classify_alive_no_pid_yet_fresh_heartbeat():
    """DB row created mid-spawn — heartbeat just landed, PID not yet."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    status, reason = classify_run_liveness(_row(pid=None, hb_offset_sec=-2))
    assert status == "alive"
    assert "no pid" in reason.lower()


# ── Status: dead ──────────────────────────────────────────────

def test_classify_dead_no_pid_no_heartbeat():
    from ghost_shell.core.process_reaper import classify_run_liveness
    status, reason = classify_run_liveness(_row(pid=None, hb_offset_sec=None))
    assert status == "dead"
    assert "no pid, no recent heartbeat" in reason


def test_classify_dead_no_pid_stale_heartbeat():
    from ghost_shell.core.process_reaper import classify_run_liveness
    status, reason = classify_run_liveness(_row(pid=0, hb_offset_sec=-500))
    assert status == "dead"


def test_classify_dead_pid_does_not_exist():
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=False):
        status, reason = classify_run_liveness(_row(pid=99999, hb_offset_sec=-30))
        assert status == "dead"
        assert "no longer exists" in reason


def test_classify_dead_pid_recycled_to_unrelated_process():
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=False):
        status, reason = classify_run_liveness(_row(pid=12345, hb_offset_sec=-30))
        assert status == "dead"
        assert "recycled" in reason


def test_classify_dead_no_psutil_stale_heartbeat(monkeypatch):
    from ghost_shell.core import process_reaper as pr
    monkeypatch.setattr(pr, "HAVE_PSUTIL", False)
    status, reason = pr.classify_run_liveness(_row(pid=12345, hb_offset_sec=-500))
    assert status == "dead"


def test_classify_dead_unparseable_heartbeat():
    """A corrupt heartbeat_at value (e.g. partially-written ISO string)
    should be treated as stale, not crash."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    bad_row = {"id": 1, "pid": 12345, "heartbeat_at": "not-iso"}
    with patch("psutil.pid_exists", return_value=False):
        status, reason = classify_run_liveness(bad_row)
        assert status == "dead"


# ── Status: wedged (PID alive but heartbeat stale) ────────────

def test_classify_wedged_alive_pid_stale_heartbeat():
    """RC-33-class scenario: monitor process alive but stuck in an
    infinite loop or deadlock. PID is ours but heartbeat hasn't
    been refreshed in >180s."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        status, reason = classify_run_liveness(_row(pid=12345, hb_offset_sec=-500))
        assert status == "wedged"
        assert "alive but heartbeat" in reason


def test_classify_wedged_no_heartbeat_at_all_alive_pid():
    """Edge case: heartbeat field missing entirely + alive PID.
    Treat as wedged (something ran but never wrote a heartbeat)."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        status, reason = classify_run_liveness(_row(pid=12345, hb_offset_sec=None))
        assert status == "wedged"


# ── Threshold parameter ───────────────────────────────────────

def test_classify_respects_custom_stale_threshold():
    """Caller can tighten / loosen the staleness window."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        # 100s old, default threshold 180s → alive
        s1, _ = classify_run_liveness(_row(pid=1, hb_offset_sec=-100))
        assert s1 == "alive"
        # Same row, threshold=60s → wedged
        s2, _ = classify_run_liveness(_row(pid=1, hb_offset_sec=-100),
                                       stale_heartbeat_sec=60)
        assert s2 == "wedged"


def test_classify_now_parameter_for_deterministic_tests():
    """When ``now`` is passed explicitly, the classifier doesn't read
    the wall clock — useful for fixture-based tests with frozen time."""
    from ghost_shell.core.process_reaper import classify_run_liveness
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        # Row claims heartbeat from "ten minutes ago" relative to a
        # forced-NOW. This is wedged regardless of actual wall clock.
        forced_now = datetime(2026, 1, 1, 12, 0, 0)
        ten_min_ago = (forced_now - timedelta(minutes=10)).isoformat(timespec="seconds")
        row = {"id": 1, "pid": 12345, "heartbeat_at": ten_min_ago}
        status, _ = classify_run_liveness(row, now=forced_now)
        assert status == "wedged"
