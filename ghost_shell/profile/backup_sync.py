"""
backup_sync.py — Cloud / remote sync targets for encrypted bundles.

Sprint 8 — wraps :mod:`ghost_shell.profile.backup` with byte-transport
adapters for S3-compatible object storage, Dropbox, and SFTP. The
bundle format is already self-describing and end-to-end encrypted, so
these adapters never see plaintext profile data — they only push and
pull opaque ``.ghs-bundle`` blobs.

Key naming convention
─────────────────────
    gs-backups/<source_host>/<profile_name>/<YYYYMMDD-HHMMSS>.ghs-bundle

Where ``source_host`` is ``socket.gethostname()`` of the machine that
created the bundle. Predictable, sortable, and lets a single bucket
serve multiple workstations without collision.

Public API
──────────
* ``SyncTarget``                base ABC with ``push``, ``pull``,
                                ``list``, ``delete``, ``ping``.
* ``S3SyncTarget``              boto3 wrapper (also covers MinIO, R2,
                                Wasabi, Backblaze B2 via S3 API).
* ``DropboxSyncTarget``         dropbox-sdk-python wrapper.
* ``SFTPSyncTarget``            paramiko wrapper.
* ``target_from_config(db)``    factory — reads backup.sync.* from DB
                                config and returns the configured
                                target (or None if disabled).
* ``apply_retention(target, profile_name, keep_last)``
                                trim oldest entries past ``keep_last``.
* ``push_profile(profile_name, master_password, db=None, target=None,
                 keep_last=None)``
                                full-stack: create_bundle → push →
                                retention. Returns metadata dict.
* ``pull_profile(target, source_host, profile_name, ts=None)``
                                fetch a specific bundle (or the most
                                recent). Returns raw bytes — caller
                                feeds them to ``restore_bundle``.

Errors raised:
  SyncConfigError   target isn't configured / required fields missing.
  SyncTransportError network / auth / permission failures from the
                    underlying SDK. Wraps so callers don't need to
                    import boto3 / dropbox / paramiko exception types.

Lazy imports
────────────
Each adapter pulls its SDK only when ``__init__`` is called. Ghost
Shell installs without boto3 / dropbox / paramiko by default — the
user installs only the SDK they actually use, and instantiating the
wrong adapter raises a friendly ``RuntimeError`` telling them which
package to install.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import io
import json
import logging
import os
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────

class SyncConfigError(Exception):
    """Configuration is missing or malformed — adapter can't start."""


class SyncTransportError(Exception):
    """Wrapping any SDK-specific transport / auth / IO error so the
    dashboard doesn't have to know about boto3 / dropbox / paramiko."""


# ──────────────────────────────────────────────────────────────
# Key naming + retention helpers
# ──────────────────────────────────────────────────────────────

KEY_PREFIX = "gs-backups"           # top-level "folder"
BUNDLE_EXT = ".ghs-bundle"


def make_key(source_host: str, profile_name: str,
             ts: Optional[datetime] = None) -> str:
    """Build the canonical key for a new bundle.

    Forward slashes only — works for S3 keys, Dropbox paths, and SFTP
    paths uniformly. Caller is responsible for translating to native
    path separators when writing to a local filesystem (we never do
    that here)."""
    safe_host    = _safe_segment(source_host or "unknown-host")
    safe_profile = _safe_segment(profile_name or "unknown-profile")
    if ts is None:
        ts = datetime.now()
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    return f"{KEY_PREFIX}/{safe_host}/{safe_profile}/{stamp}{BUNDLE_EXT}"


def parse_key(key: str) -> Optional[dict]:
    """Reverse of make_key. Returns None if the key isn't ours."""
    if not key or not key.startswith(KEY_PREFIX + "/"):
        return None
    parts = key.split("/")
    if len(parts) != 4:
        return None
    _, host, profile, fname = parts
    if not fname.endswith(BUNDLE_EXT):
        return None
    stamp = fname[: -len(BUNDLE_EXT)]
    try:
        ts = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        ts = None
    return {
        "host":         host,
        "profile_name": profile,
        "stamp":        stamp,
        "ts":           ts,
        "filename":     fname,
        "key":          key,
    }


def _safe_segment(s: str) -> str:
    """Squash anything that's not [-A-Za-z0-9._] into a dash. Keeps
    keys URL-safe and avoids accidental nested folders."""
    out = []
    for ch in s:
        if ch.isalnum() or ch in "-._":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out) or "_"


# ──────────────────────────────────────────────────────────────
# Base adapter
# ──────────────────────────────────────────────────────────────

@dataclass
class BundleEntry:
    """Listing entry returned by SyncTarget.list()."""
    key:          str
    size:         int            # bytes
    last_modified: Optional[datetime] = None
    parsed:       dict = field(default_factory=dict)   # parse_key(key)


class SyncTarget(ABC):
    """Adapter contract. Concrete subclasses ship raw bytes — they do
    NOT crack open the bundle, derive keys, or do anything that
    requires the master password. Encryption is the caller's job."""

    name: str = "abstract"

    @abstractmethod
    def push(self, key: str, blob: bytes,
             metadata: Optional[dict] = None) -> dict:
        """Upload ``blob`` under ``key``. Returns provider-specific
        metadata (etag, version-id, etc.) for the dashboard log."""

    @abstractmethod
    def pull(self, key: str) -> bytes:
        """Download bundle bytes for ``key``. Raises
        :class:`SyncTransportError` if the key doesn't exist."""

    @abstractmethod
    def list(self, prefix: str = "") -> List[BundleEntry]:
        """List bundles whose key starts with ``prefix``. Default is
        all bundles in the target."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a bundle. No-op if it doesn't exist (different
        SDKs disagree — we normalize to silent no-op)."""

    def ping(self) -> dict:
        """Light connectivity / auth check — used by the dashboard's
        'Test connection' button. Default impl tries a list with a
        non-matching prefix (cheap). Adapters can override with
        something more meaningful."""
        try:
            self.list(prefix=f"{KEY_PREFIX}/__ping__/")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# S3 adapter (also covers MinIO / R2 / Wasabi / B2 via S3 API)
# ──────────────────────────────────────────────────────────────

class S3SyncTarget(SyncTarget):
    """boto3 wrapper. Compatible with any S3-API service:

    * AWS S3                — leave ``endpoint_url`` empty
    * Cloudflare R2         — ``https://<accountid>.r2.cloudflarestorage.com``
    * Backblaze B2          — ``https://s3.<region>.backblazeb2.com``
    * MinIO / SeaweedFS     — local endpoint URL
    * Wasabi                — ``https://s3.<region>.wasabisys.com``

    Auth via standard AWS creds (access_key + secret_key) or an
    AWS profile (~/.aws/credentials)."""

    name = "s3"

    def __init__(self, bucket: str,
                 access_key: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 region: Optional[str] = None,
                 endpoint_url: Optional[str] = None,
                 aws_profile: Optional[str] = None):
        if not bucket:
            raise SyncConfigError("S3 target needs a bucket name")
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "boto3 is required for the S3 sync target. "
                "pip install boto3>=1.28"
            ) from e

        self.bucket = bucket
        kwargs = {}
        if access_key and secret_key:
            kwargs["aws_access_key_id"]     = access_key
            kwargs["aws_secret_access_key"] = secret_key
        if region:
            kwargs["region_name"] = region
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if aws_profile and not (access_key and secret_key):
            session = boto3.Session(profile_name=aws_profile)
            self._client = session.client("s3", **kwargs)
        else:
            self._client = boto3.client("s3", **kwargs)

    def push(self, key: str, blob: bytes,
             metadata: Optional[dict] = None) -> dict:
        from botocore.exceptions import ClientError
        # Stringify metadata keys/values — S3 user-metadata only
        # accepts ASCII strings.
        meta = {k: str(v) for k, v in (metadata or {}).items()
                if k and v is not None}
        try:
            r = self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=blob,
                ContentType="application/octet-stream",
                Metadata=meta,
            )
        except ClientError as e:
            raise SyncTransportError(f"S3 put_object failed: {e}") from e
        return {
            "etag":       (r.get("ETag") or "").strip('"'),
            "version_id": r.get("VersionId"),
            "size":       len(blob),
        }

    def pull(self, key: str) -> bytes:
        from botocore.exceptions import ClientError
        try:
            r = self._client.get_object(Bucket=self.bucket, Key=key)
            return r["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise SyncTransportError(f"S3 key not found: {key}") from e
            raise SyncTransportError(f"S3 get_object failed: {e}") from e

    def list(self, prefix: str = "") -> List[BundleEntry]:
        from botocore.exceptions import ClientError
        # Default to our top-level prefix so we don't accidentally
        # walk the entire bucket if it has unrelated objects.
        eff_prefix = prefix or (KEY_PREFIX + "/")
        out: List[BundleEntry] = []
        token = None
        try:
            while True:
                kw = {"Bucket": self.bucket, "Prefix": eff_prefix}
                if token:
                    kw["ContinuationToken"] = token
                r = self._client.list_objects_v2(**kw)
                for obj in r.get("Contents") or []:
                    parsed = parse_key(obj["Key"]) or {}
                    out.append(BundleEntry(
                        key=obj["Key"],
                        size=int(obj.get("Size") or 0),
                        last_modified=obj.get("LastModified"),
                        parsed=parsed,
                    ))
                if not r.get("IsTruncated"):
                    break
                token = r.get("NextContinuationToken")
        except ClientError as e:
            raise SyncTransportError(f"S3 list failed: {e}") from e
        return out

    def delete(self, key: str) -> None:
        from botocore.exceptions import ClientError
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            # delete_object normally returns 204 even for missing
            # keys; only treat 4xx-other / 5xx as errors.
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return
            raise SyncTransportError(f"S3 delete failed: {e}") from e


# ──────────────────────────────────────────────────────────────
# Dropbox adapter
# ──────────────────────────────────────────────────────────────

class DropboxSyncTarget(SyncTarget):
    """Dropbox API v2 wrapper.

    Auth via a long-lived **app access token** (Dropbox developer
    console → "Generate access token") OR a **refresh token** + app
    credentials for token-rotating apps. The Dropbox SDK is unhappy
    if both are supplied — caller picks one.

    Files live under ``/Apps/<your-app>/gs-backups/...`` when the app
    has the App-Folder permission; under ``/gs-backups/...`` for
    full-Dropbox apps. We treat the path as opaque from the caller's
    perspective."""

    name = "dropbox"

    def __init__(self,
                 access_token: Optional[str] = None,
                 refresh_token: Optional[str] = None,
                 app_key: Optional[str] = None,
                 app_secret: Optional[str] = None,
                 root_path: str = ""):
        try:
            import dropbox  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "dropbox is required for the Dropbox sync target. "
                "pip install dropbox>=11"
            ) from e
        import dropbox as _dbx
        if not (access_token or (refresh_token and app_key and app_secret)):
            raise SyncConfigError(
                "Dropbox target needs either access_token or "
                "refresh_token + app_key + app_secret"
            )
        if access_token:
            self._dbx = _dbx.Dropbox(oauth2_access_token=access_token)
        else:
            self._dbx = _dbx.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret,
            )
        # Normalize root: must start with '/' or be empty.
        self._root = root_path.rstrip("/")
        if self._root and not self._root.startswith("/"):
            self._root = "/" + self._root

    def _fullpath(self, key: str) -> str:
        return f"{self._root}/{key}" if self._root else f"/{key}"

    def push(self, key: str, blob: bytes,
             metadata: Optional[dict] = None) -> dict:
        # Dropbox has no per-object metadata field — we just upload.
        # Caller can encode metadata into the key path if needed.
        import dropbox
        path = self._fullpath(key)
        try:
            r = self._dbx.files_upload(
                blob,
                path,
                mode=dropbox.files.WriteMode.overwrite,
                mute=True,
            )
        except dropbox.exceptions.ApiError as e:
            raise SyncTransportError(f"Dropbox upload failed: {e}") from e
        except dropbox.exceptions.AuthError as e:
            raise SyncTransportError(f"Dropbox auth failed: {e}") from e
        return {
            "rev":  r.rev,
            "id":   r.id,
            "size": r.size,
        }

    def pull(self, key: str) -> bytes:
        import dropbox
        path = self._fullpath(key)
        try:
            _, resp = self._dbx.files_download(path)
            return resp.content
        except dropbox.exceptions.ApiError as e:
            raise SyncTransportError(f"Dropbox download failed: {e}") from e

    def list(self, prefix: str = "") -> List[BundleEntry]:
        import dropbox
        eff = prefix or (KEY_PREFIX + "/")
        path = self._fullpath(eff.rstrip("/"))
        out: List[BundleEntry] = []
        try:
            r = self._dbx.files_list_folder(path, recursive=True)
            while True:
                for entry in r.entries:
                    if not isinstance(entry, dropbox.files.FileMetadata):
                        continue
                    # Strip the root prefix so the returned key is
                    # always relative to KEY_PREFIX.
                    rel = entry.path_display.lstrip("/")
                    if self._root:
                        root_rel = self._root.lstrip("/") + "/"
                        if rel.startswith(root_rel):
                            rel = rel[len(root_rel):]
                    parsed = parse_key(rel) or {}
                    out.append(BundleEntry(
                        key=rel,
                        size=entry.size,
                        last_modified=entry.client_modified or entry.server_modified,
                        parsed=parsed,
                    ))
                if not r.has_more:
                    break
                r = self._dbx.files_list_folder_continue(r.cursor)
        except dropbox.exceptions.ApiError as e:
            # path-not-found is acceptable (empty target) — return [].
            err = e.error if hasattr(e, "error") else None
            if (err and getattr(err, "is_path", lambda: False)()
                and getattr(err.get_path(), "is_not_found", lambda: False)()):
                return []
            raise SyncTransportError(f"Dropbox list failed: {e}") from e
        return out

    def delete(self, key: str) -> None:
        import dropbox
        path = self._fullpath(key)
        try:
            self._dbx.files_delete_v2(path)
        except dropbox.exceptions.ApiError as e:
            err = e.error if hasattr(e, "error") else None
            if (err and getattr(err, "is_path_lookup", lambda: False)()
                and getattr(err.get_path_lookup(), "is_not_found",
                            lambda: False)()):
                return
            raise SyncTransportError(f"Dropbox delete failed: {e}") from e


# ──────────────────────────────────────────────────────────────
# SFTP adapter
# ──────────────────────────────────────────────────────────────

class SFTPSyncTarget(SyncTarget):
    """paramiko SFTP wrapper. The simplest deployment model — point
    it at any SSH host the user has shell + filesystem access on.

    Auth options:
      * password
      * private key file (path)
      * private key contents (string PEM)
      * SSH agent (default — paramiko picks up ~/.ssh/agent or
        Pageant if neither password nor key is supplied)

    Each call opens a fresh transport; we don't try to reuse
    connections because the dashboard's call rate is low and a stale
    transport is the most common SFTP gotcha."""

    name = "sftp"

    def __init__(self,
                 host: str,
                 port: int = 22,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 key_file: Optional[str] = None,
                 key_data: Optional[str] = None,
                 key_passphrase: Optional[str] = None,
                 base_path: str = "."):
        if not host:
            raise SyncConfigError("SFTP target needs a host")
        try:
            import paramiko  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "paramiko is required for the SFTP sync target. "
                "pip install paramiko>=3"
            ) from e
        self.host = host
        self.port = int(port or 22)
        self.username = username
        self.password = password
        self.key_file = key_file
        self.key_data = key_data
        self.key_passphrase = key_passphrase
        # base_path is the directory on the remote where KEY_PREFIX
        # lives. Default '.' = the SFTP user's home directory.
        self.base_path = (base_path or ".").rstrip("/") or "."

    # ───────── transport plumbing ─────────

    def _open(self):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.host, "port": self.port,
            "username": self.username, "look_for_keys": True,
            "allow_agent": True, "timeout": 20,
        }
        if self.password:
            connect_kwargs["password"] = self.password
        if self.key_file:
            connect_kwargs["key_filename"] = self.key_file
        if self.key_data:
            from io import StringIO
            try:
                k = paramiko.RSAKey.from_private_key(
                    StringIO(self.key_data), password=self.key_passphrase
                )
            except paramiko.SSHException:
                # Try Ed25519 — paramiko doesn't autodetect.
                k = paramiko.Ed25519Key.from_private_key(
                    StringIO(self.key_data), password=self.key_passphrase
                )
            connect_kwargs["pkey"] = k
        try:
            client.connect(**connect_kwargs)
        except Exception as e:
            client.close()
            raise SyncTransportError(f"SFTP connect failed: {e}") from e
        sftp = client.open_sftp()
        return client, sftp

    def _ensure_remote_dir(self, sftp, path: str) -> None:
        """mkdir -p over SFTP. Walks each segment, ignores 'exists'."""
        parts = [p for p in path.split("/") if p]
        cur = "" if not path.startswith("/") else "/"
        for p in parts:
            cur = (cur + "/" + p) if cur not in ("", "/") else (cur + p) if cur else p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except Exception as e:
                    raise SyncTransportError(
                        f"SFTP mkdir {cur} failed: {e}"
                    ) from e

    def _full(self, key: str) -> str:
        if self.base_path == ".":
            return key
        return f"{self.base_path}/{key}"

    # ───────── public API ─────────

    def push(self, key: str, blob: bytes,
             metadata: Optional[dict] = None) -> dict:
        path = self._full(key)
        client, sftp = self._open()
        try:
            self._ensure_remote_dir(sftp, os.path.dirname(path))
            tmp = path + ".part"
            with sftp.open(tmp, "wb") as f:
                f.write(blob)
            try:
                sftp.posix_rename(tmp, path)
            except (AttributeError, IOError):
                # Fallback for SFTP servers without posix_rename:
                # remove existing target, then plain rename.
                try:
                    sftp.remove(path)
                except IOError:
                    pass
                sftp.rename(tmp, path)
            attrs = sftp.stat(path)
            return {
                "size":     attrs.st_size,
                "mtime":    attrs.st_mtime,
                "remote_path": path,
            }
        except SyncTransportError:
            raise
        except Exception as e:
            raise SyncTransportError(f"SFTP push failed: {e}") from e
        finally:
            try:
                sftp.close()
                client.close()
            except Exception:
                pass

    def pull(self, key: str) -> bytes:
        path = self._full(key)
        client, sftp = self._open()
        try:
            with sftp.open(path, "rb") as f:
                return f.read()
        except IOError as e:
            raise SyncTransportError(f"SFTP read failed: {e}") from e
        finally:
            try:
                sftp.close()
                client.close()
            except Exception:
                pass

    def list(self, prefix: str = "") -> List[BundleEntry]:
        eff = (prefix or (KEY_PREFIX + "/")).rstrip("/")
        path = self._full(eff) if eff else self.base_path
        out: List[BundleEntry] = []
        client, sftp = self._open()
        try:
            self._walk_listdir(sftp, path, eff, out)
        except SyncTransportError:
            raise
        except IOError:
            # path doesn't exist yet — empty target.
            return []
        finally:
            try:
                sftp.close()
                client.close()
            except Exception:
                pass
        return out

    def _walk_listdir(self, sftp, abs_dir: str, rel_prefix: str,
                      acc: list) -> None:
        try:
            entries = sftp.listdir_attr(abs_dir)
        except IOError:
            return
        for a in entries:
            child_abs = f"{abs_dir}/{a.filename}"
            child_rel = (f"{rel_prefix}/{a.filename}"
                         if rel_prefix else a.filename)
            try:
                from stat import S_ISDIR
                is_dir = S_ISDIR(a.st_mode)
            except Exception:
                is_dir = False
            if is_dir:
                self._walk_listdir(sftp, child_abs, child_rel, acc)
            else:
                if not child_rel.endswith(BUNDLE_EXT):
                    continue
                parsed = parse_key(child_rel) or {}
                acc.append(BundleEntry(
                    key=child_rel,
                    size=int(a.st_size or 0),
                    last_modified=(
                        datetime.fromtimestamp(a.st_mtime)
                        if a.st_mtime else None
                    ),
                    parsed=parsed,
                ))

    def delete(self, key: str) -> None:
        path = self._full(key)
        client, sftp = self._open()
        try:
            try:
                sftp.remove(path)
            except IOError:
                # Missing — silent no-op, matches S3/Dropbox behaviour.
                return
        finally:
            try:
                sftp.close()
                client.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────
# Factory + retention + high-level helpers
# ──────────────────────────────────────────────────────────────

def target_from_config(db=None) -> Optional[SyncTarget]:
    """Read ``backup.sync.*`` config keys from the DB and instantiate
    the configured target.

    Returns ``None`` if ``backup.sync.enabled`` is false / unset, so
    callers can no-op gracefully on installs that haven't opted in.

    Recognized keys:
      backup.sync.enabled              bool
      backup.sync.provider             "s3" | "dropbox" | "sftp"

      # S3
      backup.sync.s3.bucket            str
      backup.sync.s3.access_key        str
      backup.sync.s3.secret_key        str  (vault-encrypted in prod)
      backup.sync.s3.region            str
      backup.sync.s3.endpoint_url      str  (R2/MinIO/Wasabi/B2)
      backup.sync.s3.aws_profile       str

      # Dropbox
      backup.sync.dropbox.access_token   str  (vault-encrypted)
      backup.sync.dropbox.refresh_token  str
      backup.sync.dropbox.app_key        str
      backup.sync.dropbox.app_secret     str  (vault-encrypted)
      backup.sync.dropbox.root_path      str

      # SFTP
      backup.sync.sftp.host             str
      backup.sync.sftp.port             int
      backup.sync.sftp.username         str
      backup.sync.sftp.password         str  (vault-encrypted)
      backup.sync.sftp.key_file         str
      backup.sync.sftp.base_path        str
    """
    if db is None:
        from ghost_shell.db.database import get_db
        db = get_db()
    if not bool(db.config_get("backup.sync.enabled", False)):
        return None
    provider = (db.config_get("backup.sync.provider", "") or "").lower()
    if provider == "s3":
        return S3SyncTarget(
            bucket       = db.config_get("backup.sync.s3.bucket"),
            access_key   = db.config_get("backup.sync.s3.access_key"),
            secret_key   = db.config_get("backup.sync.s3.secret_key"),
            region       = db.config_get("backup.sync.s3.region"),
            endpoint_url = db.config_get("backup.sync.s3.endpoint_url"),
            aws_profile  = db.config_get("backup.sync.s3.aws_profile"),
        )
    if provider == "dropbox":
        return DropboxSyncTarget(
            access_token  = db.config_get("backup.sync.dropbox.access_token"),
            refresh_token = db.config_get("backup.sync.dropbox.refresh_token"),
            app_key       = db.config_get("backup.sync.dropbox.app_key"),
            app_secret    = db.config_get("backup.sync.dropbox.app_secret"),
            root_path     = db.config_get("backup.sync.dropbox.root_path", "") or "",
        )
    if provider == "sftp":
        return SFTPSyncTarget(
            host           = db.config_get("backup.sync.sftp.host"),
            port           = db.config_get("backup.sync.sftp.port", 22) or 22,
            username       = db.config_get("backup.sync.sftp.username"),
            password       = db.config_get("backup.sync.sftp.password"),
            key_file       = db.config_get("backup.sync.sftp.key_file"),
            key_data       = db.config_get("backup.sync.sftp.key_data"),
            key_passphrase = db.config_get("backup.sync.sftp.key_passphrase"),
            base_path      = db.config_get("backup.sync.sftp.base_path", ".") or ".",
        )
    raise SyncConfigError(f"unknown backup.sync.provider {provider!r}")


def apply_retention(target: SyncTarget, source_host: str,
                    profile_name: str, keep_last: int) -> List[str]:
    """Trim oldest bundles for one (host, profile) pair past
    ``keep_last``. Returns the list of keys deleted.

    keep_last <= 0 disables retention (no-op)."""
    if keep_last is None or keep_last <= 0:
        return []
    prefix = f"{KEY_PREFIX}/{_safe_segment(source_host)}/{_safe_segment(profile_name)}/"
    entries = [e for e in target.list(prefix=prefix)
               if e.parsed.get("ts") is not None]
    if len(entries) <= keep_last:
        return []
    entries.sort(key=lambda e: e.parsed["ts"], reverse=True)
    to_delete = entries[keep_last:]
    deleted = []
    for e in to_delete:
        try:
            target.delete(e.key)
            deleted.append(e.key)
        except Exception as ex:
            log.warning("[backup_sync] retention delete failed for %s: %s",
                        e.key, ex)
    return deleted


def push_profile(profile_name: str,
                 master_password: str,
                 db=None,
                 target: Optional[SyncTarget] = None,
                 keep_last: Optional[int] = None,
                 source_host: Optional[str] = None) -> dict:
    """High-level: create_bundle → upload to target → apply retention.

    Returns ``{"ok": bool, "key": ..., "size": ..., "deleted": [...]}``.

    Errors propagate as ``SyncConfigError`` / ``SyncTransportError`` /
    ``ValueError`` from the underlying create_bundle. Caller is
    responsible for catching them."""
    from ghost_shell.profile.backup import create_bundle
    if target is None:
        target = target_from_config(db)
        if target is None:
            raise SyncConfigError(
                "no sync target configured — set "
                "backup.sync.enabled = true and pick a provider"
            )
    blob = create_bundle(profile_name, master_password=master_password, db=db)
    host = source_host or socket.gethostname()
    key  = make_key(host, profile_name)
    meta = {
        "schema":      "ghost_shell_bundle_v1",
        "profile":     profile_name,
        "host":        host,
        "ts":          datetime.now().isoformat(timespec="seconds"),
        "size":        str(len(blob)),
    }
    push_info = target.push(key, blob, metadata=meta)
    # Retention runs after a successful push so a failed push doesn't
    # cause the previous-good bundle to be evicted.
    deleted: List[str] = []
    if keep_last is None and db is not None:
        try:
            keep_last = int(db.config_get("backup.sync.keep_last", 0) or 0)
        except (TypeError, ValueError):
            keep_last = 0
    if keep_last and keep_last > 0:
        try:
            deleted = apply_retention(target, host, profile_name, keep_last)
        except Exception as e:
            log.warning("[backup_sync] retention skipped: %s", e)
    return {
        "ok":      True,
        "key":     key,
        "size":    len(blob),
        "host":    host,
        "deleted": deleted,
        "push":    push_info,
    }


def pull_profile(target: SyncTarget,
                 source_host: str,
                 profile_name: str,
                 stamp: Optional[str] = None) -> bytes:
    """Fetch a bundle. ``stamp=None`` returns the most recent.

    The caller decrypts via ``restore_bundle``. We deliberately don't
    chain into restore here so the dashboard can present the
    inspect-bundle UX before the user types their master password."""
    prefix = f"{KEY_PREFIX}/{_safe_segment(source_host)}/{_safe_segment(profile_name)}/"
    entries = [e for e in target.list(prefix=prefix)
               if e.parsed.get("ts") is not None]
    if not entries:
        raise SyncTransportError(
            f"no bundles found for host={source_host!r} "
            f"profile={profile_name!r}"
        )
    if stamp:
        match = [e for e in entries if e.parsed.get("stamp") == stamp]
        if not match:
            raise SyncTransportError(
                f"no bundle with stamp {stamp!r} for "
                f"host={source_host!r} profile={profile_name!r}"
            )
        return target.pull(match[0].key)
    entries.sort(key=lambda e: e.parsed["ts"], reverse=True)
    return target.pull(entries[0].key)


def list_remote_bundles(target: SyncTarget,
                        source_host: Optional[str] = None,
                        profile_name: Optional[str] = None) -> List[dict]:
    """Flat-listing helper for the dashboard's remote-browser UI.

    Returns dicts in the JSON-serializable shape the dashboard expects
    (no datetime objects, no dataclass), grouped/filtered by host
    and profile when supplied."""
    if source_host and profile_name:
        prefix = f"{KEY_PREFIX}/{_safe_segment(source_host)}/{_safe_segment(profile_name)}/"
    elif source_host:
        prefix = f"{KEY_PREFIX}/{_safe_segment(source_host)}/"
    else:
        prefix = KEY_PREFIX + "/"
    out = []
    for e in target.list(prefix=prefix):
        if profile_name and e.parsed.get("profile_name") != profile_name:
            continue
        out.append({
            "key":           e.key,
            "size":          e.size,
            "host":          e.parsed.get("host"),
            "profile_name":  e.parsed.get("profile_name"),
            "stamp":         e.parsed.get("stamp"),
            "ts":            (e.parsed["ts"].isoformat()
                              if e.parsed.get("ts") else None),
            "last_modified": (e.last_modified.isoformat()
                              if e.last_modified else None),
        })
    out.sort(key=lambda d: d.get("ts") or "", reverse=True)
    return out
