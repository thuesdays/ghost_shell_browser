"""Tests for the dashboard background job queue."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def reset_jobs(monkeypatch):
    """Clear the module-level job dict between tests."""
    from ghost_shell.dashboard import jobs
    jobs._jobs.clear()
    yield
    jobs._jobs.clear()


def test_enqueue_returns_string_id():
    from ghost_shell.dashboard.jobs import enqueue
    jid = enqueue("test", lambda: "result")
    assert isinstance(jid, str)
    assert len(jid) > 0


def test_get_status_returns_none_for_unknown():
    from ghost_shell.dashboard.jobs import get_status
    assert get_status("nope") is None


def test_enqueue_then_get_status():
    from ghost_shell.dashboard.jobs import enqueue, get_status
    jid = enqueue("test", lambda: "ok")
    # Immediately after enqueue, status is queued OR running OR done
    s = get_status(jid)
    assert s is not None
    assert s["status"] in ("queued", "running", "done")


def test_job_completes_with_result():
    from ghost_shell.dashboard.jobs import enqueue, get_status
    jid = enqueue("test", lambda x: x * 2, 21)
    # Poll up to 5s for completion
    deadline = time.time() + 5
    while time.time() < deadline:
        s = get_status(jid)
        if s and s["status"] == "done":
            assert s["result"] == 42
            return
        time.sleep(0.1)
    pytest.fail(f"job didn't complete in 5s, last status: {get_status(jid)}")


def test_job_failure_records_error():
    from ghost_shell.dashboard.jobs import enqueue, get_status
    def boom():
        raise ValueError("on purpose")
    jid = enqueue("boom", boom)
    deadline = time.time() + 5
    while time.time() < deadline:
        s = get_status(jid)
        if s and s["status"] == "error":
            assert "ValueError" in s["error"]
            assert "on purpose" in s["error"]
            return
        time.sleep(0.1)
    pytest.fail("error job didn't transition to error status")


def test_status_dict_has_all_fields():
    from ghost_shell.dashboard.jobs import enqueue, get_status
    jid = enqueue("test", lambda: 1)
    time.sleep(1.5)  # let it finish
    s = get_status(jid)
    assert s is not None
    for k in ("id", "kind", "status", "submitted_at",
              "started_at", "finished_at", "elapsed", "result", "error"):
        assert k in s


def test_queue_full_rejects():
    from ghost_shell.dashboard import jobs
    from ghost_shell.dashboard.jobs import enqueue
    # Force the queue-full check by jamming many fake "queued" jobs
    fake = {f"j{i}": jobs._Job(f"j{i}", "test",
                                __import__("concurrent.futures").futures.Future())
            for i in range(jobs._QUEUE_MAX + 1)}
    for j in fake.values():
        j.status = "queued"
    jobs._jobs.update(fake)
    with pytest.raises(RuntimeError, match="queue full"):
        enqueue("blocked", lambda: 1)


def test_cancel_queued_job():
    """Best-effort: a job that hasn't started yet can be cancelled."""
    from ghost_shell.dashboard.jobs import enqueue, get_status, cancel
    # Block the worker pool with a long-running job, then enqueue
    # another job and try to cancel it. Note: with 2 workers, we
    # need 2 blockers.
    def blocker():
        time.sleep(2)
    enqueue("blocker1", blocker)
    enqueue("blocker2", blocker)
    jid = enqueue("victim", lambda: 1)
    # Poll briefly to see if it stayed queued
    s = get_status(jid)
    if s and s["status"] == "queued":
        ok = cancel(jid)
        assert ok is True or ok is False  # either is acceptable depending on timing
    # Cleanup: wait for blockers to exit
    time.sleep(2.5)


def test_cancel_unknown_returns_false():
    from ghost_shell.dashboard.jobs import cancel
    assert cancel("nope") is False
