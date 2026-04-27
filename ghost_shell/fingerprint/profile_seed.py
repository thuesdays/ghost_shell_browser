"""
profile_seed.py — Derive deterministic per-profile seeds for the C++
stealth patches.

Sprint 11: the four Tier-2 Chromium patches (Skia canvas noise,
WebRTC ICE filter, BoringSSL JA3, font privacy budget) each read their
own seed from a CLI flag passed at chrome launch. To keep the surface
area on the Python side small and the cross-launch identity stable,
we derive ALL of them from a single root seed: ``profile.id`` +
``profile.name`` salted with a build-time secret. Same profile across
launches → same seed → same fingerprint. Different profiles → different
seeds → different fingerprints.

Salt strategy
─────────────
The build-time salt is read from ``GHOST_SHELL_FP_SALT`` env var, with
a fallback to a constant baked into ``ghost_shell/__init__.__version__``.
Why a salt at all: prevents a third party from precomputing a table
of "profile_name → expected canvas hash" if they ever got hold of the
naming convention. The salt is application-private; it doesn't need to
be cryptographically secret like a vault key.

Public API
──────────
* ``derive_root_seed(profile_id, profile_name) -> int``
* ``derive_canvas_seed(profile_id, profile_name) -> int``
* ``derive_webrtc_seed(profile_id, profile_name) -> int``
* ``derive_ja3_seed(profile_id, profile_name) -> int``
* ``derive_font_budget_seed(profile_id, profile_name) -> int``
* ``build_chrome_args(profile_id, profile_name) -> list[str]``
    one-shot helper that produces every CLI flag the patches consume.

The seeds are 64-bit unsigned ints rendered as hex (16 chars) when
passed on the command line — Chromium's flag parser accepts strings
and the C++ side calls ``strtoull(..., 16)`` to recover.

Why 64-bit and not full SHA-256:
* Chromium switches store/parse flag values as ``base::Value::Type::STRING``
  with a 256-byte size limit; a 16-char hex stays well under.
* The patches use the seed to seed ``std::mt19937_64`` (Skia, font budget)
  or as input to ``MD5`` (WebRTC nonce, JA3 GREASE). 64 bits of entropy
  is plenty for those use cases.
* Predictable size makes the C++ parser bulletproof.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import hashlib
import os
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

# Build-time salt. Override per-deployment via env var. The fallback
# is a literal string baked into the package — see security note in
# the module docstring.
_DEFAULT_SALT = b"ghost-shell-anty-v0.3.0-fp-seed-salt"

# Domain-separation tags. Each derived seed prepends its own tag so
# canvas / webrtc / ja3 / font seeds for the SAME profile are
# uncorrelated — a detector who learned one seed can't predict the
# others.
_DOMAIN_CANVAS  = b"canvas"
_DOMAIN_WEBRTC  = b"webrtc"
_DOMAIN_JA3     = b"ja3"
_DOMAIN_FONT    = b"font_budget"


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _salt() -> bytes:
    """Read the build-time salt from env, falling back to default."""
    return (os.environ.get("GHOST_SHELL_FP_SALT") or "").encode("utf-8") \
           or _DEFAULT_SALT


def _hash64(*parts: bytes) -> int:
    """Concat all parts with NUL separators, SHA-256, take low 64 bits.

    NUL separators prevent ambiguity collisions like
    ``("ab", "c") == ("a", "bc")``."""
    h = hashlib.sha256()
    for i, p in enumerate(parts):
        if i:
            h.update(b"\x00")
        h.update(p)
    digest = h.digest()
    # Low 64 bits, big-endian, unsigned. mt19937_64 takes uint64_t.
    return int.from_bytes(digest[:8], "big", signed=False)


def _profile_key(profile_id: Optional[int],
                 profile_name: str) -> bytes:
    """Canonical bytes representation of a profile identity. Both
    fields included so a rename doesn't accidentally re-use another
    profile's seed (id is the stable key; name is for display)."""
    pid = int(profile_id) if profile_id is not None else 0
    return f"{pid}:{profile_name or ''}".encode("utf-8")


# ──────────────────────────────────────────────────────────────
# Public API — per-domain seed derivation
# ──────────────────────────────────────────────────────────────

def derive_root_seed(profile_id: Optional[int],
                     profile_name: str) -> int:
    """Master seed. Not used directly by the C++ flags — the
    domain-tagged derivers below are. Exposed for tests + tooling."""
    return _hash64(_salt(), _profile_key(profile_id, profile_name))


def derive_canvas_seed(profile_id: Optional[int],
                       profile_name: str) -> int:
    """Seeds the Skia canvas noise injection. Same profile across
    launches → same canvas pixel-noise pattern → stable canvas hash.
    Different profiles → uncorrelated patterns."""
    return _hash64(_salt(), _DOMAIN_CANVAS,
                   _profile_key(profile_id, profile_name))


def derive_webrtc_seed(profile_id: Optional[int],
                       profile_name: str) -> int:
    """Seeds the per-profile WebRTC local-IP nonce.

    The patch substitutes a deterministic per-profile fake CIDR for
    the host's real LAN IP; the nonce randomises the suffix so two
    profiles on the same host don't collide on 192.168.1.42."""
    return _hash64(_salt(), _DOMAIN_WEBRTC,
                   _profile_key(profile_id, profile_name))


def derive_ja3_seed(profile_id: Optional[int],
                    profile_name: str) -> int:
    """Seeds BoringSSL ClientHello extension ordering + GREASE values
    so each profile presents a stable JA3 across sessions."""
    return _hash64(_salt(), _DOMAIN_JA3,
                   _profile_key(profile_id, profile_name))


def derive_font_budget_seed(profile_id: Optional[int],
                            profile_name: str) -> int:
    """Seeds the per-profile font allowlist used by the privacy-budget
    patch. Each profile sees a deterministic random subset of the
    host's installed fonts."""
    return _hash64(_salt(), _DOMAIN_FONT,
                   _profile_key(profile_id, profile_name))


# ──────────────────────────────────────────────────────────────
# Chrome CLI args composer
# ──────────────────────────────────────────────────────────────

def build_chrome_args(profile_id: Optional[int],
                      profile_name: str,
                      enabled_patches: Optional[set[str]] = None) -> list[str]:
    """Return the list of ``--flag=value`` strings to pass at launch.

    Each patch checks its own flag and silently no-ops when absent —
    so an unpatched stock Chromium still launches, the stealth
    features just don't fire. This makes Ghost Shell forward-
    compatible with operators who haven't applied the patches yet.

    Args:
        profile_id:       row id from profiles table (can be None for
                          legacy single-profile mode → seed is keyed
                          off name only).
        profile_name:     profile name string.
        enabled_patches:  subset of {"canvas", "webrtc", "ja3", "font"}
                          for selective rollout. None / empty → ALL.

    Returns: list of flag strings ready for ``options.add_argument``.

    Example:
        >>> build_chrome_args(7, "fb_main_us")
        ['--gs-canvas-seed=7c3e98a14f6d2b5e',
         '--gs-webrtc-seed=...',
         '--gs-ja3-seed=...',
         '--gs-font-budget-seed=...',
         '--webrtc-public-only',
         '--use-privacy-budget']
    """
    enabled = enabled_patches or {"canvas", "webrtc", "ja3", "font"}
    args: list[str] = []

    if "canvas" in enabled:
        seed = derive_canvas_seed(profile_id, profile_name)
        args.append(f"--gs-canvas-seed={seed:016x}")

    if "webrtc" in enabled:
        seed = derive_webrtc_seed(profile_id, profile_name)
        args.append(f"--gs-webrtc-seed={seed:016x}")
        # Companion flag — the patch only filters host candidates when
        # this is present. Lets ops disable just the filter while
        # keeping the seed (e.g. for debugging local dev).
        args.append("--webrtc-public-only")

    if "ja3" in enabled:
        seed = derive_ja3_seed(profile_id, profile_name)
        args.append(f"--gs-ja3-seed={seed:016x}")

    if "font" in enabled:
        seed = derive_font_budget_seed(profile_id, profile_name)
        args.append(f"--gs-font-budget-seed={seed:016x}")
        # Master switch for the privacy-budget code path. Patch
        # respects this so a built-with-patch Chromium can run the
        # stock font-list when the flag is absent.
        args.append("--use-privacy-budget")

    return args
