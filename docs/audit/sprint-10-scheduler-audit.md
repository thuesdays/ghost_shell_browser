# Sprint 10 audit — Scheduler: race conditions and scenarios

Comprehensive review pass over the scheduler subsystem:

* `ghost_shell/scheduler/scheduler.py` — 798 lines, the standalone
  daemon that spawns runs.
* `ghost_shell/scheduler/cron.py` — 191 lines, custom 5-field cron
  parser.
* `ghost_shell/dashboard/server.py` — `/api/scheduler/*` endpoints
  + `_scheduler_pid_alive`, `SCHEDULER_PID_FILE` lifecycle.
* `ghost_shell/db/database.py` — `scheduled_tasks_*` helpers (4
  methods) + the `scheduler.*` config keys.
* `dashboard/pages/scheduler.html` + `dashboard/js/pages/scheduler.js`
  — the operator UI.

Severity legend: 🔴 critical · 🟡 should-fix · 🟢 informational.

## 🚨 Architecture-level findings

**ARCH-01 🔴 CRITICAL — `scheduled_tasks` table is dead code**
- `scheduled_tasks` is created in the DB schema (line 290), has 4
  CRUD helpers (`scheduled_tasks_list`, `scheduled_task_create`,
  `scheduled_task_update`, `scheduled_task_delete`), and is exposed
  via `GET /api/scripts/<id>/schedules` + `POST /api/scripts/<id>/schedules`.
- **The scheduler daemon never reads it.** `scheduler.py:main()`
  loops over `scheduler.*` global config keys (target_runs,
  active_hours, min/max_interval, schedule_mode + cron_expression).
  No call to `scheduled_tasks_list` anywhere in `scheduler.py`.
- Net effect: a user creates a per-script cron schedule via the
  UI, sees the row appear in the DB, but it's silently ignored.
  The user expects "Script X runs every Mon at 9am" but the
  scheduler keeps doing whatever the global config says.
- **Fix candidate (Sprint 10.1)**: extend the tick loop to:
  1. compute `next_fire` for every enabled `scheduled_tasks` row;
  2. when `next_run_at <= now`, spawn the script's flow against
     the listed profiles via `_spawn_via_dashboard`;
  3. set `last_run_at = now`, recompute `next_run_at`.
  This is the design `scheduled_tasks` was created for — the
  table layout already has the right columns.

**ARCH-02 🟡 SHOULD-FIX — Two scheduling paradigms compete**
- The `scheduler.*` config-key model is "one global cron / density
  loop, picks profiles randomly from a pool".
- The `scheduled_tasks` model is "per-script cron, fires the
  script's own flow on a specific profile list".
- Even if ARCH-01 is fixed, the user has TWO ways to schedule a
  workload and the docs don't say which to pick when. UX hazard.
- **Fix candidate**: pick one as primary (`scheduled_tasks` is
  more flexible), demote the other to a single global "default
  density loop" toggle.

## Scheduler daemon — race conditions & lifecycle

**SC-01 🟡 SHOULD-FIX — `_iter_no` resets to 0 on restart but
runs_today persists**
- After a Stop+Start cycle mid-day, `_iter_no` starts at 1 again.
- `runs_today()` keeps counting from midnight, so the log says
  "tick #1 ... Run 17/30" — the iteration counter and the run
  counter disagree. Cosmetic but confusing.
- **Fix**: bump iter_no by `runs_today()` at start, OR drop iter_no
  in favor of run-number-only logging.

**SC-02 🟢 BENIGN — Duplicate scheduler protection**
- `_scheduler_pid_alive` does cmdline-validation: a recycled PID
  is treated as "not ours" and the stale file is cleared. Fix
  applied in an earlier sprint (mentioned in the comment block at
  line 3201). Verified safe.

**SC-03 🔴 CRITICAL — DB connection NOT thread-safe across sched
heartbeat thread + main loop**
- `scheduler.py:main()` spawns `_hb_ticker` thread that calls
  `get_db().config_set(...)` every 15s.
- Main loop also calls `get_db()` for `runs_today`,
  `consecutive_failures`, `heartbeat({...})` etc.
- `DB._local = threading.local()` is class-level (we discovered
  this in Sprint 8.3 fixture work), so each thread gets its own
  connection — that's GOOD. **But** the connection is opened with
  `check_same_thread=False`, isolation_level=None, WAL mode. WAL
  + busy_timeout makes concurrent writes safe.
- Verdict: actually fine. The class-level threading.local is the
  right design for this. Keeping the finding open as informational
  because the comment block doesn't explain WHY it's safe — a
  future contributor might "fix" what isn't broken.

**SC-04 🟡 SHOULD-FIX — Heartbeat thread keeps writing during shutdown**
- `_hb_ticker` writes heartbeat every 15s. When `_shutdown=True`,
  it bails out of its 15-sleep. But `mark_stopped()` runs in the
  main thread's `finally` block, which clears
  `scheduler.heartbeat_at = None`. There's a window where:
  1. Main loop hits SIGINT, sets `_shutdown=True`
  2. Heartbeat thread is mid-iteration, writes a fresh timestamp
  3. Main thread clears it via `mark_stopped()`
  4. Heartbeat thread wakes from 15s sleep, sees `_shutdown=True`, exits
- The 15s window is within tolerance (status check is 120s threshold),
  but the flicker is real.
- **Fix**: heartbeat thread should also check `_shutdown` before
  EVERY config_set, not just at the top of its sleep.

**SC-05 🟡 SHOULD-FIX — Quota-met sleep_until_window has wrong
tomorrow boundary**
- `runs_today >= target_runs` → `sleep_sec = time_until_next_window(active_hours)`.
- `time_until_next_window` returns "until h_start TODAY if we're
  before, else h_start TOMORROW". After 7am the same day, this
  correctly bumps to tomorrow.
- BUT: the wake-up time after quota fills is calculated from "next
  start of active window", not "midnight tomorrow when runs_today
  resets". If active_hours=[7,20] and we hit quota at 14:00, we
  sleep 17h to 7am next day — fine.
- Edge case: if active_hours=[0,24] (always-on), `time_until_next_window`
  returns 60s minimum because `now.time() >= dtime(0)` is always
  true → target moves to tomorrow at 00:00 → small window. Effect:
  the scheduler busy-waits in 60s slices when quota is met on a
  24/7 setup. Wasteful CPU and writes a heartbeat every 15s the
  whole time.
- **Fix**: when quota is met, sleep until midnight (when
  runs_today rolls over), not until h_start.

**SC-06 🟡 SHOULD-FIX — `runs_today()` uses `LIKE 'YYYY-MM-DD%'`
on `started_at` — assumes ISO format**
- A run started at 23:59:59 with `finished_at = next-day-00:00:01`
  counts toward "today" because it STARTED today. Reasonable but
  the log message says "runs today" which a user might read as
  "completed today".
- More serious: if a future migration changes `started_at` to
  include timezone offset (e.g. `2025-04-27T14:30:00+03:00`), the
  `LIKE 'YYYY-MM-DD%'` filter still works (the prefix matches),
  but the day boundary is computed against local-tz `today`. A
  user in UTC+3 who runs through midnight UTC sees the count
  reset 3 hours late.
- **Fix candidate**: use `strftime('%Y-%m-%d', started_at)` on
  the SQL side and pass `date('now', 'localtime')` for the boundary.

**SC-07 🔴 CRITICAL — Dashboard-routed spawn timeout doesn't kill the run**
- `_wait_for_run_via_dashboard` polls `/api/run/status` for up to
  30 minutes. On timeout it POSTs `/api/run/<id>/stop` (best-effort)
  and returns `(-1, elapsed)`.
- The timeout ITSELF is fine, but the scheduler logs `exit=-1`
  and continues to the NEXT iteration. If the run is still alive
  on the dashboard side after the stop POST (network glitch,
  stop-while-busy), the next scheduler tick can spawn ANOTHER run
  for the same profile because the dashboard's pre-spawn guard
  only refuses when there's a known live run for that profile —
  and the just-stopped run might already be marked finished by
  then.
- Worst case: two main.py processes wedge fighting for the same
  profile dir.
- **Fix candidate**: after the stop POST, poll `/api/run/status`
  one more time with a short timeout to confirm `running=False`
  before returning. If still running, escalate to
  `taskkill /F /T /PID <pid>` via `/api/admin/reap-zombies` or
  similar, then return.

**SC-08 🟡 SHOULD-FIX — Direct-Popen fallback has NO timeout in
the dashboard-offline path of the BATCH branch**
- Line 761: `rc = handle.wait(timeout=30 * 60)` for direct
  Popen — same 30min cap as dashboard path. Verified.
- Single-profile path line 499: ditto. Verified.
- **However**: `_spawn_via_dashboard` also has a 5s timeout, so
  a transient dashboard hang causes the scheduler to fall through
  to direct Popen on every tick. If the dashboard recovers
  partway, the scheduler keeps using direct Popen for the rest of
  the session because the URLError caches "offline" implicitly via
  short-circuit. Actually no, it doesn't cache — every tick re-tries.
  But within ONE iteration's wait, the dashboard going down between
  spawn and status-poll means the `_wait_for_run_via_dashboard`
  loop spins for 30min logging "status poll error" every 5s and
  the scheduler is hung on a single run.
- **Fix**: bound consecutive status-poll failures (e.g. > 12
  failures in a row = bail), assume the run is lost, schedule a
  reap, move to next iteration.

**SC-09 🟢 BENIGN — Group-based pick_batch falls back to
profile_names on group lookup failure**
- Verified with the try/except around `db.group_get(int(group_id))`.
  Good defensive coding.

**SC-10 🟡 SHOULD-FIX — `_round_robin_idx` is a module-level int**
- Survives across tick iterations (good) but resets on scheduler
  restart (bad — a Stop+Start mid-day rewinds round-robin to
  profile #0 even if we already cycled through 5 profiles). Not a
  correctness bug, but breaks the user's mental model of "fair
  rotation across the day".
- **Fix**: persist `scheduler.round_robin_idx` to DB, restore on
  startup. Cheap.

**SC-11 🟡 SHOULD-FIX — `consecutive_failures()` walks runs ORDER
BY id DESC LIMIT 20 — only sees the last 20 runs**
- For a high-frequency scheduler (interval=30s), 20 runs is < 10
  minutes of history. If 19 failures happened in burst then a
  success, the count returns 0 even though the user wants the
  alert. Conversely, if 20 failures happened, success at #21 hides
  it from the count.
- **Fix**: walk until first success OR until `started_at < scheduler.started_at`.
  Drop the 20-row LIMIT, but keep an upper bound (e.g. 200 rows)
  for safety.

**SC-12 🟢 BENIGN — `signal.signal(SIGTERM, ...)` registered at
module-import time**
- When the scheduler is imported by the dashboard for unit tests
  or the cron module, registering a SIGTERM handler at module
  scope is awkward — it overrides whatever the importer already
  set. In practice tests that import this module are rare and
  the test harness re-registers its own handlers, so benign.
- Minor cleanup: move signal.signal calls inside `main()` so
  imports don't side-effect.

## Cron parser — robustness

**CR-01 🟢 BENIGN — `next_fire` walks 1-minute increments up to 1 year**
- Worst case: an unsatisfiable expression (e.g. `0 0 31 2 *` —
  Feb 31 doesn't exist) loops 526k iterations before bailing. Comment
  acknowledges this. ~5s blocking. Caller should not invoke this
  in a hot path; the scheduler calls it at most once per tick.
- Verified — fine.

**CR-02 🟡 SHOULD-FIX — Cron does NOT honor active_days when in
"cron" mode**
- `is_active_day` gates EVERY mode — verified at the top of the
  tick loop. So if user sets cron `*/5 * * * *` and active_days=[1,2,3,4,5]
  (weekdays), the cron expression's own day-of-week field is
  ignored on weekends because the higher-level gate sleeps
  through them. This is mostly intentional (active_days is the
  master switch), but cron's own DOW field offers finer control.
- Worse: the cron `next_fire` returns the next match including
  weekends, but the gate then sleeps until Monday — the dashboard
  says "next run at 14:00 Sat" but actually fires at 00:00:01 Mon.
  Misleading.
- **Fix**: in cron mode, when `is_active_day` is False, compute
  next_fire AFTER the active_days gate so the displayed next-run-time
  is honest.

**CR-03 🟢 BENIGN — `describe()` returns "X×Y min/hour combos" for
complex expressions**
- Cosmetic. The full descriptive output for, e.g., `*/15 9-17 *
  * 1-5` would be "every 15 minutes between 9am and 5pm on
  weekdays" but we render "4×9 min/hour combos; on Mon, Tue, Wed,
  Thu, Fri". Functional but ugly.

## Dashboard endpoints

**EP-01 🟢 SHIPPED — Stop endpoint is robust**
- Three-stage escalation: terminate → kill → taskkill /F /T. Cleans
  up children FIRST so chrome.exe doesn't orphan. Always clears
  pidfile + heartbeat so /status reports stopped after a partial
  failure. Solid.

**EP-02 🟡 SHOULD-FIX — `/api/scheduler/start` returns 409 on
already-running but `_scheduler_pid_alive` does mtime cleanup**
- The first call to `_scheduler_pid_alive` may DELETE the stale
  file (good) but a parallel POST that comes between the deletion
  and the next call sees no pidfile and successfully spawns a
  second scheduler.
- Race window: a few milliseconds. Probability is low but not
  zero — a user who double-clicks the Start button can end up
  with two scheduler processes both writing heartbeats.
- **Fix**: Lock around the start handler. `flock(SCHEDULER_PID_FILE)`
  on Linux, `msvcrt.locking` on Windows. Or simpler: use the
  pidfile's atomic creation (`os.O_EXCL`) instead of "check then
  create".

**EP-03 🟢 BENIGN — `/api/scheduler/status` calculates health from
3 signals (PID, heartbeat freshness, heartbeat presence)**
- The `crashed` state (no PID + recent heartbeat < 5min) gives the
  user a useful "scheduler died uncleanly" hint. Good UX.

**EP-04 🟡 SHOULD-FIX — Status endpoint doesn't return active-runs count**
- `runs_today` is returned but the user has no way to see "is the
  scheduler currently running a profile right now?" without
  cross-referencing /api/runs. Should include
  `active_runs_count: int` so the UI can show a green dot
  on the scheduler card while a run is in flight.

**EP-05 🟢 BENIGN — Reset-fails endpoint atomically updates `started_at`**
- Ensures the consecutive_failures() walker sees zero history
  after the reset. Good.

## scheduled_tasks endpoints (the dead-code path)

**ST-01 🔴 CRITICAL — Endpoints exist but produce no behaviour**
- `POST /api/scripts/<id>/schedules` writes a row that the
  scheduler will never read (per ARCH-01). The endpoint should
  either:
  1. Be marked deprecated until ARCH-01 is fixed, OR
  2. Return 503 with a "scheduling backend not connected" message
     until the integration is shipped.
- Either way, currently we accept rows that do nothing.

## Race conditions across processes

**RP-01 🟡 SHOULD-FIX — Manual run via dashboard collides with
scheduler tick**
- User clicks "Run profile_01" in the dashboard at 14:00:30.
- Scheduler tick happens at 14:00:35, picks profile_01 from
  pool (random), tries to spawn.
- Dashboard's pre-spawn guard catches the collision (one-run-per-profile
  rule from earlier sprints), returns "already running" to the
  scheduler's POST.
- Scheduler logs `exit=-1` and counts it as a failure. Backoff
  multiplier kicks in.
- Net effect: a user who spam-clicks Run inadvertently triggers
  the scheduler's failure-pause logic.
- **Fix**: scheduler should treat HTTP 409 from /api/run/start
  as "skip this iteration, not a failure". The current code
  doesn't differentiate.

**RP-02 🟢 BENIGN — Two scheduler instances after a crash**
- The `scheduler.pid` config key + the file lock both protect
  against this. Even if the file lock fails, the second instance
  immediately writes a new pidfile that the first instance's
  next tick will treat as theirs (PID matches), but the second
  instance's heartbeat will overwrite the first's. Whichever wins
  the heartbeat race becomes "the scheduler". Not ideal but not
  destructive — both write the same heartbeat key, both spawn
  runs that go through the dashboard's per-profile guard.

**RP-03 🟡 SHOULD-FIX — `kill_chrome_for_user_data_dir` from the
dashboard's reap-zombies button can kill scheduler-spawned chrome**
- The reap helper walks every chrome.exe whose `--user-data-dir`
  matches a profile path. If the scheduler is running profile X
  and the user clicks "Reap zombies" while an admin tab is open,
  the helper kills the live chrome.
- Hard to fix without per-profile distributed locking. Document
  as a known foot-gun.

## UI

**UI-01 🟡 SHOULD-FIX — Status card doesn't differentiate "spawning
a run" vs "between runs"**
- Both states show "Healthy" + "Next run at HH:MM". The user
  can't see "we're mid-spawn, next run hasn't been queued yet".
  Add the active_runs_count from EP-04.

**UI-02 🟢 SHIPPED — Cron expression preview**
- `dashboard/js/pages/scheduler.js` calls `cron.describe()` for a
  live tooltip. Good UX.

## Action items for next session

1. **ARCH-01 + ST-01 (1-2 days)** — wire `scheduled_tasks` into
   the tick loop. This is the biggest correctness improvement
   available. Either implement it or remove the dead UI flow.
2. **SC-07 (4 hr)** — verify run is actually dead before
   returning from timeout path; add `active_runs_count` to
   /status (EP-04) so the UI can show "running now" vs "idle".
3. **RP-01 (1 hr)** — scheduler handles HTTP 409 from
   /api/run/start as "skip, not fail".
4. **SC-04, SC-05, SC-06, SC-10 (~half day total)** — small
   correctness fixes to heartbeat shutdown, quota-met sleep,
   timezone boundary, round-robin persistence.
5. **SC-11 (30 min)** — drop the 20-row LIMIT in
   `consecutive_failures()`, walk to first success or
   session boundary.
6. **EP-02 (30 min)** — `os.O_EXCL` atomic pidfile create.
7. **CR-02 (30 min)** — re-order cron-mode gating so the
   displayed next-run-time is honest under active_days.
