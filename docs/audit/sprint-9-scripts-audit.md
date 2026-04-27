# Sprint 9 audit — Scripts: UI, backend, runner, templates

Comprehensive review pass over the scripts feature surface area:

* `dashboard/pages/scripts.html` — library + editor view
* `dashboard/js/pages/scripts.js` — 3 379 lines, library + flow editor
* `ghost_shell/dashboard/server.py` — 13 endpoints under `/api/scripts/*`
  + `/api/profiles/<n>/script`
* `ghost_shell/db/database.py` — 9 helper methods (`scripts_list`,
  `script_get`, `script_create`, `script_update`, `script_delete`,
  `script_assign_to_profile`, `script_set_pinned`,
  `script_resolve_for_profile`, …)
* `ghost_shell/actions/runner.py` — 2 dispatch tables
  (`ACTION_HANDLERS`, `LOOP_ACTION_HANDLERS`) + the unified-flow
  executor (`run_flow` / `_exec_steps`) with its own dispatch
* `ghost_shell/scripts_templates/` — 17 bundled templates

Severity legend: 🔴 critical · 🟡 should-fix · 🟢 informational.

## Templates — selectors & logic

**TPL-01 🔴 CRITICAL — `:has-text()` is Playwright-only, not CSS**
- Templates using it will hit `InvalidSelectorException` at the
  first matching step:
  - `instagram_login.json` — `button:has-text('Not now'), button:has-text('Не сейчас')` (steps 11, 13)
  - `reddit_login.json` — `button[type='submit'], button:has-text('Log In')` (step 8) — the comma-fallback saves it because the first selector matches, but the second branch is dead.
  - `google_account_login.json` — `#identifierNext, button[type='button']:has-text('Next')` and `#passwordNext, button[type='button']` (steps 6, 11) — same comma-fallback applies, the second branch is dead.
- **Fix**: replace with XPath via `_act_click_selector`'s wrapper
  (e.g. `xpath=//button[contains(., 'Not now')]`) OR drop the
  `:has-text()` clause entirely and use the language-neutral
  attribute selector. For Instagram in particular, there's a
  better selector: `div[role='dialog'] button._a9--._a9_1`. For now
  I'll strip the `:has-text()` branches so the comma-fallback
  doesn't raise for users on Selenium ≥ 4.18 which is stricter.

**TPL-02 🔴 CRITICAL — MetaMask template references Vault item by
name placeholder that the runner can't resolve in single-script flow**
- `metamask_unlock.json` step 2 uses `value: "{vault.metamask_main.password}"`.
- Runner's `_interpolate` in `RunContext` resolves `{vault.<id>.<field>}`
  against the in-flight vault, but ONLY when the script is run via
  `run_flow()`. The legacy `dispatch_pipeline` (used for the
  ad-pipeline `ACTION_HANDLERS`) doesn't go through the same
  interpolation path.
- The MetaMask flow is single-script-flow exclusive (uses
  `extension_*` actions which only exist in `flow_actions`
  dispatcher), so it works — but it's worth a comment in the
  template's description so users on the WAITING-LIST don't try to
  paste these steps into the ad-pipeline.

**TPL-03 🟡 SHOULD-FIX — Login templates lack 2FA / cookie-banner
handling**
- Facebook, Instagram, LinkedIn, Twitter/X, Google, Reddit, GitHub
  templates all jump straight from "click submit" to "wait for
  authenticated page". They don't handle:
  - GDPR cookie banners on EU IP exits (Facebook/Google pop a full-screen consent modal that BLOCKS the form fields).
  - 2FA prompts (TOTP / SMS / push notifications).
  - "We don't recognize this device" interstitials.
  - Captcha challenges (reCAPTCHA / hCaptcha).
- Net effect: on a fresh profile or an unfamiliar IP, the script
  hangs on `wait_for` until the timeout, the run is logged as
  "errored", and the user has no signal *why*.
- **Fix candidate (Sprint 9.1)**: add an `if` step at the top of
  each login template that detects the cookie-banner element and
  clicks "Accept" if present; ditto for the "trust this device"
  interstitial. Document 2FA as a separate template
  (`vault_totp_unlock.json`).

**TPL-04 🟡 SHOULD-FIX — YouTube / Reddit / NYT selectors are
volatile**
- `youtube_warmup.json` uses `ytd-video-renderer #video-title` —
  works today, has been broken twice in the last 18 months.
- `reddit_login.json`'s `header, [data-testid='community-list']`
  doesn't match the new (post-redesign) Reddit UI.
- `news_dwell_session.json` uses `a[data-qa='homepage-link']`
  which the WaPo redesign deprecated.
- These need a quarterly refresh. Tracking issue rather than fix.

**TPL-05 🟢 SHIPPED — Vault placeholder description copy is solid**
- `facebook_login.json` description includes step-by-step
  instructions for binding Vault credentials. Good UX, copy can
  be reused for the other login templates.

**TPL-06 🟡 SHOULD-FIX — `proxy_test.json` doesn't actually verify
the IP matches the configured proxy**
- It visits `ipify` and `browserleaks/ip` and dwells. It doesn't
  call `extract_text` on a known JSON path or store the result.
  A user looking at the run log sees "ran" for every step but
  has no in-DB record of what IP was reported.
- **Fix candidate**: add `extract_text` with `selector: 'pre'`
  and `store_as: 'reported_ip'`, then a final `execute_js` step
  that throws if `reported_ip` doesn't match
  `ctx.profile.expected_country` (when set).

## Runner / backend

**RN-01 🟡 SHOULD-FIX — Two dispatch tables share action names**
- `ACTION_HANDLERS` (line 1471) — for ad-pipeline steps run on
  every detected ad.
- `LOOP_ACTION_HANDLERS` (line 1796) — for top-level loop-script
  steps.
- Plus the `_exec_single` dispatch in `run_flow()` (line 2920+) —
  for the unified single-script flow.
- All three share names like `visit`, `dwell`, `scroll`. The
  three handlers behave SUBTLY differently (e.g. `dwell` in the
  loop scope hits the watchdog heartbeat, in the ad scope it
  doesn't). When a user authors a step in the editor, the
  inspector pulls action metadata from a single source — which
  means the help text is correct for one dispatch context and
  wrong for the others.
- **Fix candidate (out of scope for this audit)**: emit an action
  catalog endpoint `GET /api/scripts/action-catalog?context=...`
  with `context ∈ {ad, loop, flow}` so the editor inspector can
  show the right help.

**RN-02 🟡 SHOULD-FIX — script_update has no optimistic concurrency
check**
- Two dashboard tabs editing the same script overwrite each other
  silently. Last-writer-wins. The DB has `updated_at` but the
  endpoint doesn't read or compare it.
- **Fix candidate**: GET returns `etag = sha256(updated_at + flow)`,
  PUT requires `If-Match: <etag>` header. Out of scope for this
  audit but worth tracking.

**RN-03 🟢 BENIGN — script_delete cascades to profile assignments**
- Verified: `script_delete` sets `profiles.script_id = NULL` for
  every profile that referenced it BEFORE issuing the DELETE. So
  affected profiles fall through to default-script resolution at
  the next run. Good.

**RN-04 🟢 BENIGN — Default script can't be deleted**
- `script_delete` raises ValueError if `is_default=1`. Correct —
  forces the user to promote another script first.

**RN-05 🟡 SHOULD-FIX — `_spawn_run` race: assign + run not atomic**
- `/api/scripts/<id>/run` does `script_assign_to_profile()` then
  `_spawn_run(name, script_id_override=script_id)`. Between those
  two calls, another tab can re-assign the profile to a different
  script. The override on `_spawn_run` saves us for THIS run, but
  the persistent assignment may not match what the user intended.
- Effect: subsequent runs start using the OTHER script.
- **Fix candidate**: keep `script_id_override` on the run row and
  show "[overridden] script X used for this run" in the run
  detail; OR wrap assign+spawn in a DB transaction with row-level
  locking on profiles.

**RN-06 🟢 BENIGN — `validate flow` recursion handles nested
containers**
- `_validate(steps, path)` walks `steps` / `then_steps` /
  `else_steps`. Verified for `if`, `loop`, `foreach_ad`. Each
  step needs a `type`. No infinite-recursion guard, but the
  client can't easily smuggle a self-referential JSON in (the
  POST body is parsed via Flask's `get_json` which doesn't
  preserve cycles). Benign.

**RN-07 🟢 BENIGN — Script-templates loaded from disk on every call**
- `/api/scripts/templates` re-reads + re-parses 17 JSON files per
  request. ~5ms total. Comment in the endpoint acknowledges this
  is intentional for fast dev iteration. No fix needed.

## Race conditions

**RC-A 🟡 SHOULD-FIX — Editing an active script while it runs**
- Operator opens script in editor, clicks Save while a profile is
  mid-run with that same script.
- Runner reads `flow` JSON ONCE at run start (snapshot via
  `script_resolve_for_profile`), so the in-flight run keeps the
  old steps. But subsequent runs immediately use the new flow.
- This is actually the right semantics, but it's surprising. The
  editor should show a banner "N profiles currently running this
  script" so the user knows their save will affect the next
  iteration.
- **Fix candidate (UI)**: add a `runs_active_for_script(id)` count
  to `GET /api/scripts/<id>` and surface it in the editor header.

**RC-B 🟡 SHOULD-FIX — Two-tab pinning conflict**
- `/api/scripts/<id>/pin` does a full replace of the pinned
  profiles list. Two tabs pinning different profiles end up with
  whichever wrote last.
- **Fix candidate**: `POST /api/scripts/<id>/pin/<profile>` for
  add and `DELETE` for remove (idempotent), instead of the
  whole-list replace.

**RC-C 🟢 BENIGN — Concurrent `script_assign_to_profile` is safe**
- The assign helper does a single UPDATE of profiles row, with the
  `use_script_on_launch=1` flip in the same transaction. SQLite
  `with conn:` block guarantees atomicity.

**RC-D 🟡 SHOULD-FIX — Templates → New script: name collision
race**
- `_openTemplatesModal._submitTemplate` checks
  `this.scripts.some(x => x.name === name)` to bump the suffix,
  but `this.scripts` is from the last library load. A second
  operator just created `Facebook login (2)` between the load
  and the click. The POST hits SQLite's UNIQUE constraint on
  scripts.name and surfaces "Could not create:".
- **Fix candidate**: server-side: when create returns 409 / a
  UNIQUE violation, suggest the next available name in the error
  body so the client can retry without re-prompting the user.

## UI

**UX-A 🟢 SHIPPED — Editor zoom + palette are well-built**
- Canvas has Ctrl/Cmd+wheel zoom, keyboard shortcuts, and a
  zoom-level chip. Step palette filtered by search. Inspector
  pane on the right with action-specific config. All standard.

**UX-B 🟡 SHOULD-FIX — No "preview run" without leaving the editor**
- Save → navigate to profile → click Run is the only way to test
  a script. Editor would benefit from a "Dry run on profile X"
  button that spawns a one-shot run with the unsaved flow against
  the user's pick of profile, without persisting the change.
- **Fix candidate (Sprint 9.2)**: `POST /api/scripts/dry-run`
  accepts `{profile, flow}` directly, no DB write.

**UX-C 🟡 SHOULD-FIX — Templates modal lacks "preview steps" mode**
- Card shows name, description, category, step count, tags. No
  way to inspect the flow before clicking "Use this". User has to
  create + delete to peek at recipes they don't end up using.
- **Fix candidate**: add a "Show steps" toggle on each card that
  expands a collapsed `<details>` listing each step's `type` +
  short summary.

**UX-D 🟢 SHIPPED — Library has search by name/desc/tag**
- Verified: `library-search` input filters cards client-side via
  the `_renderLibrary` filter. Good.

**UX-E 🟢 SHIPPED — Library cards show pinned-profile chips**
- Up to 3 chips inline + an overflow "+N" pill. Click navigates
  to the profile detail page. Good.

**UX-F 🟡 SHOULD-FIX — Templates modal doesn't propagate vault-
binding instructions**
- `description` is rendered as plain text. The Facebook template's
  description includes literal `<- click 🔑 to pick username from
  Vault ->` with formatting that's hard to read in plain text.
- **Fix candidate**: render description as Markdown (we already
  use `marked.js` elsewhere) so the inline instructions
  highlight properly.

## Coverage / tests

**CV-A 🟡 GAP — No test coverage for script_resolve_for_profile**
- The `resolve` helper drives every run start. Tests should
  cover: assigned + active → returns assigned; assigned + missing
  → returns default; no assignment + default exists → returns
  default; no assignment + no default → returns None.

**CV-B 🟡 GAP — No template smoke-test**
- The 17 templates are JSON files. CI should at minimum:
  1. Parse each as JSON (catches syntax errors).
  2. Validate every step has a `type`.
  3. Validate every `type` value exists in at least one of
     `ACTION_HANDLERS` / `LOOP_ACTION_HANDLERS` / the unified-flow
     dispatcher.
- This catches regressions where a template references an action
  that was renamed or removed.

## Action items for next session

1. **TPL-01 fix (5 min)** — strip `:has-text()` clauses from the
   3 affected templates so the comma-fallback doesn't raise on
   strict Selenium versions.
2. **TPL-03 design (1 day)** — add a `dismiss_cookie_banner`
   conditional `if` step at the top of every login template,
   using a generic CSS-selector union for the major consent
   frameworks (OneTrust, Quantcast, plain GDPR-banner, etc.).
3. **CV-B (30 min)** — add `tests/test_script_templates.py` with
   the 3-check smoke suite.
4. **RC-A UI (1 hr)** — return `active_runs` count on
   `GET /api/scripts/<id>` and render a banner in the editor.
5. **UX-B preview (1 day)** — `/api/scripts/dry-run` endpoint +
   "Try it" button in the editor toolbar.
6. **RN-02 etag (4 hr)** — optional ETag/If-Match concurrency on
   script PUT.
