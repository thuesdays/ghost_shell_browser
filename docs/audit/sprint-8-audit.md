# Sprint 8 audit — Backup UI + Cloud sync + coverage tests

Comprehensive review pass over the Sprint 7 + 8 surface area:

* `ghost_shell/profile/backup.py` (Sprint 7 core) — re-audited in light
  of new callers from sync layer.
* `ghost_shell/profile/backup_sync.py` (Sprint 8.2) — new.
* `ghost_shell/dashboard/server.py` — new endpoints under
  `/api/profiles/<name>/backup`, `/api/backup/inspect`,
  `/api/backup/restore`, `/api/backup/sync/*`.
* `dashboard/pages/profile.html` + `dashboard/js/pages/profile-detail.js` —
  new backup card, three modals (download / local-restore /
  cloud-restore), nine new methods.
* `tests/test_backup.py` (19 cases), `tests/test_backup_sync.py`
  (26 cases), `tests/test_db_runs_health_helpers.py` (28 cases).

Severity legend: 🔴 critical (silent corruption / security) ·
🟡 should-fix (correctness or UX hole) · 🟢 informational.

## Security

**SA-01 🟡 SHOULD-FIX — master password posted as JSON over loopback**
- The download flow sends `{master_password}` as JSON body to
  `/api/profiles/<name>/backup`. Push-to-cloud does the same to
  `/api/profiles/<name>/backup/sync/push`. Both go to
  `127.0.0.1:5000` over plain HTTP.
- On the user's own machine this is fine. On a multi-user box
  another local user could read `127.0.0.1` traffic with raw
  sockets (rare) or sniff via `lsof`/proc fd inspection.
- The dashboard already has this exposure for vault unlock —
  not a regression, but worth documenting in the wiki.
- **Mitigation candidates** (next sprint): require the dashboard
  to be reachable only from `localhost` (already default), add an
  optional API key for write endpoints, or sit Flask behind
  Caddy/nginx with TLS for multi-host installs.

**SA-02 🟢 INFORMATIONAL — bundle does not bind to a specific host**
- A `.ghs-bundle` carries the original `source_host` only as
  metadata. Anyone who has both the bundle and the master password
  can restore on any machine. This is the design (it's the whole
  point of "portable backup"), but worth calling out in the user-
  facing docs so users understand the threat model.

**SA-03 🟢 INFORMATIONAL — bundle does not embed a checksum**
- Format v1 has no MAC over the header. Fernet token has its own
  MAC over the ciphertext, so any bit-flip in the ciphertext fails
  the auth tag and raises `BundleAuthError`. Bit-flips in the
  HEADER (magic / version / payload_len) are caught by either
  `_unpack_header` or the `len(ciphertext) != payload_len` check,
  so the format is self-validating end-to-end. No fix needed.

**SA-04 🟢 INFORMATIONAL — bundle includes ciphertext vault items**
- Verified: `vault_items` rows are exported as their
  Fernet-encrypted blobs (column `secrets_enc`), never decrypted
  in `create_bundle`. On restore the rows are re-INSERTed verbatim
  and the destination's vault decrypts them with the same master
  password. The backup module never holds plaintext credentials.
- This means: if the user's vault master is different from their
  backup master, restored vault rows won't decrypt on the new
  host. The UX copy in the download modal already calls this out
  ("use the SAME password as your vault master"). Worth a wiki
  note.

**SA-05 🟢 INFORMATIONAL — path-traversal defense verified**
- `_unpack_user_data_dir` rejects entries with `..`, absolute
  paths, and any `realpath` that escapes `target_real`. Symlinks
  pointing outside the target are also rejected because their
  `realpath` resolves outside.
- Test `test_unpack_userdir_rejects_path_traversal` (Sprint 7)
  exercises both classic path-traversal and absolute-path attacks.
- A **directory traversal via tar member with `..` in a hardlink
  target** is technically possible if `tarfile.extract` chases
  hardlinks; we don't write tar archives that contain hardlinks
  in `_pack_user_data_dir` (we only `tf.add` regular files), and
  the source paths are all under the user's own profile dir, so
  this is a non-issue in practice. No fix.

**SA-06 🟢 INFORMATIONAL — sync credentials live in DB config**
- `backup.sync.s3.secret_key` etc. are read straight from
  `db.config_get(...)`. The DB is on the user's local disk; the
  same trust boundary as the vault key. Users who care should
  encrypt their `~/.ghost_shell/db.sqlite` with full-disk
  encryption (Windows BitLocker / macOS FileVault / Linux LUKS).
- Future sprint candidate: store sync credentials inside the
  vault (so they're Fernet-encrypted at rest) and pull them lazily
  in `target_from_config`.

## Correctness / race conditions

**CR-01 🟡 SHOULD-FIX — cloud push runs retention before push completes
in the multi-tenant case (theoretical)**
- `push_profile()` sequence: create_bundle → target.push → retention.
- If two operators push for the same `(host, profile)` concurrently
  (e.g. scheduler + manual), they both compute their own
  `make_key(...)` with second-precision timestamps. When stamps
  collide (unlikely but possible inside the same second), one
  push overwrites the other.
- Effect: silent loss of the older bundle. Both operators see
  "ok": True, but only one bundle persists.
- **Fix candidate** (deferred): replace stamp granularity with
  `%Y%m%d-%H%M%S-%f` (microseconds) or append a random nonce. Risk
  is low for a single-operator install.

**CR-02 🟢 BENIGN — retention runs after push, never before**
- The order is intentional: a failed push must NOT cause the
  previous-good bundle to be evicted. Verified in
  `test_push_profile_applies_retention_after_push` —
  `out["key"] in target.store` always holds.

**CR-03 🟢 BENIGN — `apply_retention` deletion failures are logged but
not fatal**
- A flaky cloud delete (e.g. transient 503) leaves the older
  bundle behind. Next push retries retention; eventually the
  delete succeeds. We log a warning per failure so users can
  notice persistent issues.

**CR-04 🟡 SHOULD-FIX — restore_bundle commits even when warnings list
is non-empty**
- If `fingerprints` insert fails (FK / schema mismatch) but
  `profiles` insert succeeded, we commit anyway and return
  `ok=True` with a warning. Caller sees "ok": True and may not
  notice the warning.
- **Fix candidate** (deferred): treat any `warnings` entry that
  starts with `<table> insert failed:` as a hard fail, raise, and
  let Flask convert to 500. Tradeoff: partial restores are
  sometimes better than "give up entirely". Worth a UX
  conversation before changing.

**CR-05 🟢 BENIGN — Cloud-fetch flow correctly clears modal state**
- Verified: `_cloudFetchAndRestore` closes the cloud picker
  modal BEFORE opening the local restore modal. If the user
  cancels mid-restore, the bytes are kept in the local
  `this._restoreFile` field — by design (lets them retry without
  re-downloading from S3).

**CR-06 🟢 BENIGN — Inspect endpoint is unauthenticated**
- `POST /api/backup/inspect` reads only the unencrypted header
  (magic, version, salt, payload_len, total bytes). No password
  required. This is the documented design — lets the UI preview
  bundle metadata before asking for the password. The salt is
  not secret (it's an input to the KDF, not an output). No fix.

## UI / UX

**UX-01 🟢 SHIPPED — Backup card placement**
- New "Backup & restore" card sits between the runtime self-check
  card and the Danger zone. Logical grouping: it's a non-
  destructive, profile-level operation that produces a file the
  user controls. Above the danger zone so users see "save first"
  before "delete forever".

**UX-02 🟢 SHIPPED — Master password input is type=password**
- All three password inputs use `type="password"` + autocomplete
  off + spellcheck off. The browser cannot autofill or remember
  these values; copy-paste from a password manager still works.

**UX-03 🟡 SHOULD-FIX — Modal close doesn't restore download submit
handler after a cloud-push session**
- `_submitCloudPush` swaps `sb.onclick` to `_cloudPushFromModal`.
- `_resetBackupModal` does NOT clear that onclick before the next
  modal open. So if the user clicks "Download backup" right after
  a cloud push, the submit button still points at cloud-push.
- **Fix needed** — clear `sb.onclick = null` in `_resetBackupModal`
  so re-opening the modal returns to the default
  `_submitBackup` path that's wired in `init()`.

**UX-04 🟡 SHOULD-FIX — Cloud restore "Fetch & restore" button
auto-submits inspect**
- Verified: `_cloudFetchAndRestore` calls `_submitInspect` directly
  after `_onRestoreFilePicked`. Inspect can fail (bad header,
  truncated download) — the user sees an error in step 1's status
  area but the button label they clicked said "Fetch & restore",
  not "Fetch & inspect". Mild label mismatch.
- Cosmetic — leave for a UX polish pass.

**UX-05 🟢 SHIPPED — Friendly category mapping for restore errors**
- Verified: HTTP 401 / 400 / 409 from `/api/backup/restore` are
  mapped to "wrong password" / "format error" / "collision —
  tick overwrite" respectively. Backend already returns category
  in JSON; UI consumes it.

**UX-06 🟡 SHOULD-FIX — No visible feedback while a 100MB+ bundle is
encrypting**
- `_submitBackup` shows "Encrypting…" in the modal status line
  but the button doesn't pulse or show a progress indicator.
- Encryption time is roughly 200ms / 10MB on a modern CPU plus
  KDF (≈300ms for 200k iterations). For a 100MB user-data-dir
  expect ~2.5 seconds where the UI freezes.
- **Fix candidate** (small): add a CSS spinner to the button
  during the encrypt window.

## Coverage / tests

**CV-01 🟢 SHIPPED — Captcha + drift + canary tests**
- 28 cases in `tests/test_db_runs_health_helpers.py`.
- Covers: empty results, day aggregation, in-flight exclusion,
  window respect, ascending order, zero-captcha buckets, totals,
  improving/degrading trend, flat-when-short-window, drift
  no-flag-when-healthy, score-drop flag, captcha-rate flag,
  critical/warn severity, sort order, window exclusion,
  runs_count_for_profile + isolation, every_n_runs gate (first
  run, off-cycle, on-cycle, disabled, fallback to config,
  unknown-profile fallthrough).

**CV-02 🟢 SHIPPED — Sync layer tests**
- 26 cases in `tests/test_backup_sync.py`.
- make_key/parse_key round-trip, _safe_segment, retention
  no-op/keeps-newest/zero-disabled/per-profile-isolation/per-host-
  isolation, target_from_config disabled/typo/dispatch-s3,
  push_profile end-to-end + retention-after-push,
  pull_profile most-recent/with-stamp/missing/unknown-stamp,
  list_remote_bundles filters + sort order, default ping +
  exception handling.

**CV-03 🟡 GAP — No live-network adapter tests**
- S3SyncTarget's actual boto3 calls aren't exercised — no live
  S3 / MinIO test fixture in CI. Same for Dropbox + SFTP.
- **Why deferred**: spinning up a containerized MinIO + an
  embedded SFTP server (paramiko-based) would let us cover the
  push/pull round-trip end-to-end. ~1 day of work; tracked for a
  future sprint.

**CV-04 🟢 SHIPPED — Edit-tool truncation guardrails**
- Two truncations during this sprint were caught by `node --check`
  on the JS file before the diff was committed; both restored
  from git HEAD via the bash heredoc pattern documented in
  `docs/audit/sprint-2-profile-workflow-audit.md`. No silent
  regressions shipped.

## Endpoints reference

For onboarding new contributors:

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/profiles/<name>/backup` | POST | Encrypt + download .ghs-bundle |
| `/api/backup/inspect` | POST | Header-only preview (no password) |
| `/api/backup/restore` | POST | Decrypt + restore (multipart upload) |
| `/api/backup/sync/test` | POST | Connectivity check for configured target |
| `/api/backup/sync/list` | GET | List remote bundles (filter by host/profile) |
| `/api/profiles/<name>/backup/sync/push` | POST | Encrypt + push to cloud |
| `/api/backup/sync/pull` | POST | Download a remote bundle (binary) |
| `/api/backup/sync/delete` | POST | Hard-delete a remote bundle |

## Config keys (Sprint 8.2)

```
backup.sync.enabled              bool   master switch
backup.sync.provider             str    "s3" | "dropbox" | "sftp"
backup.sync.keep_last            int    retention policy (0 = off)

# S3 / R2 / MinIO / Wasabi / B2
backup.sync.s3.bucket            str
backup.sync.s3.access_key        str
backup.sync.s3.secret_key        str    move to vault next sprint
backup.sync.s3.region            str
backup.sync.s3.endpoint_url      str
backup.sync.s3.aws_profile       str

# Dropbox
backup.sync.dropbox.access_token   str  move to vault
backup.sync.dropbox.refresh_token  str
backup.sync.dropbox.app_key        str
backup.sync.dropbox.app_secret     str  move to vault
backup.sync.dropbox.root_path      str

# SFTP
backup.sync.sftp.host             str
backup.sync.sftp.port             int   default 22
backup.sync.sftp.username         str
backup.sync.sftp.password         str   move to vault
backup.sync.sftp.key_file         str
backup.sync.sftp.key_data         str   inline PEM
backup.sync.sftp.key_passphrase   str   move to vault
backup.sync.sftp.base_path        str   default "."
```

## Action items for next session

1. **UX-03 fix** — clear `sb.onclick = null` inside
   `_resetBackupModal` so re-opening the password modal after a
   cloud-push session returns to the local-download submit. ~5 min.
2. **CR-01 / CR-04** — discuss with operator whether stamp-collision
   prevention and warn-on-partial-restore are worth tightening.
3. **SA-06** — move sync credentials into the vault. Touch points:
   `target_from_config()` reads via `vault.get(...)` instead of
   `db.config_get(...)` for the `*.access_key`, `*.secret_key`,
   `*.password`, `*.access_token`, `*.app_secret`, `*.passphrase`
   keys. Add a Settings-UI form to enter them.
4. **CV-03** — set up live-adapter tests under
   `tests/integration/` with a docker-compose MinIO + paramiko
   `transport.Transport.start_server()` SFTP for push/pull round-
   trips.
5. **Settings UI** — there is currently no dashboard form to
   configure `backup.sync.*`. Power users can poke
   `db.config_set` from the REPL; a proper Settings → Backup
   page is the obvious next deliverable.
