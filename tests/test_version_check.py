"""Tests for the Chrome/chromedriver version check module.

Doesn't actually run any binary — mocks subprocess.run so the version
strings can be controlled. Tests the regex parser, the cache layer,
and the verdict logic."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    """Each test starts with a clean cache so previous tests' fakes
    don't leak verdicts."""
    from ghost_shell.core import version_check as vc
    vc.invalidate_cache()
    yield
    vc.invalidate_cache()


# ── _parse_version_string ──────────────────────────────────

def test_parse_canonical_version():
    from ghost_shell.core.version_check import _parse_version_string
    assert _parse_version_string("149.0.7805.0") == "149.0.7805.0"


def test_parse_chromedriver_version_with_label():
    from ghost_shell.core.version_check import _parse_version_string
    s = "ChromeDriver 149.0.7805.0 (abc123) refs/branch-heads/149"
    assert _parse_version_string(s) == "149.0.7805.0"


def test_parse_chrome_product_version_with_trailing_newline():
    from ghost_shell.core.version_check import _parse_version_string
    assert _parse_version_string("149.0.7805.0\n") == "149.0.7805.0"


def test_parse_no_match_returns_none():
    from ghost_shell.core.version_check import _parse_version_string
    assert _parse_version_string("no version here") is None
    assert _parse_version_string("") is None
    assert _parse_version_string(None) is None


def test_parse_partial_version_no_match():
    """3-part versions don't match — we only accept canonical 4-part."""
    from ghost_shell.core.version_check import _parse_version_string
    assert _parse_version_string("149.0.7805") is None


def test_major_of_extracts_first_part():
    from ghost_shell.core.version_check import _major_of
    assert _major_of("149.0.7805.0") == 149
    assert _major_of("88.0.4324.190") == 88
    assert _major_of(None) is None
    assert _major_of("") is None
    assert _major_of("garbage") is None


# ── check_compatibility verdict ────────────────────────────

def test_compat_no_chrome_binary_warns(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: None)
    monkeypatch.setattr(vc, "find_chromedriver", lambda: None)
    v = vc.check_compatibility(use_cache=False)
    assert v["ok"] is False
    assert v["level"] == "warn"
    assert "not found" in v["reason"].lower()


def test_compat_no_chromedriver_critical(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/path/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: None)
    monkeypatch.setattr(vc, "_cached_probe", lambda *a, **kw: "149.0.0.0")
    v = vc.check_compatibility(use_cache=False)
    assert v["ok"] is False
    assert v["level"] == "critical"


def test_compat_matching_majors_ok(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/c/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: "/c/chromedriver.exe")
    # Two probes, both 149.x
    monkeypatch.setattr(vc, "get_chrome_version",
                        lambda p=None: "149.0.7805.0")
    monkeypatch.setattr(vc, "get_chromedriver_version",
                        lambda p=None: "149.0.7805.5")
    v = vc.check_compatibility(use_cache=False)
    assert v["ok"] is True
    assert v["level"] == "ok"
    assert v["chrome_major"] == 149
    assert v["driver_major"] == 149


def test_compat_mismatched_majors_critical(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/c/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: "/c/chromedriver.exe")
    monkeypatch.setattr(vc, "get_chrome_version", lambda p=None: "149.0.0.0")
    monkeypatch.setattr(vc, "get_chromedriver_version", lambda p=None: "150.0.0.0")
    v = vc.check_compatibility(use_cache=False)
    assert v["ok"] is False
    assert v["level"] == "critical"
    assert "mismatch" in v["reason"].lower()


def test_compat_unparseable_version_warn(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/c/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: "/c/chromedriver.exe")
    monkeypatch.setattr(vc, "get_chrome_version", lambda p=None: None)
    monkeypatch.setattr(vc, "get_chromedriver_version", lambda p=None: None)
    v = vc.check_compatibility(use_cache=False)
    assert v["level"] == "warn"


# ── cache behaviour ────────────────────────────────────────

def test_invalidate_cache_drops_verdict(monkeypatch):
    from ghost_shell.core import version_check as vc
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/c/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: "/c/chromedriver.exe")
    monkeypatch.setattr(vc, "get_chrome_version", lambda p=None: "149.0.0.0")
    monkeypatch.setattr(vc, "get_chromedriver_version", lambda p=None: "149.0.0.0")
    v1 = vc.check_compatibility(use_cache=False)
    assert v1["ok"]
    vc.invalidate_cache()
    # After invalidate, cache is empty — next call re-probes
    monkeypatch.setattr(vc, "get_chrome_version", lambda p=None: "150.0.0.0")
    v2 = vc.check_compatibility(use_cache=True)
    # Cache was cleared; new probe shows mismatch
    assert v2["ok"] is False


def test_check_compatibility_uses_cache_within_60s(monkeypatch):
    from ghost_shell.core import version_check as vc
    call_count = [0]
    def counting_probe(p=None):
        call_count[0] += 1
        return "149.0.0.0"
    monkeypatch.setattr(vc, "find_chrome_binary", lambda: "/c/chrome.exe")
    monkeypatch.setattr(vc, "find_chromedriver", lambda: "/c/chromedriver.exe")
    monkeypatch.setattr(vc, "get_chrome_version", counting_probe)
    monkeypatch.setattr(vc, "get_chromedriver_version", lambda p=None: "149.0.0.0")
    vc.check_compatibility(use_cache=True)
    initial = call_count[0]
    vc.check_compatibility(use_cache=True)  # should hit cache
    assert call_count[0] == initial   # no second probe


# ── Sprint 8 fallback tests: when --product-version returns nothing,
#    we should still find a version via the sibling-dir scan (works
#    even when the patched Chrome strips CLI flags).

def test_chrome_version_falls_back_to_dir_scan(tmp_path, monkeypatch):
    """Mock _run_version_probe to return None (binary refuses to print
    a version) and place a versioned sibling dir next to the binary.
    get_chrome_version should still return the right string."""
    from ghost_shell.core import version_check as vc
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    (tmp_path / "149.0.7805.0").mkdir()
    # Throw a noise dir + a file in for realism
    (tmp_path / "Locales").mkdir()
    (tmp_path / "snapshot_blob.bin").write_bytes(b"")
    monkeypatch.setattr(vc, "_run_version_probe", lambda *a, **k: None)
    # Disable PE fallback so we know dir scan is what answered.
    monkeypatch.setattr(vc, "_chrome_version_from_pe", lambda *a, **k: None)
    assert vc.get_chrome_version(str(chrome)) == "149.0.7805.0"


def test_chrome_version_dir_scan_picks_newest():
    """If two versioned dirs sit next to the binary (leftover from an
    upgrade), pick the lexically-greatest (which equals newest for
    Chromium's four-part scheme)."""
    from ghost_shell.core.version_check import _chrome_version_from_dir
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        chrome = os.path.join(tmp, "chrome.exe")
        open(chrome, "wb").close()
        os.makedirs(os.path.join(tmp, "148.0.7000.0"))
        os.makedirs(os.path.join(tmp, "149.0.7805.0"))
        assert _chrome_version_from_dir(chrome) == "149.0.7805.0"


def test_chrome_version_dir_scan_returns_none_when_no_match(tmp_path):
    """No versioned subdirs → None, caller falls through to PE."""
    from ghost_shell.core.version_check import _chrome_version_from_dir
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    (tmp_path / "Locales").mkdir()
    assert _chrome_version_from_dir(str(chrome)) is None


def test_chrome_version_get_uses_flag_probe_when_available(monkeypatch, tmp_path):
    """When --product-version works, we should NOT fall through to
    the dir scan / PE read."""
    from ghost_shell.core import version_check as vc
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"x")
    (tmp_path / "999.0.0.0").mkdir()  # this would mislead dir scan
    vc.invalidate_cache()
    monkeypatch.setattr(vc, "_run_version_probe",
                        lambda p, args, **k: "149.0.7805.0")
    # If dir scan ran, we'd get 999.0.0.0 — assert we got the flag value.
    assert vc.get_chrome_version(str(chrome)) == "149.0.7805.0"
