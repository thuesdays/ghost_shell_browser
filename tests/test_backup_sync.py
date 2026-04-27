"""Sprint 8.2 — tests for ghost_shell/profile/backup_sync.py.

Network adapters (S3 / Dropbox / SFTP) aren't covered here — those
require live SDKs + real targets. We test the bits that don't need
the network:

* make_key / parse_key round-trip + edge cases
* _safe_segment squashes weird characters
* apply_retention's policy with a fake target
* target_from_config factory dispatch
* push_profile / pull_profile high-level orchestration with a fake
  target so we can verify ordering (push → retention) and key
  resolution (host + profile_name + stamp → bytes).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────
# In-memory fake target for orchestration tests
# ──────────────────────────────────────────────────────────────

class _FakeTarget:
    """Stores blobs in a dict keyed by their key. Mirrors the
    SyncTarget contract just enough for the orchestration tests."""

    name = "fake"

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.deleted: List[str] = []
        self.push_calls: List[dict] = []

    def push(self, key, blob, metadata=None):
        from ghost_shell.profile.backup_sync import BundleEntry  # noqa
        self.store[key] = blob
        self.push_calls.append({"key": key, "size": len(blob),
                                "metadata": dict(metadata or {})})
        return {"size": len(blob), "etag": "fake"}

    def pull(self, key):
        from ghost_shell.profile.backup_sync import SyncTransportError
        if key not in self.store:
            raise SyncTransportError(f"missing {key}")
        return self.store[key]

    def list(self, prefix=""):
        from ghost_shell.profile.backup_sync import (
            BundleEntry, parse_key,
        )
        out = []
        for k, v in self.store.items():
            if prefix and not k.startswith(prefix):
                continue
            parsed = parse_key(k) or {}
            out.append(BundleEntry(
                key=k,
                size=len(v),
                last_modified=parsed.get("ts"),
                parsed=parsed,
            ))
        return out

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)

    def ping(self):
        return {"ok": True}


# ──────────────────────────────────────────────────────────────
# make_key / parse_key
# ──────────────────────────────────────────────────────────────

def test_make_key_uses_canonical_layout():
    from ghost_shell.profile.backup_sync import make_key, KEY_PREFIX
    ts = datetime(2026, 4, 27, 14, 30, 5)
    key = make_key("workstation-1", "myprof", ts=ts)
    assert key == f"{KEY_PREFIX}/workstation-1/myprof/20260427-143005.ghs-bundle"


def test_make_key_sanitises_dangerous_segments():
    """A profile name with slashes shouldn't escape into a sibling
    folder. Same for hosts."""
    from ghost_shell.profile.backup_sync import make_key
    ts = datetime(2026, 1, 1)
    key = make_key("host/with/slashes", "../escape", ts=ts)
    # Slashes get squashed; '..' is replaced segment-by-segment but
    # the dots inside the segment survive (they're allowed).
    assert "/" not in key.split("/")[1]   # host segment
    assert "/" not in key.split("/")[2]   # profile segment
    # Whole key must still have exactly the expected 4 path parts
    assert key.count("/") == 3


def test_parse_key_round_trip():
    from ghost_shell.profile.backup_sync import make_key, parse_key
    ts = datetime(2026, 4, 27, 9, 15, 0)
    key = make_key("hostA", "profB", ts=ts)
    parsed = parse_key(key)
    assert parsed is not None
    assert parsed["host"] == "hostA"
    assert parsed["profile_name"] == "profB"
    assert parsed["stamp"] == "20260427-091500"
    assert parsed["ts"] == ts


def test_parse_key_rejects_alien():
    from ghost_shell.profile.backup_sync import parse_key
    assert parse_key("something/else.zip") is None
    assert parse_key("gs-backups/host/profile") is None  # missing fname
    assert parse_key("") is None
    assert parse_key(None) is None


def test_parse_key_returns_none_ts_for_malformed_stamp():
    """parse_key shouldn't crash on a bundle with a manually-edited
    filename — stamp parse failure leaves ts=None."""
    from ghost_shell.profile.backup_sync import parse_key, KEY_PREFIX
    parsed = parse_key(f"{KEY_PREFIX}/h/p/not-a-stamp.ghs-bundle")
    assert parsed is not None
    assert parsed["ts"] is None
    assert parsed["stamp"] == "not-a-stamp"


# ──────────────────────────────────────────────────────────────
# _safe_segment
# ──────────────────────────────────────────────────────────────

def test_safe_segment_squashes_problematic_chars():
    from ghost_shell.profile.backup_sync import _safe_segment
    assert _safe_segment("a b c") == "a-b-c"
    assert _safe_segment("a/b/c") == "a-b-c"
    assert _safe_segment("a@b#c") == "a-b-c"


def test_safe_segment_keeps_safe_chars():
    from ghost_shell.profile.backup_sync import _safe_segment
    assert _safe_segment("alpha-Beta_1.0") == "alpha-Beta_1.0"


def test_safe_segment_empty_returns_underscore():
    from ghost_shell.profile.backup_sync import _safe_segment
    assert _safe_segment("") == "_"


# ──────────────────────────────────────────────────────────────
# apply_retention
# ──────────────────────────────────────────────────────────────

def _seed_fake_bundles(target, host, profile, n, base_ts=None):
    """Insert n fake bundles, oldest→newest with 1-hour gaps."""
    from ghost_shell.profile.backup_sync import make_key
    base = base_ts or datetime(2026, 4, 1, 0, 0, 0)
    keys = []
    for i in range(n):
        ts = base + timedelta(hours=i)
        k = make_key(host, profile, ts=ts)
        target.store[k] = f"bundle-{i}".encode()
        keys.append(k)
    return keys


def test_retention_noop_under_threshold():
    """Fewer bundles than keep_last → nothing deleted."""
    from ghost_shell.profile.backup_sync import apply_retention
    t = _FakeTarget()
    _seed_fake_bundles(t, "h", "p", 3)
    deleted = apply_retention(t, "h", "p", keep_last=5)
    assert deleted == []
    assert len(t.store) == 3


def test_retention_keeps_newest_n():
    """When N+k bundles exist, the k oldest get deleted."""
    from ghost_shell.profile.backup_sync import apply_retention
    t = _FakeTarget()
    keys = _seed_fake_bundles(t, "h", "p", 7)
    deleted = apply_retention(t, "h", "p", keep_last=3)
    assert len(deleted) == 4
    # Oldest 4 keys should be in deleted; newest 3 should remain.
    assert set(deleted) == set(keys[:4])
    assert set(t.store.keys()) == set(keys[4:])


def test_retention_zero_disables_policy():
    """keep_last=0 / negative is treated as 'no retention'."""
    from ghost_shell.profile.backup_sync import apply_retention
    t = _FakeTarget()
    _seed_fake_bundles(t, "h", "p", 5)
    assert apply_retention(t, "h", "p", keep_last=0) == []
    assert apply_retention(t, "h", "p", keep_last=-1) == []
    assert apply_retention(t, "h", "p", keep_last=None) == []
    assert len(t.store) == 5


def test_retention_isolates_by_profile():
    """Two profiles on the same host shouldn't interfere with each
    other's retention policy."""
    from ghost_shell.profile.backup_sync import apply_retention
    t = _FakeTarget()
    p1_keys = _seed_fake_bundles(t, "h", "p1", 5)
    p2_keys = _seed_fake_bundles(t, "h", "p2", 5,
                                  base_ts=datetime(2026, 5, 1))
    deleted = apply_retention(t, "h", "p1", keep_last=2)
    assert len(deleted) == 3
    # p2 untouched
    assert all(k in t.store for k in p2_keys)


def test_retention_isolates_by_host():
    """Two hosts with same profile name don't cross-evict."""
    from ghost_shell.profile.backup_sync import apply_retention
    t = _FakeTarget()
    h1_keys = _seed_fake_bundles(t, "h1", "p", 5)
    h2_keys = _seed_fake_bundles(t, "h2", "p", 5,
                                  base_ts=datetime(2026, 5, 1))
    deleted = apply_retention(t, "h1", "p", keep_last=2)
    assert len(deleted) == 3
    assert all(k in t.store for k in h2_keys)


# ──────────────────────────────────────────────────────────────
# target_from_config factory
# ──────────────────────────────────────────────────────────────

def test_target_from_config_returns_none_when_disabled():
    """No flag → no target. Caller treats this as 'feature off'."""
    from ghost_shell.profile.backup_sync import target_from_config
    fake_db = MagicMock()
    fake_db.config_get.side_effect = lambda key, default=None: (
        False if key == "backup.sync.enabled" else default
    )
    assert target_from_config(fake_db) is None


def test_target_from_config_unknown_provider_raises():
    """A typo in provider name should fail loudly so the user
    notices, not silently."""
    from ghost_shell.profile.backup_sync import (
        target_from_config, SyncConfigError,
    )
    fake_db = MagicMock()
    def cfg(key, default=None):
        if key == "backup.sync.enabled": return True
        if key == "backup.sync.provider": return "azure"     # typo
        return default
    fake_db.config_get.side_effect = cfg
    with pytest.raises(SyncConfigError):
        target_from_config(fake_db)


def test_target_from_config_dispatches_s3(monkeypatch):
    """provider=s3 + bucket → S3SyncTarget instance (mocked boto3)."""
    from ghost_shell.profile import backup_sync as bs
    fake_db = MagicMock()
    def cfg(key, default=None):
        d = {
            "backup.sync.enabled": True,
            "backup.sync.provider": "s3",
            "backup.sync.s3.bucket": "my-bucket",
            "backup.sync.s3.access_key": "AKIA",
            "backup.sync.s3.secret_key": "secret",
            "backup.sync.s3.region": "us-east-1",
        }
        return d.get(key, default)
    fake_db.config_get.side_effect = cfg
    fake_boto = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto)
    t = bs.target_from_config(fake_db)
    assert isinstance(t, bs.S3SyncTarget)
    assert t.bucket == "my-bucket"
    fake_boto.client.assert_called_once()


# ──────────────────────────────────────────────────────────────
# push_profile / pull_profile orchestration
# ──────────────────────────────────────────────────────────────

def _seed_minimal_profile_for_backup(db, name="bk-prof"):
    """Same shape as the test_backup.py helper but local so this
    file doesn't depend on it."""
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO profiles (name, created_at, ready_at) "
        "VALUES (?, datetime('now'), datetime('now'))", (name,)
    )
    conn.commit()


def test_push_profile_creates_bundle_and_pushes(in_memory_db,
                                                tmp_path, monkeypatch):
    """End-to-end: push_profile encrypts via create_bundle, hands
    bytes to target.push, fills in metadata."""
    from ghost_shell.profile import backup_sync as bs
    monkeypatch.setattr(
        "ghost_shell.core.platform_paths.PROJECT_ROOT", str(tmp_path)
    )
    _seed_minimal_profile_for_backup(in_memory_db, "p1")

    target = _FakeTarget()
    out = bs.push_profile(
        "p1",
        master_password="hunter2",
        db=in_memory_db,
        target=target,
        keep_last=0,
        source_host="ws-1",
    )
    assert out["ok"] is True
    assert out["host"] == "ws-1"
    assert out["size"] > 0
    assert len(target.push_calls) == 1
    pushed = target.push_calls[0]
    # Bundle should start with the GHSB magic
    assert target.store[pushed["key"]].startswith(b"GHSB")
    # Metadata fields the dashboard log relies on
    assert pushed["metadata"]["profile"] == "p1"
    assert pushed["metadata"]["host"] == "ws-1"


def test_push_profile_applies_retention_after_push(in_memory_db,
                                                   tmp_path, monkeypatch):
    """When keep_last is set, older bundles for the same (host,
    profile) get pruned AFTER the new push lands."""
    from ghost_shell.profile import backup_sync as bs
    monkeypatch.setattr(
        "ghost_shell.core.platform_paths.PROJECT_ROOT", str(tmp_path)
    )
    _seed_minimal_profile_for_backup(in_memory_db, "p1")
    target = _FakeTarget()
    # Pre-seed 3 old fake bundles for the same host+profile
    base = datetime(2026, 1, 1)
    _seed_fake_bundles(target, "ws-1", "p1", 3, base_ts=base)
    pre = set(target.store.keys())

    out = bs.push_profile(
        "p1", master_password="x", db=in_memory_db, target=target,
        keep_last=2, source_host="ws-1",
    )
    assert len(out["deleted"]) == 2   # 3 old + 1 new = 4, keep 2 → drop 2
    # The new bundle MUST still be in the store — never evict the
    # bundle we just pushed.
    assert out["key"] in target.store


def test_pull_profile_returns_most_recent_when_no_stamp():
    """pull_profile resolves (host, profile, None) to the newest
    bundle by parsed timestamp."""
    from ghost_shell.profile import backup_sync as bs
    target = _FakeTarget()
    keys = _seed_fake_bundles(target, "h", "p", 5)
    blob = bs.pull_profile(target, source_host="h", profile_name="p")
    # Newest seeded was index 4
    assert blob == target.store[keys[4]]


def test_pull_profile_with_specific_stamp():
    from ghost_shell.profile import backup_sync as bs
    target = _FakeTarget()
    keys = _seed_fake_bundles(target, "h", "p", 5)
    parsed = bs.parse_key(keys[2])
    blob = bs.pull_profile(target, source_host="h", profile_name="p",
                            stamp=parsed["stamp"])
    assert blob == target.store[keys[2]]


def test_pull_profile_raises_when_no_match():
    from ghost_shell.profile import backup_sync as bs
    from ghost_shell.profile.backup_sync import SyncTransportError
    target = _FakeTarget()
    with pytest.raises(SyncTransportError):
        bs.pull_profile(target, source_host="ghost", profile_name="ghost")


def test_pull_profile_unknown_stamp_raises():
    from ghost_shell.profile import backup_sync as bs
    from ghost_shell.profile.backup_sync import SyncTransportError
    target = _FakeTarget()
    _seed_fake_bundles(target, "h", "p", 3)
    with pytest.raises(SyncTransportError):
        bs.pull_profile(target, source_host="h", profile_name="p",
                         stamp="99999999-999999")


def test_list_remote_bundles_filters_by_host_and_profile():
    """list_remote_bundles flattens to JSON-serializable dicts and
    applies optional host/profile filters."""
    from ghost_shell.profile import backup_sync as bs
    target = _FakeTarget()
    _seed_fake_bundles(target, "h1", "pA", 2)
    _seed_fake_bundles(target, "h1", "pB", 2,
                        base_ts=datetime(2026, 5, 1))
    _seed_fake_bundles(target, "h2", "pA", 2,
                        base_ts=datetime(2026, 6, 1))
    all_items = bs.list_remote_bundles(target)
    assert len(all_items) == 6
    # Filtered by host
    h1_only = bs.list_remote_bundles(target, source_host="h1")
    assert all(it["host"] == "h1" for it in h1_only)
    assert len(h1_only) == 4
    # Filtered by both
    pa_h1 = bs.list_remote_bundles(target, source_host="h1",
                                    profile_name="pA")
    assert len(pa_h1) == 2
    assert all(it["host"] == "h1" and it["profile_name"] == "pA"
               for it in pa_h1)


def test_list_remote_bundles_sorts_newest_first():
    """Dashboard expects a newest-first listing for the picker UI."""
    from ghost_shell.profile import backup_sync as bs
    target = _FakeTarget()
    keys = _seed_fake_bundles(target, "h", "p", 5)
    out = bs.list_remote_bundles(target, source_host="h", profile_name="p")
    stamps = [it["stamp"] for it in out]
    assert stamps == sorted(stamps, reverse=True)


# ──────────────────────────────────────────────────────────────
# SyncTarget base default ping
# ──────────────────────────────────────────────────────────────

def test_default_ping_uses_list():
    """Default ping calls list with a sentinel prefix; success
    propagates as ok=True. Useful for adapters that don't override."""
    from ghost_shell.profile.backup_sync import SyncTarget

    class _Stub(SyncTarget):
        name = "stub"
        def push(self, *a, **k): pass
        def pull(self, *a, **k): return b""
        def list(self, prefix=""):
            self._listed = prefix
            return []
        def delete(self, *a, **k): pass

    s = _Stub()
    info = s.ping()
    assert info["ok"] is True
    assert "__ping__" in s._listed


def test_default_ping_catches_exceptions():
    from ghost_shell.profile.backup_sync import SyncTarget

    class _Boom(SyncTarget):
        name = "boom"
        def push(self, *a, **k): pass
        def pull(self, *a, **k): return b""
        def list(self, prefix=""):
            raise RuntimeError("network down")
        def delete(self, *a, **k): pass

    info = _Boom().ping()
    assert info["ok"] is False
    assert "network down" in info["error"]
