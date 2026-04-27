"""Unit tests for the .ghost_shell.lock helpers in browser/runtime.py.

These cover the JSON-format lock used to detect concurrent runs and
heartbeat-stale (hung) processes. Critical for cross-process safety —
a regression here = "kill legitimate run" or "let two runs corrupt
each other's user-data-dir".

Mocks:
  * psutil.pid_exists                 (controllable)
  * pid_looks_like_ghost_shell       (controllable)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def lock_helpers():
    """Import the helpers fresh per-test to avoid module-level cache
    leaks across tests."""
    from ghost_shell.browser import runtime
    return runtime


def _write_legacy_pid_lock(path: Path, pid: int):
    path.write_text(str(pid), encoding="utf-8")


def _write_json_lock(path: Path, pid: int, hb_offset_sec: int = 0):
    """Write a JSON lock with heartbeat at NOW + offset (negative = stale)."""
    hb = (datetime.now() + timedelta(seconds=hb_offset_sec)).isoformat(timespec="seconds")
    path.write_text(json.dumps({
        "pid": pid,
        "acquired_at": hb,
        "heartbeat_at": hb,
    }), encoding="utf-8")


# ── _read_gs_lock ─────────────────────────────────────────────

def test_read_lock_missing_file(lock_helpers, tmp_path):
    assert lock_helpers._read_gs_lock(str(tmp_path / "nope.lock")) == {}


def test_read_lock_empty_file(lock_helpers, tmp_path):
    p = tmp_path / "empty.lock"
    p.write_text("")
    assert lock_helpers._read_gs_lock(str(p)) == {}


def test_read_lock_legacy_pid_format(lock_helpers, tmp_path):
    p = tmp_path / "legacy.lock"
    _write_legacy_pid_lock(p, 12345)
    data = lock_helpers._read_gs_lock(str(p))
    assert data["pid"] == 12345
    assert data["acquired_at"] is None
    assert data["heartbeat_at"] is None


def test_read_lock_json_format(lock_helpers, tmp_path):
    p = tmp_path / "json.lock"
    _write_json_lock(p, 9999)
    data = lock_helpers._read_gs_lock(str(p))
    assert data["pid"] == 9999
    assert data["acquired_at"] is not None
    assert data["heartbeat_at"] is not None


def test_read_lock_corrupt_json_returns_empty(lock_helpers, tmp_path):
    p = tmp_path / "corrupt.lock"
    p.write_text("{not valid json")
    assert lock_helpers._read_gs_lock(str(p)) == {}


def test_read_lock_zero_pid_returns_empty(lock_helpers, tmp_path):
    p = tmp_path / "zero.lock"
    p.write_text(json.dumps({"pid": 0}))
    assert lock_helpers._read_gs_lock(str(p)) == {}


def test_read_lock_negative_pid_returns_empty(lock_helpers, tmp_path):
    p = tmp_path / "neg.lock"
    p.write_text(json.dumps({"pid": -1}))
    assert lock_helpers._read_gs_lock(str(p)) == {}


# ── _write_gs_lock ────────────────────────────────────────────

def test_write_lock_creates_json(lock_helpers, tmp_path):
    p = tmp_path / "new.lock"
    ok = lock_helpers._write_gs_lock(str(p))
    assert ok is True
    data = json.loads(p.read_text())
    assert data["pid"] == os.getpid()
    assert data["acquired_at"]
    assert data["heartbeat_at"] == data["acquired_at"]


def test_write_lock_overwrites_existing(lock_helpers, tmp_path):
    p = tmp_path / "over.lock"
    p.write_text(json.dumps({"pid": 99999}))
    lock_helpers._write_gs_lock(str(p))
    data = json.loads(p.read_text())
    assert data["pid"] == os.getpid()


# ── _heartbeat_age_sec ────────────────────────────────────────

def test_heartbeat_age_uses_json_field_when_present(lock_helpers, tmp_path):
    p = tmp_path / "fresh.lock"
    _write_json_lock(p, 1, hb_offset_sec=-30)  # 30s ago
    data = lock_helpers._read_gs_lock(str(p))
    age = lock_helpers._heartbeat_age_sec(data, str(p))
    assert age is not None
    assert 28 <= age <= 32   # allow some slack


def test_heartbeat_age_falls_back_to_mtime_for_legacy_lock(lock_helpers, tmp_path):
    p = tmp_path / "legacy.lock"
    _write_legacy_pid_lock(p, 1)
    # Touch back to 60s ago
    past = time.time() - 60
    os.utime(p, (past, past))
    data = lock_helpers._read_gs_lock(str(p))
    age = lock_helpers._heartbeat_age_sec(data, str(p))
    assert age is not None
    assert 58 <= age <= 62


def test_heartbeat_age_returns_none_for_missing_path(lock_helpers, tmp_path):
    nope = str(tmp_path / "nope.lock")
    age = lock_helpers._heartbeat_age_sec({}, nope)
    assert age is None


# ── _heartbeat_gs_lock ────────────────────────────────────────

def test_heartbeat_refreshes_existing_lock(lock_helpers, tmp_path):
    p = tmp_path / "hb.lock"
    lock_helpers._write_gs_lock(str(p))
    initial = json.loads(p.read_text())["heartbeat_at"]
    time.sleep(1.05)  # ensure ISO seconds-precision diff
    ok = lock_helpers._heartbeat_gs_lock(str(p))
    assert ok is True
    refreshed = json.loads(p.read_text())["heartbeat_at"]
    assert refreshed != initial
    # acquired_at preserved
    assert json.loads(p.read_text())["acquired_at"] == json.loads(p.read_text())["acquired_at"]


def test_heartbeat_refuses_to_refresh_if_pid_doesnt_match(lock_helpers, tmp_path):
    p = tmp_path / "other.lock"
    _write_json_lock(p, pid=99999)  # someone else's lock
    ok = lock_helpers._heartbeat_gs_lock(str(p))
    assert ok is False


def test_heartbeat_returns_false_for_missing_lock(lock_helpers, tmp_path):
    ok = lock_helpers._heartbeat_gs_lock(str(tmp_path / "missing.lock"))
    assert ok is False


# ── _is_lock_live ─────────────────────────────────────────────
# These cover the live-vs-stale decision the active-run guard uses.

def test_is_lock_live_zero_pid_is_dead(lock_helpers):
    assert lock_helpers._is_lock_live({"pid": 0}, "/tmp/x") is False
    assert lock_helpers._is_lock_live({}, "/tmp/x") is False


def test_is_lock_live_dead_pid(lock_helpers, tmp_path):
    p = tmp_path / "dead.lock"
    _write_json_lock(p, pid=99998, hb_offset_sec=-10)
    data = lock_helpers._read_gs_lock(str(p))
    with patch.object(lock_helpers, "_heartbeat_age_sec", return_value=10):
        # pid_exists False → dead
        with patch("psutil.pid_exists", return_value=False):
            assert lock_helpers._is_lock_live(data, str(p)) is False


def test_is_lock_live_alive_with_fresh_heartbeat(lock_helpers, tmp_path):
    p = tmp_path / "live.lock"
    _write_json_lock(p, pid=99997, hb_offset_sec=-30)
    data = lock_helpers._read_gs_lock(str(p))
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        assert lock_helpers._is_lock_live(data, str(p)) is True


def test_is_lock_live_alive_pid_but_stale_heartbeat(lock_helpers, tmp_path):
    """The RC-33 fix: hung process (pid alive, no heartbeat in >180s)
    should be classified DEAD so we can break the lock."""
    p = tmp_path / "hung.lock"
    _write_json_lock(p, pid=99996, hb_offset_sec=-300)  # 5min ago
    data = lock_helpers._read_gs_lock(str(p))
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=True):
        assert lock_helpers._is_lock_live(data, str(p)) is False


def test_is_lock_live_pid_not_ghost_shell(lock_helpers, tmp_path):
    """PID got recycled to unrelated process — not ours, treat as dead."""
    p = tmp_path / "recycled.lock"
    _write_json_lock(p, pid=99995, hb_offset_sec=-30)
    data = lock_helpers._read_gs_lock(str(p))
    with patch("psutil.pid_exists", return_value=True), \
         patch("ghost_shell.core.process_reaper.pid_looks_like_ghost_shell",
               return_value=False):
        assert lock_helpers._is_lock_live(data, str(p)) is False
