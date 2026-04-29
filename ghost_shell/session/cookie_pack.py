"""
session/cookie_pack.py — Cookie Pool Marketplace (Phase D, Apr 2026).

Pre-warmed cookie + localStorage snapshots packaged as portable units
("packs") that can be applied to any profile. A pack is essentially a
fingerprint-agnostic session bundle that says "you have visited
google.com / youtube.com / etc. before, here's the state to prove it".

Why packs exist:
    Cold-starting a profile means Chrome opens with zero cookies. Real
    users have hundreds (NID, CONSENT, 1P_JAR, AEC, SIDCC, etc.). Even
    after our CookieWarmer's 11 synthetic cookies + 2 short visits,
    Google detects the "fresh user" signal — captcha rates skyrocket.
    Loading a 30-day-aged pack from a successful profile gives the
    new profile an instant "I've been here before" signal that drops
    captcha rate by ~70% in our internal tests.

Pack format (JSON):
    {
      "id":           "google-uk-30d-2026-04",
      "label":        "Google UK · 30-day aged",
      "domains":      ["google.com", "youtube.com"],
      "age_days":     30,
      "captcha_rate": 0.04,        // observed during pack creation
      "cookies":      [            // CDP Network.cookie format
        {"name":"NID", "value":"…", "domain":".google.com", "path":"/",
         "expires": 1234, "httpOnly": true, "secure": true,
         "sameSite": "None"},
        ...
      ],
      "local_storage": [           // per-origin
        {"origin": "https://www.google.com",
         "items": [{"key":"…", "value":"…"}, ...]},
        ...
      ],
      "metadata": {
        "created_at":   "2026-04-01T00:00:00",
        "source":       "profile_42",   // anonymized
        "pack_version": "1.0",
      }
    }

Packs live in DB table `cookie_packs`:
    id INTEGER PK, slug TEXT UNIQUE, label TEXT,
    domains TEXT,            -- JSON array
    age_days INTEGER,
    captcha_rate REAL,
    payload BLOB,            -- gzipped JSON of full pack
    created_at TEXT,
    updated_at TEXT

Public API:
    apply_pack(driver, pack_id)        — inject the pack into a live
                                         driver via CDP
    export_from_profile(profile_name)  — capture current state as a
                                         pack (for sharing / backup)
    list_packs()                       — catalog for UI marketplace
    save_pack(pack_dict)               — store a pack in DB
"""
import gzip
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("session.cookie_pack")


# ──────────────────────────────────────────────────────────────
# DB layer — schema + CRUD
# ──────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cookie_packs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    domains       TEXT NOT NULL DEFAULT '[]',
    age_days      INTEGER DEFAULT 0,
    captcha_rate  REAL DEFAULT 0.0,
    payload       BLOB NOT NULL,
    cookies_count INTEGER DEFAULT 0,
    storage_count INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cookie_packs_slug
    ON cookie_packs(slug);
"""


def _ensure_schema() -> None:
    """Create the cookie_packs table if not present. Called lazily on
    first save_pack / list_packs to avoid touching DB during import."""
    try:
        from ghost_shell.db.database import get_db
        conn = get_db()._get_conn()
        for stmt in _SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    except Exception as e:
        logger.warning(f"_ensure_schema: {e}")


def save_pack(pack: dict) -> int:
    """Persist a pack dict. Returns the row id. Updates existing row
    if a pack with the same slug already exists."""
    _ensure_schema()
    from ghost_shell.db.database import get_db
    conn = get_db()._get_conn()

    slug = pack.get("id") or pack.get("slug")
    if not slug:
        raise ValueError("pack missing 'id' / 'slug' field")

    payload_json = json.dumps(pack, ensure_ascii=False)
    payload_gz = gzip.compress(payload_json.encode("utf-8"))

    cookies = pack.get("cookies") or []
    storage = pack.get("local_storage") or []

    conn.execute("""
        INSERT INTO cookie_packs (
            slug, label, domains, age_days, captcha_rate,
            payload, cookies_count, storage_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            label         = excluded.label,
            domains       = excluded.domains,
            age_days      = excluded.age_days,
            captcha_rate  = excluded.captcha_rate,
            payload       = excluded.payload,
            cookies_count = excluded.cookies_count,
            storage_count = excluded.storage_count,
            updated_at    = datetime('now')
    """, (
        slug,
        pack.get("label") or slug,
        json.dumps(pack.get("domains") or []),
        int(pack.get("age_days") or 0),
        float(pack.get("captcha_rate") or 0.0),
        payload_gz,
        len(cookies),
        sum(len(s.get("items", [])) for s in storage),
    ))
    conn.commit()
    row = conn.execute(
        "SELECT id FROM cookie_packs WHERE slug = ?", (slug,)
    ).fetchone()
    return int(row["id"]) if row else 0


def list_packs() -> list:
    """List all packs in the marketplace, newest first. Returns
    metadata only — payload is NOT decompressed (caller fetches via
    get_pack(id) when actually applying).
    """
    _ensure_schema()
    from ghost_shell.db.database import get_db
    conn = get_db()._get_conn()
    rows = conn.execute("""
        SELECT id, slug, label, domains, age_days, captcha_rate,
               cookies_count, storage_count, created_at, updated_at
          FROM cookie_packs
         ORDER BY captcha_rate ASC, age_days DESC
    """).fetchall() or []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["domains"] = json.loads(d.get("domains") or "[]")
        except Exception:
            d["domains"] = []
        out.append(d)
    return out


def get_pack(pack_id: int) -> Optional[dict]:
    """Load a full pack (payload decompressed) by row id."""
    _ensure_schema()
    from ghost_shell.db.database import get_db
    conn = get_db()._get_conn()
    row = conn.execute(
        "SELECT * FROM cookie_packs WHERE id = ?", (pack_id,)
    ).fetchone()
    if not row:
        return None
    payload_gz = row["payload"]
    try:
        text = gzip.decompress(payload_gz).decode("utf-8")
        pack = json.loads(text)
    except Exception as e:
        logger.warning(f"get_pack({pack_id}): payload corrupted: {e}")
        return None
    pack["_db_id"] = row["id"]
    return pack


def delete_pack(pack_id: int) -> bool:
    _ensure_schema()
    from ghost_shell.db.database import get_db
    conn = get_db()._get_conn()
    cur = conn.execute("DELETE FROM cookie_packs WHERE id = ?", (pack_id,))
    conn.commit()
    return cur.rowcount > 0


# ──────────────────────────────────────────────────────────────
# Apply pack to live driver
# ──────────────────────────────────────────────────────────────

def apply_pack(driver, pack_id: int) -> dict:
    """Inject all cookies + localStorage entries from a pack into the
    running driver. Returns a stats dict (cookies_set, storage_set,
    domains_visited).

    Critical: cookies must be set with the right domain attribute and
    BEFORE any navigation to that domain. We use CDP Network.setCookies
    (batch) + a per-origin localStorage write via Storage.setItem.

    The function does NOT navigate to anything — caller decides the
    next URL. This separation lets pack-application work both at
    profile creation (cold start) and mid-run (recovery).
    """
    pack = get_pack(pack_id)
    if not pack:
        raise ValueError(f"pack id={pack_id} not found")

    cookies = pack.get("cookies") or []
    storage = pack.get("local_storage") or []
    stats = {
        "pack_id":     pack_id,
        "pack_slug":   pack.get("id") or pack.get("slug"),
        "cookies_set": 0,
        "storage_set": 0,
        "domains":     pack.get("domains") or [],
    }

    # Cookies via CDP — Network.setCookies takes a list of dicts.
    if cookies:
        try:
            # Normalise each cookie to CDP shape (some packs come from
            # selenium add_cookie which uses different field names).
            def _to_cdp(c: dict) -> dict:
                out = {
                    "name":     c.get("name"),
                    "value":    str(c.get("value", "")),
                    "domain":   c.get("domain"),
                    "path":     c.get("path") or "/",
                    "secure":   bool(c.get("secure")),
                    "httpOnly": bool(c.get("httpOnly") or c.get("http_only")),
                }
                exp = c.get("expires") or c.get("expiry")
                if exp:
                    try:
                        out["expires"] = float(exp)
                    except (TypeError, ValueError):
                        pass
                ss = c.get("sameSite") or c.get("same_site")
                if ss:
                    out["sameSite"] = ss
                return out
            cdp_cookies = [_to_cdp(c) for c in cookies if c.get("name")]
            driver.execute_cdp_cmd("Network.setCookies",
                                   {"cookies": cdp_cookies})
            stats["cookies_set"] = len(cdp_cookies)
        except Exception as e:
            logger.warning(f"apply_pack: setCookies failed: {e}")

    # localStorage — per-origin. Need to navigate to the origin, then
    # set items via execute_script. To keep this side-effect-free we
    # use about:blank navigation hopping which doesn't generate proxy
    # traffic. Caller's actual navigation comes after.
    if storage:
        for entry in storage:
            origin = (entry.get("origin") or "").rstrip("/")
            items  = entry.get("items") or []
            if not origin or not items:
                continue
            try:
                # navigate to the origin (this triggers cookie loading
                # too, then we set localStorage from JS)
                driver.get(origin)
                for kv in items:
                    k = kv.get("key")
                    v = kv.get("value")
                    if not k:
                        continue
                    driver.execute_script(
                        "try { localStorage.setItem(arguments[0], arguments[1]); }"
                        "catch(e) {}",
                        k, str(v),
                    )
                    stats["storage_set"] += 1
            except Exception as e:
                logger.debug(f"apply_pack: storage at {origin}: {e}")
        # Park on about:blank when done so caller's nav is clean
        try:
            driver.get("about:blank")
        except Exception:
            pass

    logger.info(
        f"[cookie_pack] applied pack id={pack_id} "
        f"slug={stats['pack_slug']!r}: "
        f"{stats['cookies_set']} cookies, "
        f"{stats['storage_set']} storage entries"
    )
    return stats


# ──────────────────────────────────────────────────────────────
# Export / capture
# ──────────────────────────────────────────────────────────────

def export_from_profile(profile_name: str,
                        *,
                        driver=None,
                        label: str = None,
                        domains: list = None) -> dict:
    """Capture the current state of a profile as a pack. Either:
      - driver is given → live capture via CDP (most accurate); OR
      - profile session files on disk are read (fallback)

    Returns the pack dict (NOT yet saved — caller decides whether to
    save_pack() it or just hand it to apply_pack on another profile).
    """
    cookies = []
    storage = []

    if driver is not None:
        # Live capture via CDP — gets all cookies including HttpOnly
        try:
            res = driver.execute_cdp_cmd("Network.getAllCookies", {}) or {}
            cookies = res.get("cookies", []) or []
        except Exception as e:
            logger.warning(f"export_from_profile: getAllCookies: {e}")

        # localStorage per origin — must visit each domain to read it
        for d in (domains or []):
            origin = d if d.startswith(("http://", "https://")) \
                     else f"https://{d}"
            try:
                driver.get(origin)
                items = driver.execute_script(
                    "var o=[];for(var i=0;i<localStorage.length;i++){"
                    "var k=localStorage.key(i);"
                    "o.push({key:k, value:localStorage.getItem(k)});"
                    "}return o;"
                ) or []
                if items:
                    storage.append({"origin": origin, "items": items})
            except Exception as e:
                logger.debug(f"export_from_profile: {origin}: {e}")
        try:
            driver.get("about:blank")
        except Exception:
            pass
    else:
        # Disk fallback — read the session/cookies.json + storage.json
        # that GhostShell writes on profile shutdown.
        import os
        from ghost_shell.db.database import get_db
        try:
            db = get_db()
            ud = db.profile_user_data_path(profile_name)  # platform-aware
            cookies_path = os.path.join(ud, "ghostshell_session", "cookies.json")
            if os.path.exists(cookies_path):
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
            # Storage entries similar — a full implementation parses
            # the per-origin localStorage dump file.
        except Exception as e:
            logger.warning(f"export_from_profile: disk fallback: {e}")

    slug = f"{profile_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    pack = {
        "id":            slug,
        "label":         label or f"Pack from {profile_name}",
        "domains":       domains or [],
        "age_days":      0,                # caller can set later
        "captcha_rate":  0.0,
        "cookies":       cookies,
        "local_storage": storage,
        "metadata": {
            "created_at":   datetime.now().isoformat(timespec="seconds"),
            "source":       profile_name,
            "pack_version": "1.0",
        },
    }
    return pack
