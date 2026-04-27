"""Sprint 11.1 — tests for the four C++ stealth-patch validator
checks (canvas active / canvas valid / webrtc host filtered / font
budget active).

Each check is a pure function over the fingerprint dict — no browser
launch needed. We feed in synthetic fingerprint payloads that mirror
what selftest.py PROBE_SCRIPT collects, and assert the returned
(status, detail) tuple.

The contract under test:
* Probe field absent  → returns ``None``  → validator marks ``skip``
* Probe field present + patch firing       → returns ``("pass", ...)``
* Probe field present + patch off / broken → returns ``("fail", ...)``
* Edge cases (low font count, total candidates)  → returns ``("warn", ...)``
  where the validator's domain-stats accumulator can decide the
  surface action.

Skip semantics matter here because pre-Sprint-11 builds without the
patches still go through the validator — they shouldn't see false
"fail" entries for features that don't exist on their build.
"""

from __future__ import annotations

import pytest


# ─── canvas patch active (deterministic across renders) ──────────

def test_canvas_active_pass_when_hashes_match():
    from ghost_shell.fingerprint.validator import check_cpp_canvas_active
    fp = {"cpp_canvas": {"hash_a": "abc123", "hash_b": "abc123",
                          "data_url": "////////"}}
    status, detail = check_cpp_canvas_active(fp, {})
    assert status == "pass"
    assert "stable" in detail


def test_canvas_active_fail_when_hashes_differ():
    from ghost_shell.fingerprint.validator import check_cpp_canvas_active
    fp = {"cpp_canvas": {"hash_a": "abc123", "hash_b": "def456",
                          "data_url": "////////"}}
    status, detail = check_cpp_canvas_active(fp, {})
    assert status == "fail"
    assert "non-deterministic" in detail


def test_canvas_active_skip_when_field_absent():
    """Stock chromium without the patch produces no cpp_canvas key.
    The check must return None so the validator marks 'skip', not
    fail. Otherwise every legacy run gets penalised."""
    from ghost_shell.fingerprint.validator import check_cpp_canvas_active
    assert check_cpp_canvas_active({}, {}) is None
    assert check_cpp_canvas_active({"cpp_canvas": {}}, {}) is None
    assert check_cpp_canvas_active({"cpp_canvas": {"hash_a": "x"}}, {}) is None


# ─── canvas render valid (sanity check) ──────────────────────────

def test_canvas_render_valid_pass():
    from ghost_shell.fingerprint.validator import check_cpp_canvas_noise_distinct
    fp = {"cpp_canvas": {"data_url": "iVBORw0KGgoAAAANSUhEUgAAAAQ="}}
    status, _ = check_cpp_canvas_noise_distinct(fp, {})
    assert status == "pass"


def test_canvas_render_fail_when_blank():
    """A canvas that rendered as all-zero pixels base64-encodes to a
    long run of A's (since '0' bytes → 'AAA' in base64). Detect the
    silent-failure mode."""
    from ghost_shell.fingerprint.validator import check_cpp_canvas_noise_distinct
    fp = {"cpp_canvas": {"data_url": "AAAAAAAAAAAAAAAA=="}}
    status, detail = check_cpp_canvas_noise_distinct(fp, {})
    assert status == "fail"
    assert "blank" in detail


def test_canvas_render_fail_when_truncated():
    from ghost_shell.fingerprint.validator import check_cpp_canvas_noise_distinct
    fp = {"cpp_canvas": {"data_url": "abc"}}  # < 16 chars
    status, detail = check_cpp_canvas_noise_distinct(fp, {})
    assert status == "fail"
    assert "truncated" in detail


def test_canvas_render_skip_when_absent():
    from ghost_shell.fingerprint.validator import check_cpp_canvas_noise_distinct
    assert check_cpp_canvas_noise_distinct({}, {}) is None


# ─── WebRTC host candidates filtered ─────────────────────────────

def test_webrtc_pass_when_host_count_zero():
    from ghost_shell.fingerprint.validator import check_cpp_webrtc_no_host_candidates
    fp = {"cpp_webrtc": {"host": 0, "srflx": 1, "relay": 0,
                          "prflx": 0, "total": 1}}
    status, _ = check_cpp_webrtc_no_host_candidates(fp, {})
    assert status == "pass"


def test_webrtc_pass_when_no_candidates_at_all():
    """No STUN configured + filter active = total 0 = clean pass.
    The contract is 'no host leakage', not 'WebRTC is healthy'."""
    from ghost_shell.fingerprint.validator import check_cpp_webrtc_no_host_candidates
    fp = {"cpp_webrtc": {"host": 0, "srflx": 0, "relay": 0,
                          "prflx": 0, "total": 0}}
    status, _ = check_cpp_webrtc_no_host_candidates(fp, {})
    assert status == "pass"


def test_webrtc_fail_when_host_count_nonzero():
    """host > 0 means a LAN IP made it to the renderer — the entire
    point of the patch was preventing this. Critical fail."""
    from ghost_shell.fingerprint.validator import check_cpp_webrtc_no_host_candidates
    fp = {"cpp_webrtc": {"host": 1, "srflx": 1, "relay": 0,
                          "prflx": 0, "total": 2}}
    status, detail = check_cpp_webrtc_no_host_candidates(fp, {})
    assert status == "fail"
    assert "host-scope" in detail
    assert "1" in detail


def test_webrtc_skip_when_field_absent():
    from ghost_shell.fingerprint.validator import check_cpp_webrtc_no_host_candidates
    assert check_cpp_webrtc_no_host_candidates({}, {}) is None
    # Partial fields also skip (host but no total = uncertain)
    assert check_cpp_webrtc_no_host_candidates({"cpp_webrtc": {"host": 0}},
                                               {}) is None


# ─── Font budget active ──────────────────────────────────────────

def test_font_budget_pass_when_subset_visible():
    """50% keep-percent → ~50% visible. Anything < 95% counts as the
    filter being active (broad threshold avoids false fails on hosts
    with naturally varied font installs)."""
    from ghost_shell.fingerprint.validator import check_cpp_font_budget_active
    fp = {"cpp_fonts": {"probed": 70, "visible_count": 38,
                          "visible_names": ["Arial", "Calibri"]}}
    status, detail = check_cpp_font_budget_active(fp, {})
    assert status == "pass"
    assert "filtered" in detail
    assert "32/70" in detail        # 70 - 38 = 32 removed


def test_font_budget_fail_when_no_filter():
    """visible == probed → patch off. 100% visible is the smoking-gun
    that --gs-font-budget-seed wasn't honoured."""
    from ghost_shell.fingerprint.validator import check_cpp_font_budget_active
    fp = {"cpp_fonts": {"probed": 70, "visible_count": 70,
                          "visible_names": []}}
    status, detail = check_cpp_font_budget_active(fp, {})
    assert status == "fail"
    assert "appears inactive" in detail


def test_font_budget_warn_when_host_has_few_fonts():
    """A Linux profile with only 4 fonts installed will legitimately
    show visible == probed even with the patch active — there's just
    nothing to filter. Warn instead of fail so the operator isn't
    misled by an irrelevant signal on a sparse host."""
    from ghost_shell.fingerprint.validator import check_cpp_font_budget_active
    fp = {"cpp_fonts": {"probed": 70, "visible_count": 4}}
    status, detail = check_cpp_font_budget_active(fp, {})
    assert status == "warn"
    assert "too low for signal" in detail


def test_font_budget_skip_when_field_absent():
    from ghost_shell.fingerprint.validator import check_cpp_font_budget_active
    assert check_cpp_font_budget_active({}, {}) is None
    assert check_cpp_font_budget_active({"cpp_fonts": {}}, {}) is None
    assert check_cpp_font_budget_active({"cpp_fonts": {"probed": 0,
                                                         "visible_count": 0}},
                                         {}) is None


# ─── Integration: validator() produces the new entries ───────────

def test_validate_emits_4_new_entries():
    """End-to-end: feed a fp + template into validate() and confirm
    the report includes all 4 new checks (4 expected names exactly).
    Catches silent registration drift in the CHECKS list."""
    from ghost_shell.fingerprint.validator import validate
    fp = {
        "cpp_canvas": {"hash_a": "x", "hash_b": "x",
                       "data_url": "iVBORw0KGgoxxxxxxx"},
        "cpp_webrtc": {"host": 0, "srflx": 1, "relay": 0,
                       "prflx": 0, "total": 1},
        "cpp_fonts":  {"probed": 70, "visible_count": 35,
                       "visible_names": []},
    }
    template = {}
    report = validate(fp, template)
    names = {c["name"] for c in report.get("checks", []) or report.get("results", [])}
    expected = {
        "Canvas patch active",
        "Canvas render valid",
        "WebRTC host filtered",
        "Font budget active",
    }
    assert expected.issubset(names), \
        f"missing checks: {expected - names}"


def test_validate_skips_new_entries_on_legacy_fp():
    """A pre-patch fp dict (no cpp_* fields) must produce 'skip'
    statuses for the 4 new checks, NOT 'fail'. This is the
    forward-compat invariant for users who haven't built the
    patched chrome.exe yet."""
    from ghost_shell.fingerprint.validator import validate
    fp = {"navigator": {"webdriver": False, "userAgent": "x"}}
    report = validate(fp, {})
    cpp_results = [
        c for c in (report.get("checks") or report.get("results") or [])
        if c["name"].startswith(("Canvas patch", "Canvas render",
                                 "WebRTC host", "Font budget"))
    ]
    assert len(cpp_results) == 4
    for r in cpp_results:
        assert r["status"] == "skip", \
            f"{r['name']}: expected skip on legacy fp, got {r['status']}"
