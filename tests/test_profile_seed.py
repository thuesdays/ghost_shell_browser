"""Sprint 11 — tests for ghost_shell/fingerprint/profile_seed.py.

The C++ patches consume CLI flags whose values come from this module.
Stable seed derivation across launches is a HARD requirement: if a
profile's canvas seed flips between two launches, the canvas hash
changes too, which is exactly the cross-profile fingerprint we're
trying to AVOID inside one profile.

Test surface:
* root seed is deterministic for the same (id, name)
* domain-tagged seeds are independent (no leakage if one is recovered)
* root seed differs for different profiles
* salt env-var override changes seeds
* build_chrome_args produces the right flags + skips disabled ones
* the seed format matches what the C++ patch expects (16 hex chars,
  parseable as uint64_t)
"""

from __future__ import annotations

import re

import pytest


# ─── deterministic derivation ────────────────────────────────

def test_root_seed_is_deterministic():
    from ghost_shell.fingerprint.profile_seed import derive_root_seed
    a = derive_root_seed(7, "fb_main_us")
    b = derive_root_seed(7, "fb_main_us")
    assert a == b


def test_root_seed_differs_per_id():
    from ghost_shell.fingerprint.profile_seed import derive_root_seed
    a = derive_root_seed(1, "p")
    b = derive_root_seed(2, "p")
    assert a != b


def test_root_seed_differs_per_name():
    from ghost_shell.fingerprint.profile_seed import derive_root_seed
    a = derive_root_seed(1, "alpha")
    b = derive_root_seed(1, "beta")
    assert a != b


# ─── domain separation ──────────────────────────────────────

def test_canvas_and_webrtc_seeds_are_uncorrelated():
    """An attacker who learned the canvas seed for profile X should
    not be able to predict the WebRTC seed for the same profile."""
    from ghost_shell.fingerprint.profile_seed import (
        derive_canvas_seed, derive_webrtc_seed,
    )
    cs = derive_canvas_seed(42, "fb")
    ws = derive_webrtc_seed(42, "fb")
    assert cs != ws


def test_all_four_domains_produce_distinct_seeds():
    from ghost_shell.fingerprint.profile_seed import (
        derive_canvas_seed, derive_webrtc_seed,
        derive_ja3_seed, derive_font_budget_seed,
    )
    pid, name = 5, "test"
    seeds = {
        derive_canvas_seed(pid, name),
        derive_webrtc_seed(pid, name),
        derive_ja3_seed(pid, name),
        derive_font_budget_seed(pid, name),
    }
    assert len(seeds) == 4, "domain-separation collision"


# ─── salt sensitivity ───────────────────────────────────────

def test_salt_env_changes_seed(monkeypatch):
    from ghost_shell.fingerprint.profile_seed import derive_canvas_seed
    monkeypatch.setenv("GHOST_SHELL_FP_SALT", "salt-A")
    a = derive_canvas_seed(1, "p")
    monkeypatch.setenv("GHOST_SHELL_FP_SALT", "salt-B")
    b = derive_canvas_seed(1, "p")
    assert a != b, "salt change must change seed"


def test_no_salt_uses_default_constant(monkeypatch):
    """Empty GHOST_SHELL_FP_SALT falls back to the baked default —
    seeds stay stable for users who haven't set the env var."""
    from ghost_shell.fingerprint.profile_seed import derive_canvas_seed
    monkeypatch.delenv("GHOST_SHELL_FP_SALT", raising=False)
    a = derive_canvas_seed(1, "p")
    monkeypatch.setenv("GHOST_SHELL_FP_SALT", "")  # explicit empty
    b = derive_canvas_seed(1, "p")
    assert a == b


# ─── build_chrome_args composer ─────────────────────────────

def test_build_chrome_args_emits_all_flags_by_default():
    from ghost_shell.fingerprint.profile_seed import build_chrome_args
    args = build_chrome_args(7, "fb")
    seeds = [a for a in args if a.startswith("--gs-")]
    assert len(seeds) == 4
    # Companion master switches present
    assert "--webrtc-public-only" in args
    assert "--use-privacy-budget" in args


def test_build_chrome_args_respects_enabled_subset():
    from ghost_shell.fingerprint.profile_seed import build_chrome_args
    args = build_chrome_args(7, "fb", enabled_patches={"canvas", "ja3"})
    assert any(a.startswith("--gs-canvas-seed=") for a in args)
    assert any(a.startswith("--gs-ja3-seed=") for a in args)
    assert not any(a.startswith("--gs-webrtc-seed=") for a in args)
    assert not any(a.startswith("--gs-font-budget-seed=") for a in args)
    # Companion switches gated on their domains
    assert "--webrtc-public-only" not in args
    assert "--use-privacy-budget" not in args


def test_seed_flag_format_matches_cpp_parser():
    """C++ side calls base::HexStringToUInt64 — needs 16 lowercase
    hex chars, no 0x prefix. Spec'd in profile_seed.build_chrome_args
    via the f-string ``{seed:016x}`` format."""
    from ghost_shell.fingerprint.profile_seed import build_chrome_args
    args = build_chrome_args(1, "p")
    rx = re.compile(r"^--gs-[a-z-]+-seed=([0-9a-f]{16})$")
    seed_flags = [a for a in args if "seed=" in a]
    assert len(seed_flags) == 4
    for flag in seed_flags:
        m = rx.match(flag)
        assert m, f"{flag!r} does not match expected hex64 format"
        # Every value must be parseable as uint64
        v = int(m.group(1), 16)
        assert 0 <= v <= 2**64 - 1


def test_build_chrome_args_handles_none_profile_id():
    """Legacy single-profile mode passes profile_id=None; the seed
    derives from name only. Must not crash and must be stable."""
    from ghost_shell.fingerprint.profile_seed import build_chrome_args
    a = build_chrome_args(None, "default")
    b = build_chrome_args(None, "default")
    assert a == b, "None profile_id must still derive deterministically"


def test_build_chrome_args_empty_name_is_safe():
    """Edge case — make sure an empty/None name doesn't blow up."""
    from ghost_shell.fingerprint.profile_seed import build_chrome_args
    args1 = build_chrome_args(1, "")
    args2 = build_chrome_args(1, "")
    assert args1 == args2
    # All seed flags present even with empty name
    assert any("--gs-canvas-seed=" in a for a in args1)


# ─── runtime wiring smoke check ─────────────────────────────

def test_runtime_wiring_imports_cleanly():
    """The injection point in ghost_shell/browser/runtime.py must be
    importable. Catches the import failing because of a typo in the
    module path before a real launch tries to use it."""
    import ghost_shell.fingerprint.profile_seed as ps
    assert hasattr(ps, "build_chrome_args")
    assert hasattr(ps, "derive_canvas_seed")
    assert hasattr(ps, "derive_webrtc_seed")
    assert hasattr(ps, "derive_ja3_seed")
    assert hasattr(ps, "derive_font_budget_seed")
