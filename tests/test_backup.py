"""Tests for ghost_shell/profile/backup.py — encrypted bundle format
+ create/restore round-trip."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Clean DB instance for backup tests.

    DB._local is class-level threading.local() — shared across every
    instance. We MUST wipe its cached connection so the next test's
    seed helpers don't try to use the previous test's closed conn."""
    monkeypatch.setattr(
        "ghost_shell.core.platform_paths.PROJECT_ROOT", str(tmp_path)
    )
    import ghost_shell.db.database as db_mod
    # Same module-import gotcha as conftest.in_memory_db: DB_PATH
    # is read once at import. Without this, fresh_db hits the same
    # ghost_shell.db file every test → UNIQUE name collisions.
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GHOST_SHELL_DB", str(db_path))
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    # The actual module-level singleton is ``_db_instance`` (lowercase).
    # Reset both names for forward-compat.
    db_mod._db_instance = None
    if hasattr(db_mod, "_DB_INSTANCE"):
        db_mod._DB_INSTANCE = None
    if hasattr(db_mod, "_DB"):
        db_mod._DB = None
    # Wipe thread-local connection cache so a new connection is opened
    # against the new tmp_path-scoped DB file.
    if hasattr(db_mod, "DB") and hasattr(db_mod.DB, "_local"):
        if hasattr(db_mod.DB._local, "conn"):
            try:
                db_mod.DB._local.conn.close()
            except Exception:
                pass
            try:
                delattr(db_mod.DB._local, "conn")
            except Exception:
                pass
    db = db_mod.get_db()
    yield db
    # Mirror cleanup on teardown so the next test (regardless of which
    # fixture it uses) gets a fresh start.
    try:
        if hasattr(db, "_get_conn"):
            db._get_conn().close()
    except Exception:
        pass
    if hasattr(db_mod, "DB") and hasattr(db_mod.DB, "_local"):
        try:
            delattr(db_mod.DB._local, "conn")
        except Exception:
            pass
    db_mod._db_instance = None
    if hasattr(db_mod, "_DB_INSTANCE"):
        db_mod._DB_INSTANCE = None


def _seed_profile(db, name="testprof"):
    """Insert a minimal profile + a fingerprint + a vault item."""
    conn = db._get_conn()
    conn.execute("INSERT INTO profiles (name, created_at, ready_at) "
                 "VALUES (?, datetime('now'), datetime('now'))", (name,))
    conn.execute("INSERT INTO fingerprints "
                 "(profile_name, timestamp, payload_json, is_current) "
                 "VALUES (?, datetime('now'), '{\"foo\":\"bar\"}', 1)",
                 (name,))
    conn.execute("INSERT INTO vault_items "
                 "(name, kind, profile_name, secrets_enc, created_at, updated_at) "
                 "VALUES ('test-cred', 'account', ?, 'fakecipher', "
                 "datetime('now'), datetime('now'))", (name,))
    conn.commit()


# ── header pack/unpack ──────────────────────────────────────

def test_pack_header_round_trip():
    from ghost_shell.profile.backup import (
        _pack_header, _unpack_header, KDF_SALT_LEN, MAGIC, SCHEMA_VERSION,
    )
    salt = b"\x01" * KDF_SALT_LEN
    blob = _pack_header(salt, payload_len=12345)
    hdr = _unpack_header(blob)
    assert hdr["magic"] == MAGIC.decode("ascii")
    assert hdr["version"] == SCHEMA_VERSION
    assert hdr["kdf_salt"] == salt
    assert hdr["payload_len"] == 12345


def test_unpack_header_bad_magic():
    from ghost_shell.profile.backup import (
        _unpack_header, BundleFormatError, KDF_SALT_LEN,
    )
    blob = struct.pack(">4sH16sI", b"XXXX", 1, b"\x00" * 16, 0)
    with pytest.raises(BundleFormatError, match="bad magic"):
        _unpack_header(blob)


def test_unpack_header_bad_version():
    from ghost_shell.profile.backup import (
        _pack_header, _unpack_header, BundleFormatError,
        KDF_SALT_LEN, MAGIC,
    )
    blob = struct.pack(">4sH16sI", MAGIC, 999, b"\x00" * KDF_SALT_LEN, 0)
    with pytest.raises(BundleFormatError, match="unsupported bundle version"):
        _unpack_header(blob)


def test_unpack_header_truncated():
    from ghost_shell.profile.backup import _unpack_header, BundleFormatError
    with pytest.raises(BundleFormatError, match="truncated"):
        _unpack_header(b"GHSB")


# ── inspect_bundle ──────────────────────────────────────────

def test_inspect_bundle_returns_metadata():
    from ghost_shell.profile.backup import (
        _pack_header, inspect_bundle, KDF_SALT_LEN,
    )
    salt = b"\x05" * KDF_SALT_LEN
    fake_payload = b"x" * 100
    blob = _pack_header(salt, len(fake_payload)) + fake_payload
    info = inspect_bundle(blob)
    assert info["magic"] == "GHSB"
    assert info["version"] == 1
    assert info["payload_size"] == 100
    assert info["total_size_bytes"] == len(blob)


def test_inspect_bundle_rejects_corrupt():
    from ghost_shell.profile.backup import inspect_bundle, BundleFormatError
    with pytest.raises(BundleFormatError):
        inspect_bundle(b"not a bundle")


# ── encrypt / decrypt round-trip ─────────────────────────────

def test_encrypt_decrypt_round_trip():
    from ghost_shell.profile.backup import (
        _encrypt_payload, _decrypt_payload, KDF_SALT_LEN,
    )
    salt = os.urandom(KDF_SALT_LEN)
    plaintext = b"hello, ghost shell"
    cipher = _encrypt_payload(plaintext, "secret-pw", salt)
    assert cipher != plaintext
    decoded = _decrypt_payload(cipher, "secret-pw", salt)
    assert decoded == plaintext


def test_decrypt_wrong_password_raises_auth_error():
    from ghost_shell.profile.backup import (
        _encrypt_payload, _decrypt_payload, BundleAuthError, KDF_SALT_LEN,
    )
    salt = os.urandom(KDF_SALT_LEN)
    cipher = _encrypt_payload(b"top secret", "good-pw", salt)
    with pytest.raises(BundleAuthError):
        _decrypt_payload(cipher, "wrong-pw", salt)


def test_decrypt_wrong_salt_raises_auth_error():
    """Same password but different salt → wrong key → InvalidToken."""
    from ghost_shell.profile.backup import (
        _encrypt_payload, _decrypt_payload, BundleAuthError, KDF_SALT_LEN,
    )
    salt1 = b"\x01" * KDF_SALT_LEN
    salt2 = b"\x02" * KDF_SALT_LEN
    cipher = _encrypt_payload(b"data", "pw", salt1)
    with pytest.raises(BundleAuthError):
        _decrypt_payload(cipher, "pw", salt2)


# ── full create / restore round-trip ────────────────────────

def test_create_then_inspect(fresh_db):
    from ghost_shell.profile.backup import create_bundle, inspect_bundle
    _seed_profile(fresh_db, "p_inspect")
    blob = create_bundle("p_inspect", master_password="hunter2")
    assert blob.startswith(b"GHSB")
    info = inspect_bundle(blob)
    assert info["magic"] == "GHSB"
    assert info["version"] == 1
    assert info["payload_size"] > 0


def test_create_unknown_profile_raises():
    from ghost_shell.profile.backup import create_bundle
    with pytest.raises(ValueError, match="not found"):
        create_bundle("nope_does_not_exist", master_password="x")


def test_create_empty_password_raises():
    from ghost_shell.profile.backup import create_bundle
    with pytest.raises(ValueError, match="master_password"):
        create_bundle("anything", master_password="")


def test_round_trip_restores_profile(fresh_db, tmp_path):
    from ghost_shell.profile.backup import create_bundle, restore_bundle
    _seed_profile(fresh_db, "src_profile")
    blob = create_bundle("src_profile", master_password="round-trip-pw")

    # Restore as a different name to avoid collision
    target_dir = tmp_path / "profiles" / "restored_profile"
    summary = restore_bundle(
        blob,
        master_password="round-trip-pw",
        target_profile_name="restored_profile",
        target_profile_dir=str(target_dir),
    )
    assert summary["ok"] is True
    assert summary["source_name"] == "src_profile"
    assert summary["target_name"] == "restored_profile"
    assert summary["written"]["profiles"] == 1
    assert summary["written"]["fingerprints"] == 1
    assert summary["written"]["vault_items"] == 1

    # Verify rows landed in DB
    conn = fresh_db._get_conn()
    p = conn.execute("SELECT name FROM profiles WHERE name = ?",
                     ("restored_profile",)).fetchone()
    assert p is not None


def test_restore_wrong_password_raises_auth_error(fresh_db):
    from ghost_shell.profile.backup import (
        create_bundle, restore_bundle, BundleAuthError,
    )
    _seed_profile(fresh_db, "src_pw")
    blob = create_bundle("src_pw", master_password="correct")
    with pytest.raises(BundleAuthError):
        restore_bundle(
            blob,
            master_password="incorrect",
            target_profile_name="restore_pw_test",
        )


def test_restore_corrupt_bundle_raises_format_error():
    from ghost_shell.profile.backup import (
        restore_bundle, BundleFormatError,
    )
    with pytest.raises(BundleFormatError):
        restore_bundle(b"not_a_bundle", master_password="x")


def test_restore_collision_without_overwrite_raises(fresh_db):
    from ghost_shell.profile.backup import create_bundle, restore_bundle
    _seed_profile(fresh_db, "collision_src")
    blob = create_bundle("collision_src", master_password="pw")
    # Restore back to the same name without overwrite → should error
    with pytest.raises(ValueError, match="already exists"):
        restore_bundle(blob, master_password="pw")


def test_restore_collision_with_overwrite_succeeds(fresh_db):
    from ghost_shell.profile.backup import create_bundle, restore_bundle
    _seed_profile(fresh_db, "overw_src")
    blob = create_bundle("overw_src", master_password="pw")
    summary = restore_bundle(blob, master_password="pw", overwrite=True)
    assert summary["ok"] is True
    assert any("overwrote" in w for w in summary["warnings"])


# ── path-traversal tar safety ──────────────────────────────

def test_unpack_userdir_rejects_path_traversal(tmp_path):
    """Tar entries with .. in the name shouldn't escape target dir."""
    import io
    import tarfile
    from ghost_shell.profile.backup import _unpack_user_data_dir

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Inject a malicious entry
        info = tarfile.TarInfo(name="../../../etc/escaped")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"bad!\n"))
        # And a normal one
        info2 = tarfile.TarInfo(name="ok.txt")
        info2.size = 5
        tf.addfile(info2, io.BytesIO(b"safe\n"))

    target = tmp_path / "restore_target"
    n = _unpack_user_data_dir(buf.getvalue(), str(target))
    # Only the "ok.txt" entry should have made it
    assert n == 1
    assert (target / "ok.txt").exists()
    # Note: the malicious-arcname extractor's path-traversal
    # guard already rejected the bad entries above. We don't probe
    # the filesystem outside the test root because the realpath
    # check inside _unpack_user_data_dir wouldn't have written
    # anywhere a normal user wouldn't expect.


def test_pack_userdir_empty_when_dir_missing(tmp_path):
    """Source profile dir doesn't exist → empty bytes, no exception.
    Caller stores user_data_dir_b64=None in the bundle on this path."""
    from ghost_shell.profile.backup import _pack_user_data_dir
    nonexistent = tmp_path / "definitely-not-here"
    assert _pack_user_data_dir(str(nonexistent)) == b""
