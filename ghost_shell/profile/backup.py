"""
backup.py — Encrypted backup / restore for a profile.

Sprint 7 — agency-grade portability + disaster recovery. Lets a user
take their entire profile (DB rows + user-data-dir + vault items
referencing the profile + cookie pool entries) and pack it into a
single encrypted ``.ghs-bundle`` file. Restore on a different machine
yields the same browser identity — same fingerprint, same cookies,
same vault credentials.

Use cases
─────────
* **Disaster recovery**: SSD dies, backup restored on new hardware,
  workflow continues with no captcha re-warming.
* **Multi-machine workflow**: build profile on workstation, run from
  laptop while traveling.
* **Agency operations**: lead operator prepares a profile package,
  hands the .ghs-bundle to a junior operator who restores into their
  Ghost Shell install.
* **Privacy archival**: encrypted backup of a research session that
  can be reopened years later.

Bundle format v1
────────────────
::

    [4 bytes ] magic            "GHSB"
    [2 bytes ] version          0x00 0x01  (big-endian uint16)
    [16 bytes] kdf_salt         random per-bundle, fed to PBKDF2
    [4 bytes ] payload_len      uint32 big-endian
    [N bytes ] payload          Fernet token (already URL-safe base64
                                 internally), wraps a JSON+binary blob

The plaintext payload is a JSON object with these top-level keys:

    "schema":            "ghost_shell_bundle_v1"
    "created_at":        ISO timestamp
    "source_host":       hostname of the source machine (informational)
    "profile_name":      original name on source — restore can rename
    "profile_row":       full profiles table row
    "fingerprints":      list of fingerprint history rows
    "vault_items":       vault rows (already Fernet-encrypted; same
                         master will decrypt on the destination)
    "extensions":        list of {extension_id, enabled} assignments —
                         the actual extension pool isn't bundled (it's
                         a shared resource, restore hooks into the
                         destination's pool).
    "cookie_snapshots":  pool entries referencing this profile
    "profile_health":    canary timeline rows
    "user_data_dir_b64": base64-encoded gzipped tar of <profiles/<name>/Default>

The MASTER PASSWORD used at backup-create-time is required to
restore. We do NOT store password hints, recovery codes, or anything
else in the bundle — lose the master, lose the bundle.

Public API
──────────
  create_bundle(profile_name: str,
                master_password: str) -> bytes
      # Returns the bundle as bytes. Caller decides where to write —
      # local file, cloud upload, etc.

  restore_bundle(bundle_bytes: bytes,
                 master_password: str,
                 target_profile_name: str = None) -> dict
      # Decrypts + parses + writes everything to DB and disk.
      # target_profile_name overrides the bundle's recorded name
      # (so users can restore into "profile_01_backup" without
      # collision). Returns a summary {written: {...}, warnings: [...]}.

  inspect_bundle(bundle_bytes: bytes) -> dict
      # Returns the unencrypted header (magic, version, salt,
      # payload size). Lets a UI surface "this is a v1 bundle, ~12MB
      # encrypted" before asking the user for a password.

Errors raised:
  BundleFormatError    bundle bytes don't match magic / version we know
  BundleAuthError      master password failed to decrypt the payload
                       (Fernet InvalidToken). Wrap so callers don't
                       have to import cryptography exception types.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import base64
import gzip
import io
import json
import logging
import os
import socket
import struct
import tarfile
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

MAGIC          = b"GHSB"            # 4 bytes
SCHEMA_VERSION = 1                  # bumped when bundle layout changes
KDF_SALT_LEN   = 16
KDF_ITERATIONS = 200_000            # matches vault.py for parity
SCHEMA_TAG     = "ghost_shell_bundle_v1"

# Header layout (8 + 16 + 4 = 28 bytes before the payload)
_HEADER_FMT    = ">4sH16sI"
_HEADER_LEN    = struct.calcsize(_HEADER_FMT)


# ──────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────

class BundleFormatError(Exception):
    """Bundle bytes don't match our magic / version — corrupt file or
    a future-version bundle this Ghost Shell can't read yet."""


class BundleAuthError(Exception):
    """Master password failed to decrypt the payload. The bundle may
    be valid but the user gave the wrong password."""


# ──────────────────────────────────────────────────────────────
# Crypto helpers — wrap cryptography library so callers don't
# need to import it
# ──────────────────────────────────────────────────────────────

def _import_crypto():
    """Lazy import to keep module-load cheap when backup isn't used."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as e:
        raise RuntimeError(
            "cryptography library required for backup module. "
            "pip install cryptography>=42"
        ) from e
    return Fernet, InvalidToken, hashes, PBKDF2HMAC


def _derive_fernet_key(master_password: str, salt: bytes) -> bytes:
    """Same KDF as vault.py — PBKDF2-HMAC-SHA256, 200k iterations.
    Produces a 32-byte URL-safe-base64 Fernet key."""
    Fernet, _, hashes, PBKDF2HMAC = _import_crypto()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))


def _encrypt_payload(plaintext: bytes, master_password: str,
                     salt: bytes) -> bytes:
    Fernet, _, _, _ = _import_crypto()
    key = _derive_fernet_key(master_password, salt)
    return Fernet(key).encrypt(plaintext)


def _decrypt_payload(ciphertext: bytes, master_password: str,
                     salt: bytes) -> bytes:
    Fernet, InvalidToken, _, _ = _import_crypto()
    key = _derive_fernet_key(master_password, salt)
    try:
        return Fernet(key).decrypt(ciphertext)
    except InvalidToken as e:
        raise BundleAuthError("master password failed to decrypt — "
                              "wrong password or bundle tampering") from e


# ──────────────────────────────────────────────────────────────
# Header serialization
# ──────────────────────────────────────────────────────────────

def _pack_header(salt: bytes, payload_len: int) -> bytes:
    if len(salt) != KDF_SALT_LEN:
        raise ValueError(f"salt must be {KDF_SALT_LEN} bytes, got {len(salt)}")
    return struct.pack(_HEADER_FMT, MAGIC, SCHEMA_VERSION, salt, payload_len)


def _unpack_header(blob: bytes) -> dict:
    if len(blob) < _HEADER_LEN:
        raise BundleFormatError(
            f"bundle truncated: only {len(blob)} bytes, header needs "
            f"{_HEADER_LEN}"
        )
    magic, ver, salt, payload_len = struct.unpack(
        _HEADER_FMT, blob[:_HEADER_LEN]
    )
    if magic != MAGIC:
        raise BundleFormatError(
            f"bad magic: got {magic!r}, expected {MAGIC!r}. "
            f"This isn't a Ghost Shell bundle."
        )
    if ver != SCHEMA_VERSION:
        raise BundleFormatError(
            f"unsupported bundle version {ver} — this Ghost Shell "
            f"reads v{SCHEMA_VERSION}. Upgrade Ghost Shell or use the "
            f"bundle on a matching version."
        )
    return {
        "magic":       magic.decode("ascii"),
        "version":     ver,
        "kdf_salt":    salt,
        "payload_len": payload_len,
    }


# ──────────────────────────────────────────────────────────────
# user-data-dir packing — gzipped tar in memory
# ──────────────────────────────────────────────────────────────

# Subset of files / dirs we include. The full user-data-dir can be
# 100s of MB of caches we don't need. These are the ones that carry
# session continuity.
_USERDIR_KEEP = {
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Local Extension Settings",
    "Default/Storage",
    "Default/Preferences",
    "Default/Login Data",
    "Default/Login Data-journal",
    "Default/Web Data",
    "Default/History",
    "Default/Bookmarks",
    "Local State",
}


def _pack_user_data_dir(profile_dir: str) -> bytes:
    """Tar+gzip the keepable subset of profile_dir. Returns raw
    compressed bytes. Empty bytes if profile_dir doesn't exist."""
    if not profile_dir or not os.path.isdir(profile_dir):
        return b""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for keep in _USERDIR_KEEP:
            full = os.path.join(profile_dir, keep)
            if not os.path.exists(full):
                continue
            arcname = keep.replace(os.sep, "/")
            try:
                tf.add(full, arcname=arcname, recursive=True)
            except OSError as e:
                logging.warning(f"[backup] skipping {keep}: {e}")
    return buf.getvalue()


def _unpack_user_data_dir(tar_bytes: bytes, target_profile_dir: str) -> int:
    """Extract tar bytes into target_profile_dir. Returns count of
    entries extracted. Skips entries that would write outside the
    target dir (path-traversal guard)."""
    if not tar_bytes:
        return 0
    os.makedirs(target_profile_dir, exist_ok=True)
    target_real = os.path.realpath(target_profile_dir)
    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            # Path-traversal defence: refuse absolute paths and ..
            if (member.name.startswith("/") or
                ".." in member.name.split("/")):
                logging.warning(
                    f"[backup] refusing suspicious tar entry: {member.name}"
                )
                continue
            dest = os.path.realpath(os.path.join(target_profile_dir, member.name))
            if not dest.startswith(target_real + os.sep) and dest != target_real:
                logging.warning(
                    f"[backup] tar entry escapes target: {member.name}"
                )
                continue
            try:
                tf.extract(member, target_profile_dir)
                extracted += 1
            except OSError as e:
                logging.warning(f"[backup] extract failed {member.name}: {e}")
    return extracted


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

def inspect_bundle(bundle_bytes: bytes) -> dict:
    """Read the unencrypted header. Doesn't require master password.

    Useful for the dashboard's restore UI — show "v1 bundle, 12 MB
    encrypted" before asking for the password."""
    hdr = _unpack_header(bundle_bytes)
    return {
        "magic":             hdr["magic"],
        "version":           hdr["version"],
        "kdf_salt_b64":      base64.b64encode(hdr["kdf_salt"]).decode("ascii"),
        "payload_size":      hdr["payload_len"],
        "total_size_bytes":  len(bundle_bytes),
    }


def create_bundle(profile_name: str,
                  master_password: str,
                  db=None,
                  profile_dir: Optional[str] = None) -> bytes:
    """Pack everything attached to ``profile_name`` into an encrypted
    bundle. Returns raw bytes — caller decides where to persist them.

    Args:
      profile_name:    name as it exists in the profiles table.
      master_password: encryption password. Same one used to unlock
                       the vault → vault items inside the bundle stay
                       decryptable on restore via the same password.
      db:              optional DB handle override. Defaults to
                       ``ghost_shell.db.database.get_db()``.
      profile_dir:     optional path to user-data-dir. Defaults to
                       ``<PROJECT_ROOT>/profiles/<profile_name>``.

    Raises:
      ValueError       if profile doesn't exist in DB.
      RuntimeError     if cryptography isn't available.
    """
    if not master_password:
        raise ValueError("master_password is required")

    if db is None:
        from ghost_shell.db.database import get_db
        db = get_db()

    # Gather everything from DB
    conn = db._get_conn()
    profile_row = conn.execute(
        "SELECT * FROM profiles WHERE name = ?", (profile_name,)
    ).fetchone()
    if profile_row is None:
        raise ValueError(f"profile {profile_name!r} not found in DB")

    fp_rows = conn.execute(
        "SELECT * FROM fingerprints WHERE profile_name = ? "
        "ORDER BY id ASC", (profile_name,)
    ).fetchall()

    vault_rows = conn.execute(
        "SELECT * FROM vault_items WHERE profile_name = ?", (profile_name,)
    ).fetchall()

    ext_rows = conn.execute(
        "SELECT * FROM profile_extensions WHERE profile_name = ?",
        (profile_name,)
    ).fetchall()

    cookie_rows = conn.execute(
        "SELECT * FROM cookie_snapshots WHERE profile_name = ? "
        "ORDER BY id ASC", (profile_name,)
    ).fetchall()

    health_rows = conn.execute(
        "SELECT * FROM profile_health WHERE profile_name = ? "
        "ORDER BY id ASC", (profile_name,)
    ).fetchall()

    # User-data-dir (optional — fresh profile may not have one)
    if profile_dir is None:
        from ghost_shell.core.platform_paths import PROJECT_ROOT
        profile_dir = os.path.join(PROJECT_ROOT, "profiles", profile_name)
    user_data_blob = _pack_user_data_dir(profile_dir)

    # Build the JSON payload
    payload_obj = {
        "schema":            SCHEMA_TAG,
        "created_at":        datetime.now().isoformat(timespec="seconds"),
        "source_host":       socket.gethostname(),
        "profile_name":      profile_name,
        "profile_row":       dict(profile_row) if profile_row else None,
        "fingerprints":      [dict(r) for r in fp_rows],
        "vault_items":       [dict(r) for r in vault_rows],
        "extensions":        [dict(r) for r in ext_rows],
        "cookie_snapshots":  [dict(r) for r in cookie_rows],
        "profile_health":    [dict(r) for r in health_rows],
        "user_data_dir_b64": (base64.b64encode(user_data_blob).decode("ascii")
                              if user_data_blob else None),
        "user_data_dir_size": len(user_data_blob),
    }
    plaintext = json.dumps(payload_obj, default=str).encode("utf-8")

    # Encrypt
    salt = os.urandom(KDF_SALT_LEN)
    ciphertext = _encrypt_payload(plaintext, master_password, salt)

    # Pack: header || ciphertext
    header = _pack_header(salt, len(ciphertext))
    return header + ciphertext


def restore_bundle(bundle_bytes: bytes,
                   master_password: str,
                   target_profile_name: Optional[str] = None,
                   db=None,
                   target_profile_dir: Optional[str] = None,
                   overwrite: bool = False) -> dict:
    """Reverse of create_bundle. Decrypts, parses, writes DB rows and
    extracts user-data-dir.

    Args:
      target_profile_name: rename on restore (avoids collision with
                           an existing profile of the same name).
                           None = use bundle's recorded name.
      overwrite:           when True, replaces existing rows for the
                           target name. When False (default), raises
                           if the target name already exists.

    Returns: dict with restoration summary:
        {
          "ok":           True,
          "schema":       "ghost_shell_bundle_v1",
          "source_host":  "...",
          "source_name":  "<original profile_name>",
          "target_name":  "<actual destination name>",
          "written":      {table: rowcount, ...},
          "user_data_extracted": int,
          "warnings":     [str, ...],
        }
    """
    hdr = _unpack_header(bundle_bytes)
    salt        = hdr["kdf_salt"]
    payload_len = hdr["payload_len"]
    ciphertext  = bundle_bytes[_HEADER_LEN:_HEADER_LEN + payload_len]
    if len(ciphertext) != payload_len:
        raise BundleFormatError(
            f"payload truncated: header says {payload_len} bytes, "
            f"got {len(ciphertext)}"
        )

    plaintext = _decrypt_payload(ciphertext, master_password, salt)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BundleFormatError(
            f"decrypted payload is not valid JSON: {e}"
        ) from e

    if payload.get("schema") != SCHEMA_TAG:
        raise BundleFormatError(
            f"unknown schema tag {payload.get('schema')!r}"
        )

    source_name = payload.get("profile_name") or "?"
    target_name = target_profile_name or source_name

    if db is None:
        from ghost_shell.db.database import get_db
        db = get_db()

    warnings = []
    written = {}

    # Collision check
    conn = db._get_conn()
    exists = conn.execute(
        "SELECT 1 FROM profiles WHERE name = ?", (target_name,)
    ).fetchone()
    if exists and not overwrite:
        raise ValueError(
            f"profile {target_name!r} already exists. Pass "
            f"target_profile_name=<new_name> or overwrite=True."
        )
    if exists and overwrite:
        # Use cascade-cleanup before re-inserting
        try:
            db.profile_delete_cascade(target_name)
            warnings.append(f"overwrote existing profile {target_name!r}")
        except Exception as e:
            warnings.append(f"cascade cleanup failed: {e}")

    # Restore profiles row
    pr = payload.get("profile_row") or {}
    if pr:
        # Normalize: rename + drop autoincrement IDs
        pr_clean = {k: v for k, v in pr.items()
                    if k != "id" and v is not None}
        pr_clean["name"] = target_name
        cols = ", ".join(pr_clean.keys())
        placeholders = ", ".join("?" for _ in pr_clean)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO profiles ({cols}) VALUES ({placeholders})",
                list(pr_clean.values()),
            )
            written["profiles"] = 1
        except Exception as e:
            warnings.append(f"profiles insert failed: {e}")
            written["profiles"] = 0

    # Helper to bulk-restore a list of rows into a table, rewriting
    # the profile_name column to target_name
    def _restore_rows(table: str, rows: list, drop_id: bool = True) -> int:
        if not rows:
            return 0
        n_inserted = 0
        for r in rows:
            r_clean = dict(r)
            if drop_id:
                r_clean.pop("id", None)
            if "profile_name" in r_clean:
                r_clean["profile_name"] = target_name
            cols = ", ".join(r_clean.keys())
            placeholders = ", ".join("?" for _ in r_clean)
            try:
                conn.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    list(r_clean.values()),
                )
                n_inserted += 1
            except Exception as e:
                warnings.append(f"{table} insert failed: {e}")
        return n_inserted

    written["fingerprints"]     = _restore_rows("fingerprints",     payload.get("fingerprints")     or [])
    written["vault_items"]      = _restore_rows("vault_items",      payload.get("vault_items")      or [])
    written["profile_extensions"] = _restore_rows("profile_extensions", payload.get("extensions")    or [], drop_id=False)
    written["cookie_snapshots"] = _restore_rows("cookie_snapshots", payload.get("cookie_snapshots") or [])
    written["profile_health"]   = _restore_rows("profile_health",   payload.get("profile_health")   or [])

    conn.commit()

    # Extract user-data-dir
    user_data_extracted = 0
    udb64 = payload.get("user_data_dir_b64")
    if udb64:
        try:
            tar_bytes = base64.b64decode(udb64)
            if target_profile_dir is None:
                from ghost_shell.core.platform_paths import PROJECT_ROOT
                target_profile_dir = os.path.join(
                    PROJECT_ROOT, "profiles", target_name
                )
            user_data_extracted = _unpack_user_data_dir(
                tar_bytes, target_profile_dir
            )
        except Exception as e:
            warnings.append(f"user-data-dir extract failed: {e}")

    return {
        "ok":                  True,
        "schema":              payload.get("schema"),
        "source_host":         payload.get("source_host"),
        "source_name":         source_name,
        "target_name":         target_name,
        "written":             written,
        "user_data_extracted": user_data_extracted,
        "warnings":            warnings,
    }
