"""
ja3_check.py — TLS fingerprint validation for the patched Chromium.

Why this matters
────────────────
Detection at the JS layer is half the story. The TLS ClientHello our
Chromium sends — cipher suite ordering, extensions, supported groups,
EC point formats — is fingerprinted by **CloudFlare, Akamai, PerimeterX,
DataDome, etc. BEFORE a single byte of HTML reaches us**. Two summary
hashes capture this:

* **JA3** (Salesforce, 2017) — MD5 over a comma-separated string of
  TLS version + cipher list + extensions + supported groups + EC
  point formats.
* **JA4** (FoxIO, 2023) — newer scheme; more granular, splits TLS
  vs HTTP/2 vs HTTP/3 fingerprints. Better but less universally
  reported by free check endpoints.

A genuine Chrome 149 emits a specific JA3 hash. If our patched
Chromium drifts even slightly (cipher reordering, extension removal,
TLS lib version skew during a Chromium rebase), CloudFlare will
fingerprint us as "weird browser" and rate-limit / captcha us
regardless of how clean our JS surface looks.

This module does not generate JA3 ourselves — it asks a public
endpoint to report what it saw, then compares.

How it's used
─────────────
::

    from ghost_shell.fingerprint.ja3_check import probe_ja3, verdict_for

    seen = probe_ja3(driver)         # blocks ~2s, runs JS in browser
    v = verdict_for(seen, expected_chrome_major=149)
    # v["ok"] / v["level"] (ok|warn|critical) / v["reason"]

The driver-based probe injects a fetch() call that hits the report
endpoint. We reach the endpoint THROUGH our proxy — so the JA3 is
the one a real site would observe (proxy-aware). The call also
captures the User-Agent the endpoint saw, for cross-checking
against navigator.userAgent.

Public API
──────────

  probe_ja3(driver, endpoint=None, timeout=15.0) -> dict
      # → {"ja3":"<hex>", "ja3_full":"...", "user_agent":"...",
      #    "ip":"<exit ip>", "tls_version":"1.3"}
      # Returns {} on probe failure (network / endpoint down / parse).

  verdict_for(probe_result: dict, expected_chrome_major: int) -> dict
      # → {"ok": bool, "level": "ok|warn|critical",
      #    "expected_ja3": "<hex>|None", "actual_ja3": "...",
      #    "reason": "..."}

  EXPECTED_JA3_BY_MAJOR: dict[int, list[str]]
      # Known-good JA3 hashes per Chrome major version. Multiple
      # values per major because Chrome rotates cipher orderings
      # across minor versions and OSes. Updated alongside Chromium
      # rebases.

The endpoint default is ``https://check.ja3.zone/json`` — picked for
JSON-only response (parses cleanly), CORS-enabled (works from JS),
and free / no-API-key. ``probe_ja3`` tolerates other endpoints with
the same fields if you override (some users prefer self-hosted).

Pure helper functions are tested in tests/test_ja3_check.py — no
live network needed (mocks driver.execute_script).
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Known-good JA3 hashes by Chrome major version
#
# These are EMPIRICAL — captured from a stock chrome.exe of each
# major version against check.ja3.zone, NOT theoretical. Values
# vary slightly across Win/Mac/Linux because OS-level TLS libs
# differ.
#
# When rebasing patched Chromium to a new major (e.g. 150 in 6
# weeks), capture stock Chrome 150's JA3 and add to the list. If
# our patched build's hash matches stock's hash → patches don't
# leak at the TLS level. If it diverges → investigate the TLS
# stack diff.
#
# Multiple values per major are expected (different minor
# versions, different OS TLS lib versions). The verdict treats a
# match against ANY listed value as ok.
# ──────────────────────────────────────────────────────────────

EXPECTED_JA3_BY_MAJOR: dict[int, list[str]] = {
    # ⚠ NO BASELINES POPULATED YET. Until each Chrome major has at
    # least one EMPIRICALLY-CAPTURED hash here, the JA3 check returns
    # warn-level → check_ja3_matches_chrome skips → no score penalty.
    # This is intentional: a fabricated placeholder hash would penalize
    # every legitimate run for "mismatching" something we made up.
    #
    # To populate baselines (one-time per Chrome major release):
    #   1. Run a STOCK chrome.exe of the target major (without our
    #      patches) on a clean Win10 x64 box.
    #   2. Visit https://check.ja3.zone/json
    #   3. Copy the "ja3" field value (32-char lowercase hex).
    #   4. Append to the list for that major below.
    #   5. Optionally add Mac / Linux variants (TLS lib differs).
    #
    # Multiple values per major are normal — Win/Mac/Linux differ on
    # cipher ordering. The verdict treats a match against ANY listed
    # hash as ok.
    149: [
        # No baseline yet. Capture and replace this comment.
    ],
    148: [
        # No baseline yet.
    ],
}


# Default report endpoint. Returns JSON like:
#   {"ja3":"<hex>","ja3_full":"<num>,<num>...","user_agent":"...",
#    "ip":"...","tls_version":"1.3"}
DEFAULT_JA3_ENDPOINT = "https://check.ja3.zone/json"


# ──────────────────────────────────────────────────────────────
# Pure helpers (no network)
# ──────────────────────────────────────────────────────────────

def parse_probe_response(raw: str) -> dict:
    """Parse a JSON response from check.ja3.zone (or compatible
    endpoint). Returns {} on parse failure or missing required
    fields. Tolerant of extra fields the endpoint may add.

    Required fields: ``ja3`` (32-char MD5 hex). Optional:
    ``ja3_full``, ``user_agent``, ``ip``, ``tls_version``."""
    if not raw or not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        ja3 = data.get("ja3")
        if not ja3 or not isinstance(ja3, str) or len(ja3) != 32:
            return {}
        # Validate hex shape
        try:
            int(ja3, 16)
        except ValueError:
            return {}
        return {
            "ja3":         ja3.lower(),
            "ja3_full":    str(data.get("ja3_full") or ""),
            "user_agent":  str(data.get("user_agent") or ""),
            "ip":          str(data.get("ip") or ""),
            "tls_version": str(data.get("tls_version") or ""),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def verdict_for(probe_result: dict,
                expected_chrome_major: Optional[int]) -> dict:
    """Compare a probe result against the EXPECTED_JA3_BY_MAJOR table.

    Returns a verdict dict with keys:
      * ``ok``           — True iff actual matches one of the expected
                           hashes for the given major version
      * ``level``        — "ok" / "warn" / "critical"
      * ``actual_ja3``   — what was observed (or None)
      * ``expected_ja3`` — list of expected hashes for that major
                           (empty list if no baseline yet)
      * ``reason``       — human-readable summary

    Levels:
      * ok       — actual == one of expected
      * warn     — no probe result OR no baseline for that major
                   (we can't decide — surface as a soft warning)
      * critical — actual != all expected (real fingerprint drift —
                   detection sites will flag this browser as
                   non-Chrome)"""
    actual = (probe_result or {}).get("ja3")
    expected_list = EXPECTED_JA3_BY_MAJOR.get(expected_chrome_major or 0, [])
    # Filter out placeholder/empty entries
    expected_list = [h for h in expected_list
                     if h and isinstance(h, str) and len(h) == 32]

    if not actual:
        return {
            "ok":           False,
            "level":        "warn",
            "actual_ja3":   None,
            "expected_ja3": expected_list,
            "reason":       "JA3 probe returned no usable data — "
                            "endpoint down, network blocked, or "
                            "TLS handshake failed.",
        }

    if not expected_list:
        return {
            "ok":           False,
            "level":        "warn",
            "actual_ja3":   actual,
            "expected_ja3": [],
            "reason":       (f"No baseline JA3 hash recorded for "
                             f"Chrome major={expected_chrome_major}. "
                             f"Capture a stock Chrome's hash and add "
                             f"to EXPECTED_JA3_BY_MAJOR before "
                             f"validating this version."),
        }

    if actual.lower() in (h.lower() for h in expected_list):
        return {
            "ok":           True,
            "level":        "ok",
            "actual_ja3":   actual,
            "expected_ja3": expected_list,
            "reason":       (f"JA3 matches expected baseline for "
                             f"Chrome {expected_chrome_major} "
                             f"({actual[:8]}…)."),
        }

    return {
        "ok":           False,
        "level":        "critical",
        "actual_ja3":   actual,
        "expected_ja3": expected_list,
        "reason":       (f"JA3 MISMATCH: observed {actual[:8]}… "
                         f"but Chrome {expected_chrome_major} should "
                         f"emit {expected_list[0][:8]}…. The patched "
                         f"build's TLS stack is drifting from stock "
                         f"Chrome — detection sites (CloudFlare, "
                         f"Akamai, PerimeterX) will fingerprint this "
                         f"as a non-Chrome browser and rate-limit / "
                         f"captcha. Investigate TLS lib version skew "
                         f"introduced by recent rebase / patches."),
    }


# ──────────────────────────────────────────────────────────────
# Live probe (Selenium-based)
# ──────────────────────────────────────────────────────────────

# JS that fetches the report endpoint and returns the JSON body as a
# string. Runs through Chrome's network stack — so the proxy and the
# patched TLS layer are both in effect, exactly the path real sites
# would observe.
_PROBE_SCRIPT = r"""
const url = arguments[0];
const cb  = arguments[arguments.length - 1];
fetch(url, { method: "GET", credentials: "omit", cache: "no-store" })
  .then(r => r.text())
  .then(txt => cb(txt))
  .catch(err => cb("ERROR:" + (err && err.message || String(err))));
"""


def probe_ja3(driver,
              endpoint: str = DEFAULT_JA3_ENDPOINT,
              timeout: float = 15.0) -> dict:
    """Run an in-browser fetch() against the JA3 report endpoint and
    return the parsed result. Empty dict on any failure.

    The driver must be at least at ``about:blank`` (or any same-origin
    or any-origin context where fetch() works — modern Chrome allows
    cross-origin fetch from about:blank).

    Args:
      driver:   A live Selenium WebDriver attached to our patched Chromium.
      endpoint: URL returning JSON with at least a ``ja3`` field.
      timeout:  Max seconds for the JS to complete.
    """
    try:
        driver.set_script_timeout(timeout)
    except Exception:
        pass
    try:
        raw = driver.execute_async_script(_PROBE_SCRIPT, endpoint)
    except Exception as e:
        logging.debug(f"[ja3_check] probe failed: {e}")
        return {}
    if not isinstance(raw, str):
        return {}
    if raw.startswith("ERROR:"):
        logging.debug(f"[ja3_check] probe browser-side error: {raw[:160]}")
        return {}
    return parse_probe_response(raw)


# ──────────────────────────────────────────────────────────────
# CLI smoke
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m ghost_shell.fingerprint.ja3_check '<json>'")
        print("       (paste the body from check.ja3.zone/json)")
        sys.exit(2)
    parsed = parse_probe_response(sys.argv[1])
    print("parsed:", json.dumps(parsed, indent=2))
    if parsed:
        v = verdict_for(parsed, expected_chrome_major=149)
        print("verdict:", json.dumps(v, indent=2))
