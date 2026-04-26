"""
cookie_pool.py — The session-resurrection layer.

After a clean run (no captcha, no block) we freeze the current
cookie state + localStorage + sessionStorage into the cookie_snapshots
table. If a later run runs into trouble, we can inject the last
clean snapshot to restore the session rather than starting cold.

Three ways to create a snapshot:
  - auto_clean_run   — main.py calls snapshot_after_run() at end of
                       a clean run (no captchas, exit_code=0)
  - auto_warmup      — warmup engine calls after a successful warmup
                       so the just-seeded cookies are immediately
                       available as a fallback
  - manual           — user clicks "Snapshot now" in the UI

Restore is manual for now: UI has a Restore button. Auto-restore
on captcha is explicitly NOT wired — it's easy to make a session
worse by injecting stale cookies, so a human confirms.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
import time
from typing import Optional

from ghost_shell.db import get_db


# ═══════════════════════════════════════════════════════════════
# Extraction — pull cookies + storage out of a running driver
# ═══════════════════════════════════════════════════════════════

def extract_state(driver) -> tuple[list, dict]:
    """Pull everything we want to freeze from a live browser.

    Cookies come from the standard WebDriver API. localStorage is
    only accessible from the current document origin, so we limit
    ourselves to whatever page is loaded — that's why snapshots
    taken from main.py happen AT THE END of a run, after the last
    SERP query (Google origin is still on the tab).

    Returns (cookies, storage) where:
      cookies: list of {name, domain, value, expires, path, httpOnly, secure, sameSite}
      storage: {"<origin>": {"localStorage": {...}, "sessionStorage": {...}}}
    """
    cookies: list = []
    try:
        cookies = driver.get_cookies() or []
    except Exception as e:
        logging.warning(f"[cookie_pool] get_cookies() failed: {e}")

    storage: dict = {}
    try:
        origin = driver.execute_script("return window.location.origin")
        local = driver.execute_script(
            "const o={}; for (let i=0;i<localStorage.length;i++){"
            "const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o;"
        ) or {}
        session = driver.execute_script(
            "const o={}; for (let i=0;i<sessionStorage.length;i++){"
            "const k=sessionStorage.key(i); o[k]=sessionStorage.getItem(k);} return o;"
        ) or {}
        storage[origin] = {"localStorage": local, "sessionStorage": session}
    except Exception as e:
        logging.debug(f"[cookie_pool] storage dump failed: {e}")

    return cookies, storage


def snapshot_save(profile_name: str, driver, *,
                  run_id: Optional[int] = None,
                  trigger: str = "manual",
                  reason: str = None) -> int:
    """Freeze driver state → cookie_snapshots row. Returns snapshot id."""
    cookies, storage = extract_state(driver)
    db = get_db()
    sid = db.snapshot_save(profile_name, cookies, storage,
                           run_id=run_id, trigger=trigger, reason=reason)
    logging.info(
        f"[cookie_pool] snapshot #{sid} saved for {profile_name!r}: "
        f"{len(cookies)} cookies, {len(storage)} origins, trigger={trigger}"
    )
    return sid


# Convenience wrapper for the main-run finalizer — safe to call
# even if driver already dead (catches everything, returns None).
def snapshot_after_run(profile_name: str, driver, run_id: int,
                       *, had_captcha: bool,
                       exit_code: int = 0) -> Optional[int]:
    """Called from main.py at end-of-run. Only snapshots on clean runs.

    Clean = exit_code 0 AND no captchas. Noisy runs would freeze bad
    state into the pool, defeating the point.
    """
    if exit_code != 0 or had_captcha:
        logging.debug(
            f"[cookie_pool] skipping auto-snapshot: "
            f"exit={exit_code}, captcha={had_captcha}"
        )
        return None
    try:
        return snapshot_save(profile_name, driver,
                             run_id=run_id, trigger="auto_clean_run",
                             reason=f"clean run #{run_id}")
    except Exception as e:
        logging.warning(f"[cookie_pool] auto-snapshot failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# Restore — inject snapshot into a (not-yet-running) browser
# ═══════════════════════════════════════════════════════════════

def restore_to_driver(snapshot_id: int, driver) -> dict:
    """Inject a snapshot's cookies + storage into a live driver.

    Order matters:
      1. Delete existing cookies (clean slate — partial merge is
         trickier than it sounds and often leaves stale sessions)
      2. Add each cookie
      3. Navigate to each storage origin in turn, restore LS/SS

    Returns a summary {cookies_added, origins_restored, failures}.
    """
    db = get_db()
    snap = db.snapshot_get(snapshot_id)
    if not snap:
        raise ValueError(f"no such snapshot: {snapshot_id}")

    cookies = snap.get("cookies", [])
    storage = snap.get("storage", {})
    added = 0
    failures: list[str] = []

    # 1. Clear + add cookies. driver.add_cookie requires the current
    # URL to match the cookie's domain — for each cookie we navigate
    # to an origin that matches before adding. Cheaper: navigate once
    # to each distinct origin, add all its cookies.
    try:    driver.delete_all_cookies()
    except Exception as e: failures.append(f"delete_all_cookies: {e}")

    by_origin: dict[str, list[dict]] = {}
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not domain: continue
        origin = f"https://{domain}/"
        by_origin.setdefault(origin, []).append(c)

    for origin, origin_cookies in by_origin.items():
        try:
            driver.get(origin)
            time.sleep(0.5)
            for c in origin_cookies:
                try:
                    driver.add_cookie(_normalize_cookie(c))
                    added += 1
                except Exception as e:
                    failures.append(f"{c.get('name')}@{origin}: {e}")
        except Exception as e:
            failures.append(f"navigate {origin}: {e}")

    # 2. Restore storage per origin
    origins_restored = 0
    for origin, data in (storage or {}).items():
        try:
            driver.get(origin)
            time.sleep(0.3)
            for k, v in (data.get("localStorage") or {}).items():
                driver.execute_script(
                    "localStorage.setItem(arguments[0], arguments[1]);", k, v
                )
            for k, v in (data.get("sessionStorage") or {}).items():
                driver.execute_script(
                    "sessionStorage.setItem(arguments[0], arguments[1]);", k, v
                )
            origins_restored += 1
        except Exception as e:
            failures.append(f"storage {origin}: {e}")

    logging.info(
        f"[cookie_pool] restored snapshot #{snapshot_id}: "
        f"{added} cookies, {origins_restored} origins, "
        f"{len(failures)} failures"
    )
    return {
        "cookies_added":     added,
        "origins_restored":  origins_restored,
        "failures":          failures,
    }


def _normalize_cookie(c: dict) -> dict:
    """Strip Selenium-incompatible fields + fix common issues."""
    out = {}
    for k in ("name", "value", "domain", "path", "expiry",
              "httpOnly", "secure", "sameSite"):
        if c.get(k) is not None:
            out[k] = c[k]
    # Some snapshots use "expires" (WebDriver API name); Selenium wants "expiry"
    if "expires" in c and "expiry" not in out:
        try:    out["expiry"] = int(c["expires"])
        except Exception: pass
    # Selenium rejects sameSite="no_restriction" — normalize to None
    if out.get("sameSite") in ("no_restriction", "unspecified"):
        out.pop("sameSite")
    return out


# ═══════════════════════════════════════════════════════════════
# Static helpers (no driver required) — for REST endpoints
# ═══════════════════════════════════════════════════════════════

def list_snapshots(profile_name: str, limit: int = 50) -> list[dict]:
    return get_db().snapshot_list(profile_name, limit=limit)


def delete_snapshot(snapshot_id: int) -> bool:
    return get_db().snapshot_delete(snapshot_id)


def get_stats(profile_name: str) -> dict:
    return get_db().snapshot_stats(profile_name)


# ═══════════════════════════════════════════════════════════════
# Cross-profile injection (browserless)
#
# Pulls a snapshot from the pool and writes it to TARGET profile's
# session_dir as cookies.json + storage.json. At next launch of the
# target profile, the existing session-restore code (in
# session/manager.py) reads these files and injects them via CDP
# Network.setCookie. Result: target profile starts with ~50 third-
# party tracking cookies from a real UA-commerce session instead of
# cold zero-state -- looks aged to Google on first SERP visit.
#
# No browser launch needed. The actual injection happens
# transparently the next time the user starts the target profile.
# ═══════════════════════════════════════════════════════════════

def inject_to_profile_dir(target_profile: str,
                          snapshot_id: int = None,
                          *,
                          country: str = None,
                          category: str = None,
                          base_dir: str = None) -> dict:
    """Write a snapshot's cookies+storage to target_profile's
    session_dir/cookies.json + storage.json.

    If snapshot_id is None, auto-pick the best matching snapshot
    via snapshot_pool_pick_best(country, category, exclude=target).

    Existing files in the target session_dir are MERGED rather than
    overwritten -- if the target already has cookies (from a previous
    run), pool cookies are appended unless a same-name+domain entry
    already exists.

    Returns: {ok, snapshot_id, source_profile, cookies_written,
              storage_origins_written, merged_with_existing}
    """
    import os
    import json as _json
    from ghost_shell.session.cookies import (
        profile_session_dir, cookies_path, storage_path,
    )

    db = get_db()

    # Resolve snapshot
    snap = None
    if snapshot_id:
        snap = db.snapshot_get(snapshot_id)
        if not snap:
            return {"ok": False, "error": f"snapshot {snapshot_id} not found"}
    else:
        snap = db.snapshot_pool_pick_best(
            country=country, category=category,
            exclude_profile=target_profile,
        )
        if not snap:
            return {
                "ok":      False,
                "error":   "no matching snapshot in pool",
                "tried":   {"country": country, "category": category},
            }
        snapshot_id = snap.get("id")

    pool_cookies = snap.get("cookies") or []
    pool_storage = snap.get("storage") or {}

    # Make sure session_dir exists
    sdir = profile_session_dir(target_profile, base_dir)
    os.makedirs(sdir, exist_ok=True)

    cpath = cookies_path(target_profile, base_dir)
    spath = storage_path(target_profile, base_dir)

    # Merge with existing cookies.json if any
    existing_cookies: list = []
    merged_with_existing = False
    if os.path.exists(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                existing_cookies = _json.load(f) or []
            merged_with_existing = True
        except Exception as e:
            logging.warning(f"[cookie_pool] couldn't read existing cookies: {e}")
            existing_cookies = []

    # Index existing by (name, domain) for dedupe
    seen = set()
    for c in existing_cookies:
        if isinstance(c, dict):
            seen.add((c.get("name"), c.get("domain")))

    appended = 0
    for c in pool_cookies:
        if not isinstance(c, dict):
            continue
        key = (c.get("name"), c.get("domain"))
        if key in seen:
            continue
        existing_cookies.append(c)
        seen.add(key)
        appended += 1

    try:
        with open(cpath, "w", encoding="utf-8") as f:
            _json.dump(existing_cookies, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False,
                "error": f"couldn't write {cpath}: {e}"}

    # Merge storage similarly -- per-origin
    existing_storage: dict = {}
    if os.path.exists(spath):
        try:
            with open(spath, "r", encoding="utf-8") as f:
                existing_storage = _json.load(f) or {}
        except Exception:
            existing_storage = {}

    storage_origins_added = 0
    for origin, data in (pool_storage or {}).items():
        if origin not in existing_storage:
            existing_storage[origin] = data
            storage_origins_added += 1

    try:
        with open(spath, "w", encoding="utf-8") as f:
            _json.dump(existing_storage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # Non-fatal -- cookies are the most important thing
        logging.warning(f"[cookie_pool] storage write failed: {e}")

    logging.info(
        f"[cookie_pool] injected snapshot #{snapshot_id} "
        f"from {snap.get('profile_name')!r} into "
        f"{target_profile!r}: +{appended} cookies, "
        f"+{storage_origins_added} storage origins "
        f"(merged_existing={merged_with_existing})"
    )

    return {
        "ok":                       True,
        "snapshot_id":              snapshot_id,
        "source_profile":           snap.get("profile_name"),
        "cookies_written":          appended,
        "storage_origins_written":  storage_origins_added,
        "total_cookies_after":      len(existing_cookies),
        "merged_with_existing":     merged_with_existing,
        "country":                  snap.get("country"),
        "category":                 snap.get("category"),
    }
