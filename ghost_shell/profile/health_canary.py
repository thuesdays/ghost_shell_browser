"""
health_canary.py — visit detection sites + score the result.

Sprint 4 (Health monitor): the profile periodically visits well-known
fingerprint detection sites and we record what they said. The dashboard
draws a sparkline per profile so the user sees "trust score is dropping
over the last 7 days, this profile is getting noticed" BEFORE captchas
start firing on the actual workload.

Three sites currently supported:

  * ``sannysoft``   — bot.sannysoft.com — a TABLE with 14+ binary
                      tests (webdriver, chrome, permissions, plugins,
                      languages, etc). Each cell green ✓ / red ✗.
                      We extract pass/fail counts.
  * ``creepjs``     — creepjs.com — a comprehensive fingerprinter
                      that emits a "Trust Score" percentage. We
                      scrape that number.
  * ``pixelscan``   — pixelscan.net — fingerprint analysis with a
                      risk label (low/medium/high). We map the
                      label to 0..100 score for trend tracking.

Each parser is best-effort: detection sites change layout often, so
the JS scrapers cast a wide net. On parser miss we record an `error`
row in the DB so the dashboard surfaces "couldn't parse" instead of
showing stale score.

Design principles
─────────────────
* Parsers return a uniform dict: site, score (0..100 or None),
  raw_score, passed, total, details (free-form), error.
* Navigation + parsing happens INSIDE the patched Chromium driver
  the caller already has — no separate Selenium session, no extra
  Chrome cost.
* All errors caught — never raise out of run_canary().
* Each site visit is independent — sannysoft failure doesn't skip
  creepjs.

Public API
──────────
  run_canary(driver, sites=None, navigation_timeout=20.0) -> list[dict]
      # → [{site, score, ...}, ...]   one entry per visited site
  parse_sannysoft(driver) -> dict
  parse_creepjs(driver) -> dict
  parse_pixelscan(driver) -> dict
  SUPPORTED_SITES: dict[str, str]   # site_id → URL
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
import time
from typing import Optional


SUPPORTED_SITES: dict[str, str] = {
    "sannysoft": "https://bot.sannysoft.com/",
    "creepjs":   "https://abrahamjuliot.github.io/creepjs/",
    "pixelscan": "https://www.pixelscan.net/",
}


# ──────────────────────────────────────────────────────────────
# JS payloads — extract score from each site's DOM
#
# These run via execute_script and return a JSON string. Wrapping
# in JSON.stringify keeps the wire payload simple and lets us
# return errors as the same shape ({"error": "..."}).
# ──────────────────────────────────────────────────────────────

_SANNYSOFT_PROBE = r"""
(function() {
    try {
        // Sannysoft variants we've seen in the wild:
        //   2018-2022 layout: <table> with <td class="result-passed/failed">
        //   2023+        : same table, classes rotated to "passed"/"failed"
        //   newer pages  : sometimes use background-color inline styles
        //   future-proof : fall through to ✓/✗/❌ unicode markers in text
        // Cast a wide net across all signal sources.
        const allRows = document.querySelectorAll('table tr, .test-row, [data-test]');
        let passed = 0, failed = 0;
        const details = [];

        for (const row of allRows) {
            const cells = row.querySelectorAll('td, .result-cell, [data-result]');
            if (cells.length < 2) continue;
            const name = (cells[0].innerText || cells[0].textContent || '').trim();
            if (!name || name.length > 100) continue;  // header/junk
            const last = cells[cells.length - 1];
            const cls  = (last.className   || '').toLowerCase();
            const text = (last.innerText   || last.textContent || '').trim();
            const ltext = text.toLowerCase();
            const dataRes = (last.getAttribute('data-result') || '').toLowerCase();
            // Background-color heuristic for old layouts
            const bg = (last.style.backgroundColor || '').toLowerCase();
            const isGreen = /(rgb\(0?,\s?128|green|#0[a-f0-9]{0,2}[8-f])/.test(bg);
            const isRed   = /(rgb\(255,\s?0|#f|red|#[a-f0-9]{0,2}0{0,4}[a-f0-9]?)/.test(bg) && bg !== '';

            const okSignals = [
                cls.includes('passed'),
                cls.includes('result-passed'),
                cls.includes('test-pass'),
                ltext === 'passed',
                ltext === 'ok',
                ltext === 'true',
                /^✓|^✔|^present|^chrome|^supported/.test(ltext),
                dataRes === 'pass' || dataRes === 'ok',
                isGreen,
            ];
            const failSignals = [
                cls.includes('failed'),
                cls.includes('result-failed'),
                cls.includes('test-fail'),
                ltext === 'failed',
                ltext === 'false',
                /^✗|^❌|^missing|^undefined|^not (present|supported)/.test(ltext),
                dataRes === 'fail' || dataRes === 'failed',
                isRed,
            ];
            const ok   = okSignals.some(Boolean);
            const fail = failSignals.some(Boolean);
            if (ok && !fail)      { passed++; details.push({name, ok: true,  v: text.slice(0, 60)}); }
            else if (fail && !ok) { failed++; details.push({name, ok: false, v: text.slice(0, 60)}); }
            // Ambiguous rows skipped — common for descriptive headings
            // and "Test name | description" rows that have no value cell.
        }
        const total = passed + failed;
        return JSON.stringify({
            passed, failed, total,
            details: details.slice(0, 25),
            // Verbose: capture page snippet for tuning iteration when
            // total is suspiciously low (<8 rows on sannysoft means
            // the parser missed the table)
            page_snippet: total < 8
                ? (document.body.innerText || '').slice(0, 800)
                : null,
        });
    } catch (e) {
        return JSON.stringify({error: String(e)});
    }
})();
"""

_CREEPJS_PROBE = r"""
(function() {
    try {
        // CreepJS shows a big "X.YY% trust" or "Trust Score: X" text.
        // Layout changes; cast a wide net via innerText regex.
        const txt = document.body.innerText || '';
        // Pattern A: "X% trust" / "X percent trust"
        let m = txt.match(/([\d.]+)\s*%?\s*(?:trust|score)/i);
        let trust = m ? parseFloat(m[1]) : null;
        // Pattern B: "Trust Score: X.YY"
        if (trust === null) {
            m = txt.match(/trust\s*score\s*:\s*([\d.]+)/i);
            if (m) trust = parseFloat(m[1]);
        }
        // Heuristic: if value > 1 assume already on 0..100 scale,
        // else it's a 0..1 fraction.
        if (trust !== null && trust <= 1.5) trust *= 100;
        // Fingerprint hash if present (for forensic dive)
        const h = txt.match(/fingerprint:\s*([0-9a-f]{16,})/i);
        return JSON.stringify({
            trust_score:    trust,
            fingerprint_hash: h ? h[1] : null,
            body_excerpt:   txt.slice(0, 500),
        });
    } catch (e) {
        return JSON.stringify({error: String(e)});
    }
})();
"""

_PIXELSCAN_PROBE = r"""
(function() {
    try {
        const txt = document.body.innerText || '';
        // Pixelscan's verdict text is one of: low / medium / high
        // risk, sometimes with a percentage.
        let risk = (txt.match(/risk\s*[:\-]?\s*(low|medium|high)/i) || [])[1];
        let pct  = (txt.match(/(\d{1,3})\s*\/\s*100/) || [])[1];
        if (pct) pct = parseInt(pct, 10);
        // Some pages say "consistency: X%" — capture as fallback
        if (!pct) {
            const m = txt.match(/consistency\s*:\s*(\d{1,3})\s*%/i);
            if (m) pct = parseInt(m[1], 10);
        }
        return JSON.stringify({
            risk_label:    risk ? risk.toLowerCase() : null,
            score_raw:     pct,
            body_excerpt:  txt.slice(0, 500),
        });
    } catch (e) {
        return JSON.stringify({error: String(e)});
    }
})();
"""


# ──────────────────────────────────────────────────────────────
# Parsers — uniform return shape
# ──────────────────────────────────────────────────────────────

def _normalize(site: str, score=None, raw_score=None, passed=None,
               total=None, details=None, error=None) -> dict:
    """Common return shape for all parsers."""
    return {
        "site":      site,
        "score":     score,
        "raw_score": raw_score,
        "passed":    passed,
        "total":     total,
        "details":   details or {},
        "error":     error,
    }


def _safe_run_js(driver, script: str) -> dict:
    """Run a probe JS, decode JSON, never raise. Returns dict or
    {"error": "..."}."""
    try:
        raw = driver.execute_script(script)
    except Exception as e:
        return {"error": f"execute_script failed: {type(e).__name__}: {e}"}
    if not isinstance(raw, str):
        return {"error": "probe returned non-string"}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"error": "probe returned non-object JSON"}
        return data
    except json.JSONDecodeError as e:
        return {"error": f"probe JSON decode failed: {e}"}


def parse_sannysoft(driver) -> dict:
    """Run the sannysoft probe + normalize. Score = passed / total
    × 100, scaled 0..100. None if nothing parseable.

    Sannysoft's typical row count is 14-18. If we extract <8 rows
    the parser likely missed the table — surface a page-snippet in
    the error so the user can iterate on selectors quickly."""
    data = _safe_run_js(driver, _SANNYSOFT_PROBE)
    if "error" in data:
        return _normalize("sannysoft", error=data["error"])
    passed = int(data.get("passed") or 0)
    total  = int(data.get("total")  or 0)
    if total <= 0:
        snip = data.get("page_snippet") or ""
        return _normalize(
            "sannysoft",
            error="no parseable rows on the page — selectors may need tuning",
            details={
                "passed": passed,
                "total":  total,
                "page_snippet": snip[:400] if snip else None,
            },
        )
    score = round(passed / total * 100)
    out = _normalize(
        "sannysoft",
        score     = score,
        raw_score = f"{passed}/{total}",
        passed    = passed,
        total     = total,
        details   = {"check_details": data.get("details", [])},
    )
    # Carry forward debug snippet only when total is suspicious
    if total < 8 and data.get("page_snippet"):
        out["details"]["page_snippet"] = data["page_snippet"][:400]
    return out


def parse_creepjs(driver) -> dict:
    """Run the creepjs probe. Score = trust_score directly when
    extractable."""
    data = _safe_run_js(driver, _CREEPJS_PROBE)
    if "error" in data:
        return _normalize("creepjs", error=data["error"])
    trust = data.get("trust_score")
    if trust is None:
        return _normalize("creepjs",
                          error="couldn't find trust score on page",
                          details={"body_excerpt": data.get("body_excerpt", "")[:200]})
    score = max(0, min(100, round(float(trust))))
    return _normalize(
        "creepjs",
        score     = score,
        raw_score = f"{trust:.1f}%" if isinstance(trust, (int, float)) else str(trust),
        details   = {"fingerprint_hash": data.get("fingerprint_hash")},
    )


def parse_pixelscan(driver) -> dict:
    """Run the pixelscan probe. Risk label maps to discrete score
    bands: low=85, medium=50, high=15. If a numeric score is
    present alongside the label, that wins."""
    data = _safe_run_js(driver, _PIXELSCAN_PROBE)
    if "error" in data:
        return _normalize("pixelscan", error=data["error"])
    raw_score = data.get("score_raw")
    risk      = data.get("risk_label")
    if raw_score is not None:
        score = max(0, min(100, int(raw_score)))
        raw_str = str(raw_score)
    elif risk:
        score = {"low": 85, "medium": 50, "high": 15}.get(risk, 50)
        raw_str = risk
    else:
        return _normalize("pixelscan",
                          error="no risk label or score on page",
                          details={"body_excerpt": data.get("body_excerpt", "")[:200]})
    return _normalize(
        "pixelscan",
        score     = score,
        raw_score = raw_str,
        details   = {"risk_label": risk},
    )


_PARSER_BY_SITE = {
    "sannysoft": parse_sannysoft,
    "creepjs":   parse_creepjs,
    "pixelscan": parse_pixelscan,
}


# ──────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────

def run_canary(driver,
               sites: Optional[list] = None,
               navigation_timeout: float = 20.0,
               settle_sec: float = 2.0,
               restore_url: bool = True) -> list:
    """Visit each site in turn, parse, return a list of result dicts.

    Args:
      driver: live Selenium WebDriver attached to our patched Chromium.
      sites: site IDs to visit. None / empty → all SUPPORTED_SITES.
        Unknown IDs raise ValueError early (caught here, recorded
        as error rows in the DB by the caller).
      navigation_timeout: max seconds to wait for each navigation.
      settle_sec: time to let JS render after onload (CreepJS in
        particular runs its work async after first paint).
      restore_url: when True (default), captures current_url before
        the first canary navigation and navigates back at the end.
        RC-81 fix: prevents the canary from leaving the browser
        parked on a detection site, breaking subsequent script
        steps that expected to be on the SERP / target page.

    Returns: list of dicts, one per site, in the order visited.
    Each conforms to ``_normalize`` shape — score may be None on
    parser failure, error populated then."""
    targets = list(sites) if sites else list(SUPPORTED_SITES.keys())
    results = []

    # RC-81: save current URL so we can restore after the canary
    # tour. Empty / about:blank URLs aren't worth restoring.
    saved_url = None
    if restore_url:
        try:
            saved_url = driver.current_url
            if not saved_url or saved_url in ("about:blank", "data:,"):
                saved_url = None
        except Exception:
            saved_url = None

    # Keep page-load timeout sane
    try:
        driver.set_page_load_timeout(navigation_timeout)
    except Exception:
        pass

    for site in targets:
        url = SUPPORTED_SITES.get(site)
        if not url:
            results.append(_normalize(site,
                error=f"unknown site id {site!r}; "
                      f"valid: {list(SUPPORTED_SITES.keys())}"))
            continue

        parser = _PARSER_BY_SITE.get(site)
        if parser is None:
            results.append(_normalize(site, error="no parser registered"))
            continue

        try:
            driver.get(url)
        except Exception as e:
            results.append(_normalize(
                site, error=f"navigation failed: {type(e).__name__}: {e}"))
            continue

        # Some detection sites run their checks asynchronously after
        # onload. Sleep a bit before scraping.
        time.sleep(settle_sec)

        try:
            results.append(parser(driver))
        except Exception as e:
            logging.exception(f"[health_canary] {site} parser crashed")
            results.append(_normalize(
                site, error=f"parser crashed: {type(e).__name__}: {e}"))

    # RC-81: restore the page the script was on before our canary
    # detour. Best-effort — if navigation fails we let the script's
    # own error handlers deal with it.
    if saved_url:
        try:
            driver.get(saved_url)
        except Exception as e:
            logging.debug(f"[health_canary] URL restore failed: {e}")

    return results
