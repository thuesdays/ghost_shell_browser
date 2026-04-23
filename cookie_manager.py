"""
cookie_manager.py — Dashboard-side cookie storage for profiles.

Reads / writes cookies for a profile WITHOUT launching Chrome. Each
profile has its session stored at:

    profiles/<name>/ghostshell_session/cookies.json

Format is the Selenium cookie dict shape (what driver.get_cookies()
returns), which maps 1:1 to the most common browser-extension export
formats (EditThisCookie, Cookie-Editor, Cookie-Quick-Manager). We also
support Netscape `cookies.txt` because curl/wget users have it everywhere.

This module is READ/WRITE to disk only. When a profile is actively
running, Chrome's own SQLite DB is authoritative — changes here take
effect on the NEXT start. The dashboard should warn the user about this.
"""

import os
import json
import time
import logging
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────

def profile_session_dir(profile_name: str, base_dir: str = None) -> str:
    """Where session artifacts (cookies, storage) live for a profile."""
    base = base_dir or "profiles"
    return os.path.join(base, profile_name, "ghostshell_session")


def cookies_path(profile_name: str, base_dir: str = None) -> str:
    return os.path.join(profile_session_dir(profile_name, base_dir), "cookies.json")


def storage_path(profile_name: str, base_dir: str = None) -> str:
    return os.path.join(profile_session_dir(profile_name, base_dir), "storage.json")


# ──────────────────────────────────────────────────────────────
# Read
# ──────────────────────────────────────────────────────────────

def list_cookies(profile_name: str, base_dir: str = None) -> list:
    """Return the list of stored cookies as Selenium-shape dicts.
    Returns [] if no cookies.json exists yet."""
    path = cookies_path(profile_name, base_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Some older formats wrap in {cookies: [...]}
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            return data["cookies"]
        return []
    except Exception as e:
        logging.warning(f"[cookies] Failed to read {path}: {e}")
        return []


def list_storage(profile_name: str, base_dir: str = None) -> dict:
    """Return the stored localStorage / sessionStorage map, or {}."""
    path = storage_path(profile_name, base_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logging.warning(f"[storage] Failed to read {path}: {e}")
        return {}


# ──────────────────────────────────────────────────────────────
# Write
# ──────────────────────────────────────────────────────────────

def save_cookies(profile_name: str, cookies: list, base_dir: str = None) -> None:
    """Overwrite the cookies file with the given list.

    The list entries should be Selenium-shape dicts:
      {name, value, domain, path, secure, httpOnly, expiry, sameSite}

    We don't validate heavily here — if the caller passes garbage,
    the worker will just skip those entries at import time. But we DO
    drop obviously-malformed records (missing name or domain) because
    those would break add_cookie() on the worker side.
    """
    cleaned = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        if not c.get("name") or not c.get("domain"):
            continue
        cleaned.append(_normalize_cookie(c))

    session_dir = profile_session_dir(profile_name, base_dir)
    os.makedirs(session_dir, exist_ok=True)
    path = cookies_path(profile_name, base_dir)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clear_cookies(profile_name: str, base_dir: str = None) -> None:
    """Remove all cookies for this profile. Chrome's own DB is NOT
    touched — only the dashboard-facing JSON. On next worker start the
    worker will load this empty file → profile browses as logged-out."""
    save_cookies(profile_name, [], base_dir)


# ──────────────────────────────────────────────────────────────
# Import format converters — accept lots of common formats
# ──────────────────────────────────────────────────────────────

def _normalize_cookie(raw: dict) -> dict:
    """Convert various extension-specific cookie shapes into the
    Selenium dict our worker expects.

    Handles:
      * Selenium / WebDriver (pass-through)
      * EditThisCookie / Cookie-Editor:
          {name, value, domain, path, secure, httpOnly, hostOnly,
           session, expirationDate, sameSite, storeId}
      * Puppeteer/Playwright:
          {name, value, domain, path, secure, httpOnly, expires, sameSite}
    """
    out = {
        "name":   raw.get("name"),
        "value":  raw.get("value", ""),
        "domain": raw.get("domain"),
        "path":   raw.get("path", "/"),
    }

    # Secure / httpOnly — direct copy, default False
    out["secure"]   = bool(raw.get("secure", False))
    out["httpOnly"] = bool(raw.get("httpOnly", raw.get("httponly", False)))

    # Expiry — unify various key names to Selenium's "expiry" (unix seconds, int)
    exp = (
        raw.get("expiry")
        or raw.get("expires")
        or raw.get("expirationDate")
        or raw.get("Expires")
    )
    if exp is not None:
        try:
            # Some exporters use floats (fractional seconds); coerce to int.
            # Negative or 0 = session cookie → just omit the key.
            iexp = int(float(exp))
            if iexp > 0:
                out["expiry"] = iexp
        except Exception:
            pass

    # sameSite — normalize case. Selenium wants "Strict" / "Lax" / "None".
    ss = raw.get("sameSite") or raw.get("samesite")
    if ss:
        ss = str(ss).strip().lower()
        if   ss in ("strict", "s"):      out["sameSite"] = "Strict"
        elif ss in ("lax",):              out["sameSite"] = "Lax"
        elif ss in ("none", "no_restriction", "unspecified"):
            out["sameSite"] = "None"

    return out


def parse_import(blob: str) -> list:
    """Parse a cookie import payload — detects JSON vs Netscape.

    JSON: a list of cookie objects OR {cookies: [...]}.
    Netscape: tab-separated lines per cookie (the classic cookies.txt).

    Returns a list of normalized Selenium-shape dicts. Raises ValueError
    if the payload is unparseable.
    """
    blob = (blob or "").strip()
    if not blob:
        raise ValueError("Empty import")

    # Try JSON first — covers ~90% of cases (browser extensions all use JSON)
    if blob[0] in "[{":
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of cookies or {cookies: [...]}")
        return [_normalize_cookie(c) for c in data if isinstance(c, dict)]

    # Netscape cookies.txt — `# Netscape HTTP Cookie File` header or tab-delimited rows
    rows = []
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, include_subs, path, secure, expiry, name, value = parts[:7]
        rows.append(_normalize_cookie({
            "domain":  domain,
            "path":    path or "/",
            "secure":  secure.upper() == "TRUE",
            "expiry":  expiry,
            "name":    name,
            "value":   value,
        }))
    if not rows:
        raise ValueError(
            "Could not parse as JSON or Netscape cookies.txt. "
            "Expected either a JSON array of cookie objects, or "
            "tab-separated lines per the Netscape spec."
        )
    return rows


# ──────────────────────────────────────────────────────────────
# Export format converters
# ──────────────────────────────────────────────────────────────

def to_netscape(cookies: list) -> str:
    """Convert our Selenium-shape list back to a Netscape cookies.txt
    string. Useful for curl/wget users."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Exported by Ghost Shell",
        "",
    ]
    now_ts = int(time.time())
    for c in cookies:
        domain = c.get("domain", "")
        # Netscape's "include subdomains" flag — convention is leading dot means yes
        include_subs = "TRUE" if domain.startswith(".") else "FALSE"
        path    = c.get("path", "/")
        secure  = "TRUE" if c.get("secure") else "FALSE"
        # Session cookies in Netscape format use 0 for expiry
        expiry  = c.get("expiry") or 0
        name    = c.get("name", "")
        value   = c.get("value", "")
        lines.append("\t".join([
            domain, include_subs, path, secure, str(expiry), name, value,
        ]))
    return "\n".join(lines) + "\n"
