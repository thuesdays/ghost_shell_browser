// ═══════════════════════════════════════════════════════════════
// pages/scripts.js — scripts library + flow editor
//
// Two modes in one page:
//
//   LIBRARY   (default)  — grid of all saved scripts. Each card
//                          shows name/description/step count/list
//                          of profiles using it. Click a card to
//                          enter editor mode for that script.
//
//   EDITOR    (scoped)   — unified-flow builder for a single
//                          script. Has its own Save, Export,
//                          Delete, Make-default buttons.
//
// State transitions happen in-page (no route change). `currentScript`
// is null in library mode, or the loaded {id, name, description,
// flow} dict in editor mode.
//
// Backend endpoints used:
//   GET/POST   /api/scripts
//   GET/PUT/DELETE  /api/scripts/<id>
//   GET        /api/actions/catalog
//   GET        /api/actions/condition-kinds
// ═══════════════════════════════════════════════════════════════

const ScriptsPage = {
  catalog:        [],
  conditionKinds: [],
  scripts:        [],     // library list (summary only)
  currentScript:  null,   // {id, name, description, flow, is_default} when editing
  flow:           [],     // current editing flow
  selection:      null,
  dirty:          false,
  _arrowsRAF:     null,

  // ── Lifecycle ────────────────────────────────────────────────

  async init() {
    await this.loadCatalog();
    await this.loadConditionKinds();
    await this.loadLibrary();

    this.wireLibraryHeader();
    this.wireEditorHeader();
    this.wirePalette();
    this.wireKeyboard();
    this.wireVarPicker();
    this.wireNewScriptModal();
    this.wireCanvasZoom();

    window.addEventListener("beforeunload", e => {
      if (this.dirty) { e.preventDefault(); e.returnValue = ""; }
    });
    window.addEventListener("resize", () => this._scheduleArrowsRedraw());

    this._showLibrary();
  },

  // ── Figma-style canvas zoom ──────────────────────────────────
  // Ctrl/Cmd + wheel anywhere inside .canvas-flow-wrap rescales
  // the inner .canvas-flow via CSS transform: scale(--zoom). Plain
  // wheel keeps default vertical scroll. Three toolbar buttons
  // (− / + / ⌂) provide a click affordance and a reset target.
  // Level persists in localStorage per editor session.
  wireCanvasZoom() {
    const ZOOM_KEY  = "scripts.canvas_zoom";
    const MIN_ZOOM  = 0.30;
    const MAX_ZOOM  = 3.00;
    const STEP      = 1.10;     // 10% per wheel tick / button press

    // Restore saved zoom or default to 1.0
    let saved = parseFloat(localStorage.getItem(ZOOM_KEY) || "1");
    if (!isFinite(saved) || saved < MIN_ZOOM || saved > MAX_ZOOM) saved = 1.0;
    this._zoom = saved;

    const apply = (newZoom, focal) => {
      const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, newZoom));
      this._zoom = z;
      const flow = document.getElementById("canvas-flow");
      if (flow) flow.style.setProperty("--zoom", String(z));
      const lvl = document.getElementById("canvas-zoom-level");
      if (lvl) lvl.textContent = `${Math.round(z * 100)}%`;
      localStorage.setItem(ZOOM_KEY, String(z));
      // Re-draw arrows after the transform settles -- their SVG
      // coords are computed against the visible bounding boxes.
      this._scheduleArrowsRedraw();

      // Keep the focal point under the cursor: adjust scrollLeft/Top
      // so the page point under the mouse stays put across zoom.
      // Without this, zooming feels like the canvas jumps away from
      // the cursor.
      if (focal) {
        const wrap = document.querySelector(".canvas-flow-wrap");
        if (wrap) {
          // ratio of NEW vs OLD is just newZoom/oldZoom; we don't
          // track oldZoom explicitly because we already updated _zoom,
          // but the focal-correction math needs the delta. We pass
          // it via focal.deltaScale.
          const ds = focal.deltaScale;
          if (ds && isFinite(ds)) {
            wrap.scrollLeft = (wrap.scrollLeft + focal.x) * ds - focal.x;
            wrap.scrollTop  = (wrap.scrollTop  + focal.y) * ds - focal.y;
          }
        }
      }
    };

    // Initial paint (in case localStorage had a non-default value)
    apply(saved);

    // Wheel listener on the wrap. preventDefault only when modifier
    // is held -- otherwise let the browser scroll natively.
    const wrap = document.querySelector(".canvas-flow-wrap");
    if (wrap && wrap.dataset._zoomWired !== "1") {
      wrap.dataset._zoomWired = "1";
      wrap.addEventListener("wheel", (e) => {
        if (!(e.ctrlKey || e.metaKey)) return;   // only modified scroll = zoom
        e.preventDefault();
        const oldZoom = this._zoom;
        const factor  = e.deltaY < 0 ? STEP : 1 / STEP;
        const newZoom = oldZoom * factor;
        // Compute focal point relative to the wrap's content origin
        const rect = wrap.getBoundingClientRect();
        const focal = {
          x: e.clientX - rect.left,
          y: e.clientY - rect.top,
          deltaScale: newZoom / oldZoom,
        };
        apply(newZoom, focal);
      }, { passive: false });
    }

    // Toolbar buttons
    const wireBtn = (id, fn) => {
      const el = document.getElementById(id);
      if (!el || el.dataset._zoomBtnWired === "1") return;
      el.dataset._zoomBtnWired = "1";
      el.addEventListener("click", fn);
    };
    wireBtn("canvas-zoom-in",    () => apply(this._zoom * STEP));
    wireBtn("canvas-zoom-out",   () => apply(this._zoom / STEP));
    wireBtn("canvas-zoom-reset", () => apply(1.0));

    // Keyboard shortcuts: Ctrl++ / Ctrl+- / Ctrl+0 (mirror Figma/IDE).
    // Only when editor view is visible -- otherwise we'd hijack the
    // user's browser-level zoom on the library page.
    document.addEventListener("keydown", (e) => {
      const editor = document.getElementById("scripts-editor-view");
      if (!editor || editor.style.display === "none") return;
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "=" || e.key === "+") {
        e.preventDefault();
        apply(this._zoom * STEP);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        apply(this._zoom / STEP);
      } else if (e.key === "0") {
        e.preventDefault();
        apply(1.0);
      }
    });
  },

  teardown() {
    if (this._arrowsRAF) cancelAnimationFrame(this._arrowsRAF);
    const vp = $("#var-picker");
    if (vp) vp.style.display = "none";
  },

  // ── Loaders (shared) ─────────────────────────────────────────


  // ════════════════════════════════════════════════════════════
  //   API
  // ════════════════════════════════════════════════════════════

  async loadCatalog() {
    try {
      const resp = await api("/api/actions/catalog");
      const items = Array.isArray(resp) ? resp : (resp.types || []);
      // Pull ad-class skip/only flags out of common_params and merge
      // them into every non-container action's params. Without this,
      // the UI never surfaces them and users can't toggle "click only
      // my own ads" / "skip target-domain ads" without editing the JSON.
      // Probability stays excluded because the inspector renders it in
      // its own Execution section -- merging it here would duplicate.
      const adFlagNames = new Set([
        "skip_on_my_domain",
        "skip_on_target",
        "only_on_target",
        "only_on_my_domain",
      ]);
      const adFlags = (resp.common_params || [])
        .filter(p => adFlagNames.has(p.name));
      this.commonParams = resp.common_params || [];

      items.forEach(c => {
        if (!c.category) c.category = "other";
        c.is_container = c.is_container ||
          ["if", "foreach_ad", "foreach", "loop"].includes(c.type);
        // Containers (if/foreach/loop) don't run per-ad logic, so the
        // ad-class flags are meaningless there. Everything else gets
        // them appended after its native params.
        if (!c.is_container && adFlags.length) {
          c.params = (c.params || []).concat(adFlags);
        }
      });
      this.catalog = items;
      this.renderPalette();
    } catch (e) {
      console.error("catalog load:", e);
      toast("Failed to load action catalog", true);
    }
  },

  async loadConditionKinds() {
    try {
      const resp = await api("/api/actions/condition-kinds");
      this.conditionKinds = resp.kinds || [];
    } catch (e) {
      this.conditionKinds = [{ kind: "always", label: "Always run" }];
    }
  },

  async loadLibrary() {
    try {
      const resp = await api("/api/scripts");
      this.scripts = resp.scripts || [];
      this.renderLibrary();
    } catch (e) {
      console.error("scripts load:", e);
      toast("Failed to load scripts", true);
    }
  },

  // ── View mode switching ──────────────────────────────────────


  // ════════════════════════════════════════════════════════════
  //   STATE / VIEW SWITCH
  // ════════════════════════════════════════════════════════════

  _showLibrary() {
    this.currentScript = null;
    this.selection = null;
    this.dirty = false;
    $("#scripts-library-view").style.display = "";
    $("#scripts-editor-view").style.display  = "none";
    this.loadLibrary();   // refresh list
  },

  async _showEditor(scriptId) {
    // Load full script (with flow) from server
    try {
      const resp = await api(`/api/scripts/${scriptId}`);
      const sc = resp.script;
      if (!sc) throw new Error("Script not found");
      this.currentScript = sc;
      this.flow = sc.flow || [];
      // Phase 4: reset undo/redo stacks per-script + check for an
      // autosaved draft. Push the just-loaded state as the baseline
      // entry so the very first user mutation has something to undo to.
      this.undoStack = [];
      this.redoStack = [];
      this._pushUndoBaseline = () => {
        // Defer until after editor inputs are populated.
        this.undoStack.push(this._snapshotState());
      };
      const draft = this._autosaveCheck(scriptId, sc.updated_at);
      if (draft && confirm(
            `An unsaved draft for "${sc.name}" was found from ` +
            `${new Date(draft.savedAt).toLocaleString()}.\n\nRestore it?`)) {
        this.flow = draft.flow || this.flow;
        // Name + description applied below in editor wiring; we
        // override there too:
        this._pendingDraftName = draft.name;
        this._pendingDraftDesc = draft.description;
        this.dirty = true;
      } else {
        this._autosaveClear(scriptId);
      }
      this.selection = null;
      this.dirty = false;
      $("#scripts-library-view").style.display = "none";
      $("#scripts-editor-view").style.display  = "";

      // Populate header fields
      $("#editor-name-input").value = sc.name || "";
      $("#editor-desc-input").value = sc.description || "";
      $("#editor-default-badge").style.display = sc.is_default ? "" : "none";
      $("#editor-default-btn").style.display   = sc.is_default ? "none" : "";
      $("#editor-delete-btn").style.display    = sc.is_default ? "none" : "";

      this.renderFlow();
    } catch (e) {
      toast(`Could not open script: ${e.message}`, true);
    }
  },

  // ═══════════════════════════════════════════════════════════════
  // LIBRARY VIEW
  // ═══════════════════════════════════════════════════════════════


  // ════════════════════════════════════════════════════════════
  //   LIBRARY (cards grid)
  // ════════════════════════════════════════════════════════════

  renderLibrary() {
    const grid = $("#scripts-library-grid");
    if (!this.scripts.length) {
      grid.innerHTML = `<div class="library-empty">
        No scripts yet. Click <strong>+ New script</strong>, or
        <strong>\u{1F4DA} Templates</strong> to start from a recipe.
      </div>`;
      return;
    }
    grid.innerHTML = this.scripts.map(s => this._renderLibraryCard(s)).join("");
    // Card click -> open editor; ⋯ menu click stops propagation and
    // opens the floating action menu instead. Event delegation keeps
    // the wiring O(1) regardless of card count.
    grid.querySelectorAll(".library-card").forEach(card => {
      card.addEventListener("click", (e) => {
        // Menu button bubble-stop is handled in _openCardMenu; here we
        // only need to skip card-open when the click started inside a
        // chip or pin element (defensive -- prevents pin-chip click
        // from accidentally opening the editor).
        if (e.target.closest(".library-card-menu-btn")) return;
        if (e.target.closest(".library-pin-chip")) return;
        const id = Number(card.dataset.scriptId);
        this._showEditor(id);
      });
    });
    // Wire ⋯ menu buttons: floating action menu with Edit / Run /
    // Apply / Pin / Duplicate / Delete.
    grid.querySelectorAll(".library-card-menu-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = Number(btn.dataset.cardMenu);
        this._openCardMenu(btn, id);
      });
    });
  },

  // ─── Phase 5: Validate-before-save ───────────────────────────

  /** Walk the flow, return list of {path, summary, message} for
   *  required-but-missing params. `path` is an encoded step path that
   *  matches what `data-path` attributes use, so the caller can mark
   *  the offending DOM card. Empty list = clean. */
  _validateFlow() {
    const errors = [];
    const walk = (steps, basePath, containerKey) => {
      (steps || []).forEach((s, i) => {
        const path = [...basePath, { key: containerKey, idx: i }];
        const meta = this.catalog.find(c => c.type === s.type);
        if (!meta) {
          errors.push({
            path,
            summary: `Step ${i + 1}`,
            message: `unknown type "${s.type}"`,
          });
          return;
        }
        for (const p of (meta.params || [])) {
          if (!p.required) continue;
          const v = s[p.name];
          const empty = v === undefined || v === null ||
            (typeof v === "string" && v.trim() === "") ||
            (Array.isArray(v) && v.length === 0);
          if (empty) {
            errors.push({
              path,
              summary: `${meta.label || s.type} (#${i + 1})`,
              message: `"${p.label || p.name}" is required`,
            });
          }
        }
        // Recurse into containers
        if (Array.isArray(s.steps))      walk(s.steps,      path, "steps");
        if (Array.isArray(s.then_steps)) walk(s.then_steps, path, "then_steps");
        if (Array.isArray(s.else_steps)) walk(s.else_steps, path, "else_steps");
      });
    };
    walk(this.flow, [], "root");
    return errors;
  },

  /** Add/remove `is-invalid` class + `data-error-msg` on flow-step
   *  DOM cards based on validation errors. Re-rendering the canvas
   *  later will reset this; we just need it to be visible until the
   *  user fixes the issue and tries Save again. */
  _renderValidationMarkers(errors) {
    const root = $("#canvas-flow");
    if (!root) return;
    root.querySelectorAll(".flow-step.is-invalid").forEach(el => {
      el.classList.remove("is-invalid");
      el.removeAttribute("data-error-msg");
    });
    for (const e of errors) {
      const sel = `.flow-step[data-path="${this._encodePath(e.path)}"]`;
      const el = root.querySelector(sel);
      if (el) {
        el.classList.add("is-invalid");
        // Aggregate multiple errors per step into one tooltip
        const cur = el.getAttribute("data-error-msg") || "";
        el.setAttribute("data-error-msg",
          cur ? cur + "; " + e.message : e.message);
      }
    }
  },

  _renderLibraryCard(s) {
    const updated = s.updated_at
      ? this._formatRelative(s.updated_at)
      : "\u2014";  // em-dash, escaped for ASCII-safety in this file
    const pCount = s.profile_count || 0;
    const pLabel = pCount === 1 ? "1 profile" : `${pCount} profiles`;
    const desc = s.description || "(no description)";

    // Run-status badge -- decoded from last_run_status (null=never run,
    // "ok"=last exit code 0, "fail"=last exit code != 0). Card sits idle
    // until the user opens a card menu so we don't compete with the
    // primary "Edit by clicking the card" affordance.
    let badge = "";
    if (s.last_run_status === "ok") {
      badge = `<span class="library-card-badge library-card-badge-ok"
                title="Last run succeeded${s.last_run_at ? " (" + s.last_run_at + ")" : ""}">\u25CF</span>`;
    } else if (s.last_run_status === "fail") {
      badge = `<span class="library-card-badge library-card-badge-fail"
                title="Last run failed${s.last_run_at ? " (" + s.last_run_at + ")" : ""}">\u25CF</span>`;
    } else {
      badge = `<span class="library-card-badge library-card-badge-idle"
                title="Never run">\u25CF</span>`;
    }

    // Pinned-profile chips: small, max ~4 visible, "+N more" overflow.
    // Click on a chip alone doesn't bubble to the card -- ⋯ menu is the
    // way to actually run/manage. Chips here are purely informative.
    const pinned = Array.isArray(s.pinned_profiles) ? s.pinned_profiles : [];
    let pinnedRow = "";
    if (pinned.length) {
      const visible = pinned.slice(0, 4);
      const overflow = pinned.length - visible.length;
      pinnedRow = `<div class="library-card-pinned">
        ${visible.map(n => `<span class="library-pin-chip" title="Pinned profile: ${escapeHtml(n)}">\u{1F4CC} ${escapeHtml(n)}</span>`).join("")}
        ${overflow > 0 ? `<span class="library-pin-chip library-pin-overflow">+${overflow}</span>` : ""}
      </div>`;
    }

    return `
      <div class="library-card" data-script-id="${s.id}">
        <div class="library-card-header">
          ${badge}
          <div class="library-card-name">${escapeHtml(s.name)}</div>
          ${s.is_default
            ? `<span class="library-card-default" title="Default script">DEFAULT</span>`
            : ""}
          <button class="library-card-menu-btn" data-card-menu="${s.id}"
                  title="More actions">\u22EF</button>
        </div>
        <div class="library-card-desc">${escapeHtml(desc)}</div>
        ${pinnedRow}
        ${(s.tags && s.tags.length) ? `<div class="library-card-tags">
          ${s.tags.map(t => `<span class="library-tag">${escapeHtml(t)}</span>`).join("")}
        </div>` : ""}
        <div class="library-card-stats">
          <span class="library-card-stat">
            <strong>${s.step_count || 0}</strong> steps
          </span>
          <span class="library-card-stat"
                title="Profiles using this script">
            <strong>${pCount}</strong> ${pLabel.replace(/^\d+ /, "")}
          </span>
          <span class="library-card-stat library-card-time">${updated}</span>
        </div>
      </div>`;
  },

  /** Phase 5: client-side library filter.
   *  Matches script.name + description + tags against the query
   *  case-insensitively. Display the count next to the search box. */
  _filterLibrary(query) {
    const q = (query || "").trim().toLowerCase();
    const grid = $("#scripts-library-grid");
    const hint = $("#library-search-hint");
    if (!grid) return;
    const cards = grid.querySelectorAll(".library-card");
    let shown = 0;
    cards.forEach(card => {
      const id = Number(card.dataset.scriptId);
      const sc = this.scripts.find(s => s.id === id);
      if (!sc) { card.style.display = "none"; return; }
      let hay = (sc.name + " " + (sc.description || "") + " " +
                 (sc.tags || []).join(" ")).toLowerCase();
      const match = !q || hay.includes(q);
      card.style.display = match ? "" : "none";
      if (match) shown++;
    });
    if (hint) {
      hint.textContent = q
        ? `${shown} of ${cards.length} scripts`
        : "";
    }
  },

  /** Turn an ISO timestamp into "3h ago" / "Apr 24" etc. Quick-n-dirty. */
  _formatRelative(ts) {
    try {
      const dt = new Date(ts.replace(" ", "T") + "Z");
      const diff = (Date.now() - dt.getTime()) / 1000;
      if (diff < 60) return "just now";
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d ago`;
      return dt.toLocaleDateString(undefined,
        { month: "short", day: "numeric" });
    } catch { return ts; }
  },


  // ════════════════════════════════════════════════════════════
  //   LIBRARY HEADER (new + import + templates)
  // ════════════════════════════════════════════════════════════

  wireLibraryHeader() {
    $("#library-new-btn").addEventListener("click", () => {
      this._openNewScriptModal();
    });
    $("#library-import-btn").addEventListener("click", () => {
      $("#library-import-file").click();
    });
    const tplBtn = $("#library-templates-btn");
    if (tplBtn) {
      tplBtn.addEventListener("click", () => this._openTemplatesModal());
    }
    // Phase 5: live library search -- filters cards by name/desc/tag
    // case-insensitively. Empty query restores full list. Hint shows
    // "N of M" while filtering.
    const searchEl = $("#library-search");
    if (searchEl) {
      searchEl.addEventListener("input", () => this._filterLibrary(searchEl.value));
    }
    $("#library-import-file").addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      e.target.value = "";
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const parsed = JSON.parse(reader.result);
          const flow = Array.isArray(parsed) ? parsed
                     : Array.isArray(parsed.flow) ? parsed.flow
                     : null;
          if (!flow) throw new Error("No flow array in file");
          // Validate
          const check = (steps, path = "") => {
            for (let i = 0; i < steps.length; i++) {
              const s = steps[i];
              if (!s || typeof s !== "object" || !s.type) {
                throw new Error(`Invalid step at ${path}[${i}]`);
              }
              if (Array.isArray(s.steps))      check(s.steps, `${path}[${i}].steps`);
              if (Array.isArray(s.then_steps)) check(s.then_steps, `${path}[${i}].then_steps`);
              if (Array.isArray(s.else_steps)) check(s.else_steps, `${path}[${i}].else_steps`);
            }
          };
          check(flow, "flow");

          // Auto-name from file metadata or filename
          const suggestedName = parsed._meta?.name
            || file.name.replace(/\.json$/i, "");
          this._openNewScriptModal({
            name: suggestedName,
            description: parsed._meta?.description || "",
            flow,
          });
        } catch (err) {
          toast(`Import failed: ${err.message}`, true);
        }
      };
      reader.readAsText(file);
    });
  },


  // ════════════════════════════════════════════════════════════
  //   NEW-SCRIPT MODAL
  // ════════════════════════════════════════════════════════════

  wireNewScriptModal() {
    document.querySelectorAll('[data-close="new-script-modal"]').forEach(el => {
      el.addEventListener("click", () => this._closeNewScriptModal());
    });
    $("#new-script-create-btn").addEventListener("click",
      () => this._confirmNewScript());
  },

  _openNewScriptModal(prefill = {}) {
    const modal = $("#new-script-modal");
    $("#new-script-name").value = prefill.name || "";
    $("#new-script-desc").value = prefill.description || "";
    modal._pendingFlow = prefill.flow || [];
    modal.style.display = "";
    setTimeout(() => $("#new-script-name").focus(), 30);
  },

  _closeNewScriptModal() {
    const modal = $("#new-script-modal");
    modal.style.display = "none";
    modal._pendingFlow = null;
  },

  async _confirmNewScript() {
    const modal = $("#new-script-modal");
    const name = $("#new-script-name").value.trim();
    const desc = $("#new-script-desc").value.trim();
    if (!name) {
      toast("Name is required", true);
      return;
    }
    const btn = $("#new-script-create-btn");
    btn.disabled = true;
    try {
      const resp = await api("/api/scripts", {
        method: "POST",
        body: JSON.stringify({
          name, description: desc,
          flow: modal._pendingFlow || [],
        }),
      });
      this._closeNewScriptModal();
      toast(`✓ Created "${name}"`);
      // Jump straight into editor for the new script
      await this._showEditor(resp.id);
    } catch (e) {
      toast(`Create failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
    }
  },

  // ═══════════════════════════════════════════════════════════════
  // EDITOR VIEW (unified flow builder)
  // ═══════════════════════════════════════════════════════════════


  // ════════════════════════════════════════════════════════════
  //   EDITOR HEADER (save / delete / export)
  // ════════════════════════════════════════════════════════════

  wireEditorHeader() {
    $("#editor-back-btn").addEventListener("click", (e) => {
      e.preventDefault();
      if (this.dirty && !confirm("Discard unsaved changes?")) return;
      this._showLibrary();
    });
    $("#editor-save-btn").addEventListener("click", () => this.save());
    $("#editor-reload-btn").addEventListener("click", () => {
      if (this.dirty && !confirm("Discard unsaved changes?")) return;
      this._showEditor(this.currentScript.id);
    });
    $("#editor-delete-btn").addEventListener("click", () => this._deleteScript());
    $("#editor-default-btn").addEventListener("click", () => this._makeDefault());
    $("#editor-export-btn").addEventListener("click", () => this._exportScript());
    $("#inspector-close-btn").addEventListener("click", () => {
      this.selection = null;
      this.renderInspector();
      this.highlightSelection();
    });

    // Name + description — live save-on-blur (no auto-save, just mark dirty)
    $("#editor-name-input").addEventListener("input", () => this._markDirty());
    $("#editor-desc-input").addEventListener("input", () => this._markDirty());
    // Phase 4: apply restored draft override (set in _showEditor before
    // inputs are populated by the surrounding code).
    if (this._pendingDraftName !== undefined) {
      $("#editor-name-input").value = this._pendingDraftName;
      this._pendingDraftName = undefined;
    }
    if (this._pendingDraftDesc !== undefined) {
      $("#editor-desc-input").value = this._pendingDraftDesc;
      this._pendingDraftDesc = undefined;
    }
    // Push the (possibly draft-overridden) initial state as the undo
    // baseline so Ctrl+Z brings the editor back to "as it loaded".
    if (this._pushUndoBaseline) {
      this._pushUndoBaseline();
      this._pushUndoBaseline = null;
    }
  },


  // ════════════════════════════════════════════════════════════
  //   EDITOR SAVE / VALIDATE / DRAFT
  // ════════════════════════════════════════════════════════════

  async save() {
    if (!this.currentScript) return;
    const btn = $("#editor-save-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Saving…";
    try {
      const name = $("#editor-name-input").value.trim();
      const desc = $("#editor-desc-input").value.trim();
      if (!name) {
        toast("Name is required", true);
        return;
      }
      // Phase 5: validate-before-save. Walks flow, checks required
      // params per step against catalog schema. On failure, mark all
      // bad-step cards red + show the first 3 errors in a toast.
      const errors = this._validateFlow();
      this._renderValidationMarkers(errors);
      if (errors.length) {
        const sample = errors.slice(0, 3)
          .map(e => `${e.summary}: ${e.message}`).join("; ");
        toast(`${errors.length} error${errors.length > 1 ? "s" : ""} -- ${sample}${errors.length > 3 ? "; ..." : ""}`, true);
        return;
      }
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name, description: desc, flow: this._cleanFlowForSave(this.flow),
        }),
      });
      this.currentScript.name = name;
      this.currentScript.description = desc;
      this.dirty = false;
      // Phase 4: drop the draft now that the server has persisted.
      if (this.currentScript) this._autosaveClear(this.currentScript.id);
      toast("✓ Saved");
    } catch (e) {
      toast(`Save failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "💾 Save";
    }
  },

  async _deleteScript() {
    if (!this.currentScript) return;
    if (!confirm(
      `Delete "${this.currentScript.name}"? Profiles using this ` +
      `script will fall back to the default.`
    )) return;
    try {
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "DELETE",
      });
      toast("✓ Deleted");
      this._showLibrary();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, true);
    }
  },

  async _makeDefault() {
    if (!this.currentScript) return;
    try {
      await api(`/api/scripts/${this.currentScript.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_default: true }),
      });
      this.currentScript.is_default = 1;
      $("#editor-default-badge").style.display = "";
      $("#editor-default-btn").style.display = "none";
      $("#editor-delete-btn").style.display   = "none";
      toast(`✓ "${this.currentScript.name}" is now the default`);
    } catch (e) {
      toast(`Could not set default: ${e.message}`, true);
    }
  },

  _exportScript() {
    if (!this.currentScript) return;
    const blob = new Blob(
      [JSON.stringify({
        _meta: {
          format:      "ghost-shell-flow",
          version:     1,
          name:        this.currentScript.name,
          description: this.currentScript.description,
          exported_at: new Date().toISOString(),
        },
        flow: this.flow,
      }, null, 2)],
      { type: "application/json" }
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeName = (this.currentScript.name || "script")
      .replace(/[^a-z0-9_-]+/gi, "_").toLowerCase();
    a.download = `${safeName}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  },

  // ── Keyboard ─────────────────────────────────────────────────


  // ════════════════════════════════════════════════════════════
  //   KEYBOARD (Ctrl+S/Z/Y/K/1..9)
  // ════════════════════════════════════════════════════════════

  wireKeyboard() {
    document.addEventListener("keydown", (e) => {
      // Phase 4: editor-wide shortcuts (work even without selection).
      // Ctrl+S, Ctrl+Z, Ctrl+Y, Ctrl+Shift+Z (redo alt). Avoid stealing
      // shortcuts when the user is mid-typing in an input.
      if (!this.currentScript) return;
      const inField = e.target && ["INPUT", "TEXTAREA", "SELECT"]
        .includes(e.target.tagName);
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "s" && !inField) {
        e.preventDefault();
        this.save();
        return;
      }
      // Phase 5: Ctrl+K opens the global command palette. Works even
      // when not in the editor -- useful for "I'm on Profiles page,
      // want to run script X on profile Y, GO".
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        this._openCommandPalette();
        return;
      }
      // Phase 5: Ctrl+1..9 trigger user-defined hotkeys. Each digit
      // maps to a script id stored in localStorage (set via the card
      // ⋯ menu). Pressing the hotkey runs that script on the user's
      // default-profile pick (last-used or first-pinned). No-op if
      // unassigned.
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && /^[1-9]$/.test(e.key) && !inField) {
        e.preventDefault();
        this._triggerHotkey(parseInt(e.key, 10));
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        this._undo();
        return;
      }
      if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") ||
          ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "z")) {
        e.preventDefault();
        this._redo();
        return;
      }

      // Steps-only shortcuts
      if (!this.selection) return;
      if (inField) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        this._removeAt(this.selection.path);
        this.selection = null;
        this._markDirty();
        this.renderFlow();
      } else if (e.key === "Escape") {
        this.selection = null;
        this.renderInspector();
        this.highlightSelection();
      }
    });
  },

  // ── PALETTE ───────────────────────────────────────────────────


  // ════════════════════════════════════════════════════════════
  //   PALETTE (catalog rendering, drag source)
  // ════════════════════════════════════════════════════════════

  renderPalette() {
    const body = $("#palette-body");
    if (!this.catalog.length) {
      body.innerHTML = `<div class="palette-empty">No actions available</div>`;
      return;
    }
    const order = ["flow", "ads", "navigation", "interaction", "timing",
                   "data", "external", "extensions", "input", "power", "other"];
    const grouped = {};
    order.forEach(c => grouped[c] = []);
    this.catalog.forEach(a => {
      const cat = order.includes(a.category) ? a.category : "other";
      grouped[cat].push(a);
    });
    const groupLabels = {
      flow:        ["#22d3ee", "Flow control"],
      ads:         ["#f97316", "Ads"],
      navigation:  ["#60a5fa", "Navigation"],
      interaction: ["#a78bfa", "Interaction"],
      timing:      ["#f59e0b", "Timing"],
      data:        ["#34d399", "Data"],
      external:    ["#fb7185", "External"],
      extensions:  ["#a855f7", "Extensions"],
      input:       ["#94a3b8", "Input"],
      power:       ["#94a3b8", "Power"],
      other:       ["#94a3b8", "Other"],
    };
    const renderItem = (a) => `
      <div class="palette-item" draggable="true"
           data-type="${escapeHtml(a.type)}"
           data-category="${escapeHtml(a.category)}"
           data-search="${escapeHtml(((a.label || "") + " " + a.type).toLowerCase())}">
        <div class="palette-item-icon">${this._iconFor(a)}</div>
        <div class="palette-item-body">
          <div class="palette-item-label">${escapeHtml(a.label || a.type)}</div>
          <div class="palette-item-desc">${escapeHtml(a.description || "")}</div>
        </div>
      </div>`;
    const html = order
      .filter(cat => grouped[cat].length)
      .map(cat => {
        const [color, label] = groupLabels[cat];
        return `
          <div class="palette-group">
            <div class="palette-group-label">
              <span class="palette-group-dot" style="background:${color}"></span>
              ${label}
            </div>
            ${grouped[cat].map(renderItem).join("")}
          </div>`;
      }).join("");
    body.innerHTML = html;
  },


  // ════════════════════════════════════════════════════════════
  //   PALETTE WIRING
  // ════════════════════════════════════════════════════════════

  wirePalette() {
    $("#palette-search").addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $("#palette-body").querySelectorAll(".palette-item").forEach(it => {
        const hay = it.dataset.search || "";
        it.style.display = (!q || hay.includes(q)) ? "" : "none";
      });
    });
    $("#palette-body").addEventListener("click", (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      this._addStep({ containerPath: [] }, item.dataset.type);
    });
    $("#palette-body").addEventListener("dragstart", (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      item.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("application/x-gs-palette",
        JSON.stringify({ type: item.dataset.type }));
    });
    $("#palette-body").addEventListener("dragend", (e) => {
      e.target.closest(".palette-item")?.classList.remove("is-dragging");
    });
  },

  // ── CANVAS (flow editor) ─────────────────────────────────────


  // ════════════════════════════════════════════════════════════
  //   CANVAS (flow rendering)
  // ════════════════════════════════════════════════════════════

  renderFlow() {
    $("#stat-total-count").textContent = this._countSteps(this.flow);
    const root = $("#canvas-flow");
    root.innerHTML = this._renderStepList(this.flow, [], "root")
                   + this._renderAddButton([]);
    this.wireCanvasInteractions();
    this._wireInlineParams();
    this.renderInspector();
    this.highlightSelection();
    this._scheduleArrowsRedraw();
  },

  _countSteps(steps) {
    let n = 0;
    for (const s of (steps || [])) {
      n++;
      n += this._countSteps(s.steps);
      n += this._countSteps(s.then_steps);
      n += this._countSteps(s.else_steps);
    }
    return n;
  },

  _renderStepList(steps, basePath, containerKey) {
    if (!steps || !steps.length) return "";
    return steps.map((s, i) => {
      const path = [...basePath, { key: containerKey, idx: i }];
      return this._renderStep(s, path);
    }).join("");
  },


  // ════════════════════════════════════════════════════════════
  //   CANVAS STEP RENDERING (simple + container)
  // ════════════════════════════════════════════════════════════

  _renderStep(step, path) {
    const meta = this.catalog.find(c => c.type === step.type);
    const isContainer = meta?.is_container ||
      ["if", "foreach_ad", "foreach", "loop"].includes(step.type);
    if (isContainer) return this._renderContainer(step, path, meta);
    return this._renderSimpleStep(step, path, meta);
  },

  _renderSimpleStep(step, path, meta) {
    const label    = meta ? (meta.label || step.type) : step.type;
    const category = meta?.category || "other";
    const enabled  = step.enabled !== false;
    const idx      = path[path.length - 1].idx;
    // Phase 5: inline param editing -- per-step accordion. The expand
    // state lives on a transient flag (step._inlineOpen) that is NOT
    // serialized to the server (filtered out in save()) and NOT
    // visible in the JSON export. Default is collapsed.
    const inlineOpen = !!step._inlineOpen;
    const editableParams = (meta?.params || [])
      .filter(p => !["steps", "then_steps", "else_steps", "condition"]
                       .includes(p.name));
    const hasParams = editableParams.length > 0;
    return `
      <div class="flow-step ${enabled ? '' : 'is-disabled'} ${inlineOpen ? 'is-inline-open' : ''}"
           data-path="${this._encodePath(path)}"
           data-category="${category}"
           data-type="${escapeHtml(step.type)}"
           draggable="true">
        <div class="flow-step-head">
          <div class="flow-step-num">${idx + 1}</div>
          <div class="flow-step-icon">${this._iconFor(meta || {type: step.type})}</div>
          <div class="flow-step-label">${escapeHtml(label)}</div>
          <div class="flow-step-actions">
            ${hasParams ? `<button class="btn-icon" data-action="inline-edit"
                    title="${inlineOpen ? 'Hide params' : 'Edit params inline'}">${inlineOpen ? '\u25BC' : '\u270E'}</button>` : ""}
            <button class="btn-icon" data-action="toggle"
                    title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '\u23F8' : '\u25B6'}</button>
            <button class="btn-icon" data-action="duplicate" title="Duplicate">\u2398</button>
            <button class="btn-icon" data-action="remove" title="Remove">\u2715</button>
          </div>
        </div>
        <div class="flow-step-body">${this._buildChips(step, meta)}</div>
        ${inlineOpen && hasParams ? `<div class="flow-step-inline-params"
             data-inline-host="${this._encodePath(path)}">
          ${editableParams.map(p => this._renderInlineParam(p, step, path)).join("")}
        </div>` : ""}
      </div>`;
  },

  _renderContainer(step, path, meta) {
    const label = meta ? (meta.label || step.type) : step.type;
    const idx   = path[path.length - 1].idx;
    const summary = this._buildContainerSummary(step, meta);
    const enabled = step.enabled !== false;

    let bodyHtml;
    if (step.type === "if") {
      bodyHtml = `
        <div class="flow-container-body ${(step.then_steps || []).length === 0 ? 'is-empty' : ''}"
             data-container-path="${this._encodePath(path)}:then_steps">
          <div class="container-subregion-label then-label">Then</div>
          ${this._renderStepList(step.then_steps || [], path, "then_steps")
            || this._renderEmptyBodyMarker("then")}
          ${this._renderAddButton(path, "then_steps")}

          ${(step.else_steps && step.else_steps.length) || this._elseOpen(path)
            ? `<div class="container-subregion-label else-label">Else</div>
               ${this._renderStepList(step.else_steps || [], path, "else_steps")
                 || this._renderEmptyBodyMarker("else")}
               ${this._renderAddButton(path, "else_steps")}`
            : `<button class="flow-add-btn"
                       data-action="add-else"
                       data-path="${this._encodePath(path)}">+ Add else branch</button>`}
        </div>`;
    } else {
      const nested = step.steps || [];
      bodyHtml = `
        <div class="flow-container-body ${nested.length === 0 ? 'is-empty' : ''}"
             data-container-path="${this._encodePath(path)}:steps">
          ${this._renderStepList(nested, path, "steps")
            || this._renderEmptyBodyMarker()}
          ${this._renderAddButton(path, "steps")}
        </div>`;
    }
    return `
      <div class="flow-container ${enabled ? '' : 'is-disabled'}"
           data-path="${this._encodePath(path)}"
           data-ctype="${escapeHtml(step.type)}"
           draggable="true">
        <div class="flow-container-head">
          <div class="flow-step-num">${idx + 1}</div>
          <div class="flow-step-icon">${this._iconFor(meta || {type: step.type})}</div>
          <div class="flow-container-title">
            <span class="flow-container-title-label">${escapeHtml(label)}</span>
            ${summary ? `<span class="flow-container-summary">${summary}</span>` : ''}
          </div>
          <div class="flow-step-actions">
            <button class="btn-icon" data-action="toggle"
                    title="${enabled ? 'Disable' : 'Enable'}">${enabled ? '⏸' : '▶'}</button>
            <button class="btn-icon" data-action="duplicate" title="Duplicate">⎘</button>
            <button class="btn-icon" data-action="remove" title="Remove">✕</button>
          </div>
        </div>
        ${bodyHtml}
      </div>`;
  },

  _renderEmptyBodyMarker(kind = "") {
    return `<div class="container-body-empty">
      Empty — drag actions here${kind ? ` to run when <strong>${kind}</strong>` : ""}.
    </div>`;
  },

  _elseOpen(path) {
    const step = this._getAt(path);
    return step && Array.isArray(step.else_steps) &&
           (step.else_steps.length > 0 || step._else_expanded);
  },

  _renderAddButton(basePath, subKey = "steps") {
    return `
      <button class="flow-add-btn"
              data-action="add-step"
              data-path="${this._encodePath(basePath)}"
              data-subkey="${subKey}">+ Add step</button>`;
  },

  _buildChips(step, meta) {
    if (!meta) {
      return `<span class="flow-step-chip chip-prob">UNKNOWN TYPE</span>`;
    }
    const chips = [];
    const params = (meta.params || [])
      .filter(p => !["steps", "then_steps", "else_steps", "condition"].includes(p.name));
    let shown = 0;
    for (const p of params) {
      if (shown >= 3) break;
      const v = step[p.name];
      if (v === undefined || v === null || v === "" || v === p.default) continue;
      const display = this._paramDisplay(p, v);
      if (!display) continue;
      chips.push(
        `<span class="flow-step-chip">
           <span class="flow-step-chip-label">${escapeHtml(p.label || p.name)}:</span>
           <code>${escapeHtml(display)}</code>
         </span>`
      );
      shown++;
    }
    const prob = step.probability !== undefined ? Number(step.probability) : 1.0;
    if (prob < 1.0) {
      chips.push(`<span class="flow-step-chip chip-prob">p = ${prob.toFixed(2)}</span>`);
    }
    if (!chips.length) {
      chips.push(`<span class="flow-step-chip"><code>${escapeHtml(step.type)}</code></span>`);
    }
    return chips.join("");
  },

  _buildContainerSummary(step, meta) {
    if (step.type === "if") {
      const c = step.condition || {};
      const kind = c.kind || "always";
      const kindMeta = this.conditionKinds.find(k => k.kind === kind);
      const label = kindMeta?.label || kind;
      return `<code>${c.negate ? "NOT " : ""}${escapeHtml(label)}</code>`;
    }
    if (step.type === "foreach_ad") {
      const n = (step.limit ? `first ${step.limit}` : "all");
      return `<code>foreach ${n} ad(s)</code>`;
    }
    if (step.type === "foreach" || step.type === "loop") {
      const items = step.items;
      const itemVar = step.item_var || "item";
      if (typeof items === "string") {
        const lines = items.split("\n").filter(l => l.trim()).length;
        return `<code>{${escapeHtml(itemVar)}} in ${lines} item(s)</code>`;
      }
      if (Array.isArray(items)) {
        return `<code>{${escapeHtml(itemVar)}} in ${items.length} item(s)</code>`;
      }
    }
    return "";
  },

  _paramDisplay(p, v) {
    if (p.type === "bool") return v ? "✓" : "✗";
    if (Array.isArray(v))  return `${v.length} item${v.length === 1 ? "" : "s"}`;
    if (typeof v === "string") {
      if (p.type === "textlist") {
        const lines = v.split("\n").filter(l => l.trim()).length;
        return `${lines} line${lines === 1 ? "" : "s"}`;
      }
      return v.length > 24 ? v.slice(0, 22) + "…" : v;
    }
    return String(v);
  },


  // ════════════════════════════════════════════════════════════
  //   CANVAS DRAG-DROP / CLICK / DELETE
  // ════════════════════════════════════════════════════════════

  wireCanvasInteractions() {
    const canvas = $("#canvas-flow");
    canvas.onclick = (e) => {
      const actionBtn = e.target.closest(".flow-step-actions button");
      if (actionBtn) {
        const card = actionBtn.closest("[data-path]");
        const path = this._decodePath(card.dataset.path);
        this._handleAction(path, actionBtn.dataset.action);
        e.stopPropagation();
        return;
      }
      const addBtn = e.target.closest('.flow-add-btn[data-action="add-step"]');
      if (addBtn) {
        const basePath = this._decodePath(addBtn.dataset.path);
        const subKey = addBtn.dataset.subkey || "steps";
        this._openTypePicker({ basePath, subKey });
        e.stopPropagation();
        return;
      }
      const elseBtn = e.target.closest('.flow-add-btn[data-action="add-else"]');
      if (elseBtn) {
        const path = this._decodePath(elseBtn.dataset.path);
        const step = this._getAt(path);
        if (step && !step.else_steps) step.else_steps = [];
        step._else_expanded = true;
        this._markDirty();
        this.renderFlow();
        return;
      }
      const card = e.target.closest("[data-path]");
      if (card && canvas.contains(card)) {
        const path = this._decodePath(card.dataset.path);
        this.selection = { path };
        this.renderInspector();
        this.highlightSelection();
      }
    };
    canvas.querySelectorAll("[data-path][draggable]").forEach(card => {
      card.addEventListener("dragstart", (e) => {
        if (e.target.closest(".flow-step-actions")) { e.preventDefault(); return; }
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("application/x-gs-move",
          JSON.stringify({ path: this._decodePath(card.dataset.path) }));
        card.classList.add("is-dragging");
        e.stopPropagation();
      });
      card.addEventListener("dragend", () => card.classList.remove("is-dragging"));
    });
    const zones = [canvas, ...canvas.querySelectorAll(".flow-container-body")];
    zones.forEach(zone => {
      zone.addEventListener("dragover", (e) => {
        if (!e.dataTransfer.types.includes("application/x-gs-palette") &&
            !e.dataTransfer.types.includes("application/x-gs-move")) return;
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("drop-active");
      });
      zone.addEventListener("dragleave", (e) => {
        if (e.currentTarget.contains(e.relatedTarget)) return;
        zone.classList.remove("drop-active");
      });
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove("drop-active");
        this._handleDrop(zone, e);
      });
    });
  },

  _handleDrop(zone, e) {
    const dt = e.dataTransfer;
    let target;
    if (zone.id === "canvas-flow") {
      target = { basePath: [], subKey: "root" };
    } else {
      const containerCard = zone.closest(".flow-container");
      if (!containerCard) return;
      const cPath = this._decodePath(containerCard.dataset.path);
      const cStep = this._getAt(cPath);
      let subKey = "steps";
      if (cStep?.type === "if") {
        const elseLabelEl = [...zone.querySelectorAll(".container-subregion-label.else-label")][0];
        if (elseLabelEl && e.clientY > elseLabelEl.getBoundingClientRect().top) {
          subKey = "else_steps";
        } else {
          subKey = "then_steps";
        }
      }
      target = { basePath: cPath, subKey };
    }
    const palRaw = dt.getData("application/x-gs-palette");
    if (palRaw) {
      try {
        const { type } = JSON.parse(palRaw);
        this._addStep({ containerPath: target.basePath,
                        subKey: target.subKey }, type);
      } catch {}
      return;
    }
    const moveRaw = dt.getData("application/x-gs-move");
    if (moveRaw) {
      try {
        const { path: srcPath } = JSON.parse(moveRaw);
        this._moveStep(srcPath, target);
      } catch {}
    }
  },

  _handleAction(path, action) {
    // inline-edit toggles the per-step accordion. NO _markDirty / no
    // renderFlow at the end because _toggleInlineEdit already does
    // its own re-render -- and inline-open is transient UI state, not
    // script content.
    if (action === "inline-edit") {
      this._toggleInlineEdit(path);
      return;
    }
    if (action === "remove") {
      this._removeAt(path);
      this.selection = null;
    } else if (action === "toggle") {
      const step = this._getAt(path);
      if (step) step.enabled = step.enabled === false;
    } else if (action === "duplicate") {
      const step = this._getAt(path);
      if (!step) return;
      const copy = JSON.parse(JSON.stringify(step));
      delete copy._else_expanded;
      this._insertAfter(path, copy);
    }
    this._markDirty();
    this.renderFlow();
  },

  // Path utilities
  _encodePath(path) {
    return path.map(s => `${s.key}:${s.idx}`).join("/");
  },
  _decodePath(enc) {
    if (!enc) return [];
    return enc.split("/").map(seg => {
      const [key, idxStr] = seg.split(":");
      return { key, idx: Number(idxStr) };
    });
  },
  _getContainerArray(path) {
    if (path.length === 0) return this.flow;
    let arr = this.flow;
    for (let i = 0; i < path.length - 1; i++) {
      const step = arr[path[i].idx];
      const childKey = path[i + 1].key;
      arr = step[childKey] || [];
    }
    return arr;
  },
  _getAt(path) {
    if (path.length === 0) return null;
    const arr = this._getContainerArray(path);
    return arr[path[path.length - 1].idx];
  },
  _removeAt(path) {
    const arr = this._getContainerArray(path);
    arr.splice(path[path.length - 1].idx, 1);
  },
  _insertAfter(path, step) {
    const arr = this._getContainerArray(path);
    arr.splice(path[path.length - 1].idx + 1, 0, step);
  },

  _addStep({ containerPath, subKey = "root" }, type) {
    const meta = this.catalog.find(c => c.type === type);
    if (!meta) return;
    const step = this._defaultStep(meta);
    let arr;
    if (subKey === "root" || containerPath.length === 0) {
      arr = this.flow;
    } else {
      const container = this._getAt(containerPath);
      if (!container) return;
      container[subKey] = container[subKey] || [];
      arr = container[subKey];
    }
    arr.push(step);
    const newIdx = arr.length - 1;
    if (subKey === "root" || containerPath.length === 0) {
      this.selection = { path: [{ key: "root", idx: newIdx }] };
    } else {
      this.selection = { path: [...containerPath,
                                { key: subKey, idx: newIdx }] };
    }
    this._markDirty();
    this.renderFlow();
  },

  _moveStep(srcPath, target) {
    const srcArr = this._getContainerArray(srcPath);
    const [step] = srcArr.splice(srcPath[srcPath.length - 1].idx, 1);
    if (!step) return;
    let dstArr;
    if (target.subKey === "root" || target.basePath.length === 0) {
      dstArr = this.flow;
    } else {
      const c = this._getAt(target.basePath);
      if (!c) { srcArr.push(step); return; }
      c[target.subKey] = c[target.subKey] || [];
      dstArr = c[target.subKey];
    }
    dstArr.push(step);
    this.selection = null;
    this._markDirty();
    this.renderFlow();
  },

  _defaultStep(meta) {
    const step = { type: meta.type, enabled: true };
    (meta.params || []).forEach(p => {
      if (p.default !== undefined &&
          !["steps", "then_steps", "else_steps"].includes(p.name)) {
        step[p.name] = p.default;
      }
    });
    if (meta.is_container) {
      if (meta.type === "if") {
        step.condition = { kind: "always" };
        step.then_steps = [];
      } else {
        step.steps = [];
      }
    }
    return step;
  },

  _markDirty() {
    this.dirty = true;
    // Phase 4: push current state onto undo stack + schedule a debounced
    // localStorage autosave. Both kept inside _markDirty so EVERY existing
    // call site (and any future ones) participates without per-site
    // surgery. snapshotState() takes a deep clone so later mutations
    // don't modify the saved snapshot.
    this._pushUndoSnapshot();
    this._scheduleAutosave();
  },

  // -----  Phase 4: Undo / Redo stack  -----------------------------
  // Strategy: undoStack always holds the CURRENT state on top. Each
  // mutation pushes the NEW state. Undo pops top, restores the next-
  // top, and pushes the popped one onto redoStack.

  _snapshotState() {
    return {
      flow:        JSON.parse(JSON.stringify(this.flow || [])),
      name:        $("#editor-name-input")?.value || "",
      description: $("#editor-desc-input")?.value || "",
    };
  },

  _pushUndoSnapshot() {
    if (!this.undoStack) this.undoStack = [];
    if (!this.redoStack) this.redoStack = [];
    this.undoStack.push(this._snapshotState());
    // Cap depth so a long editing session does not blow the heap. 80
    // entries covers easily a full afternoon of work; older entries
    // get trimmed silently.
    if (this.undoStack.length > 80) this.undoStack.shift();
    // Any new mutation invalidates the redo branch -- standard editor
    // semantics. Without this, redo could resurrect a state from a
    // different timeline.
    this.redoStack.length = 0;
  },

  _applyState(s) {
    this.flow = JSON.parse(JSON.stringify(s.flow || []));
    const nameEl = $("#editor-name-input");
    const descEl = $("#editor-desc-input");
    if (nameEl) nameEl.value = s.name || "";
    if (descEl) descEl.value = s.description || "";
    this.selection = null;
    this.renderFlow();
  },

  _undo() {
    if (!this.undoStack || this.undoStack.length < 2) {
      toast("Nothing to undo");
      return;
    }
    const cur = this.undoStack.pop();
    this.redoStack.push(cur);
    const prev = this.undoStack[this.undoStack.length - 1];
    this._applyState(prev);
    this.dirty = true;
  },

  _redo() {
    if (!this.redoStack || !this.redoStack.length) {
      toast("Nothing to redo");
      return;
    }
    const next = this.redoStack.pop();
    this.undoStack.push(next);
    this._applyState(next);
    this.dirty = true;
  },

  // -----  Phase 4: localStorage autosave  -------------------------
  // Debounced save -- writes 800ms after the last mutation so a burst
  // of typing does not hammer storage. On editor open we check for a
  // draft newer than the server's `updated_at` and offer to restore.

  _scheduleAutosave() {
    if (!this.currentScript) return;
    if (this._autosaveTimer) clearTimeout(this._autosaveTimer);
    this._autosaveTimer = setTimeout(() => this._autosaveFlush(), 800);
  },

  _autosaveKey(scriptId) { return `gs_script_draft_${scriptId}`; },

  _autosaveFlush() {
    if (!this.currentScript) return;
    try {
      const draft = {
        ...this._snapshotState(),
        savedAt: Date.now(),
      };
      localStorage.setItem(this._autosaveKey(this.currentScript.id),
                           JSON.stringify(draft));
    } catch (e) {
      // Storage quota / private mode -- silently degrade. Autosave
      // is a safety net, not a feature the user paid for.
    }
  },

  _autosaveClear(scriptId) {
    try { localStorage.removeItem(this._autosaveKey(scriptId)); } catch {}
  },

  /** Returns a draft payload if a newer-than-server one is found.
   *  Caller (in _showEditor) decides whether to prompt the user. */
  _autosaveCheck(scriptId, serverUpdatedAt) {
    try {
      const raw = localStorage.getItem(this._autosaveKey(scriptId));
      if (!raw) return null;
      const draft = JSON.parse(raw);
      // Compare against server's updated_at -- if server has changed
      // since the draft (rare: another tab or a teammate saved), bias
      // toward the server: stale drafts get auto-cleared, no prompt.
      if (serverUpdatedAt) {
        const serverMs = new Date(serverUpdatedAt.replace(" ", "T") + "Z").getTime();
        if (!Number.isNaN(serverMs) && draft.savedAt < serverMs) {
          this._autosaveClear(scriptId);
          return null;
        }
      }
      return draft;
    } catch {
      return null;
    }
  },

  // Selection
  highlightSelection() {
    $("#canvas-flow").querySelectorAll(".is-selected")
      .forEach(el => el.classList.remove("is-selected"));
    const panes = document.querySelector(".scripts-panes");
    const inspector = $("#scripts-inspector");
    if (!this.selection) {
      $("#inspector-close-btn").style.display = "none";
      if (inspector) inspector.style.display = "none";
      panes?.classList.add("inspector-hidden");
      return;
    }
    if (inspector) inspector.style.display = "";
    panes?.classList.remove("inspector-hidden");
    $("#inspector-close-btn").style.display = "";
    const enc = this._encodePath(this.selection.path);
    $("#canvas-flow").querySelector(`[data-path="${enc}"]`)
      ?.classList.add("is-selected");
  },

  // ── INSPECTOR (identical to v3 — full params, condition builder) ──


  // ════════════════════════════════════════════════════════════
  //   INSPECTOR (right panel, parameter editor)
  // ════════════════════════════════════════════════════════════

  renderInspector() {
    const body  = $("#inspector-body");
    const title = $("#inspector-title");
    if (!this.selection) {
      title.textContent = "Inspector";
      body.innerHTML = "";
      return;
    }
    const step = this._getAt(this.selection.path);
    if (!step) { this.selection = null; return this.renderInspector(); }
    const meta = this.catalog.find(c => c.type === step.type);
    title.textContent = meta?.label || step.type;

    const sections = [];
    sections.push(`
      <div class="inspector-section">
        <div class="inspector-badge-row">
          <span class="inspector-type-tag">${escapeHtml(step.type)}</span>
          <span class="inspector-cat-tag" data-category="${meta?.category || 'other'}">${
            escapeHtml(meta?.category || 'other')}</span>
        </div>
        ${meta?.description
          ? `<div class="inspector-field-hint">${escapeHtml(meta.description)}</div>`
          : ""}
      </div>`);

    sections.push(`
      <div class="inspector-section">
        <div class="inspector-section-title">Execution</div>
        <label class="inspector-check-row">
          <input type="checkbox" data-insp="enabled"
                 ${step.enabled !== false ? "checked" : ""}>
          <div>
            <div>Enabled</div>
            <span class="inspector-check-row-hint">Disabled steps skip at runtime.</span>
          </div>
        </label>
        <div class="inspector-field" style="margin-top: 8px;">
          <div class="inspector-field-label">
            Probability
            <span style="color: var(--text-muted); font-weight: 400;">
              ${Number(step.probability ?? 1.0).toFixed(2)}
            </span>
          </div>
          <input type="range" min="0" max="1" step="0.05"
                 data-insp="probability"
                 value="${Number(step.probability ?? 1.0)}"
                 style="width: 100%;">
          <div class="inspector-field-hint">
            Fraction of runs that execute this step.
          </div>
        </div>
      </div>`);

    if (step.type === "if") {
      sections.push(this._renderConditionBuilder(step));
    }

    const params = (meta?.params || [])
      .filter(p => !["steps", "then_steps", "else_steps", "condition"]
                       .includes(p.name));
    if (params.length) {
      sections.push(`
        <div class="inspector-section">
          <div class="inspector-section-title">Parameters</div>
          ${params.map(p => this._renderParam(p, step)).join("")}
        </div>`);
    }
    body.innerHTML = sections.join("");
    this._wireInspectorInputs();
  },

  _renderConditionBuilder(step) {
    const cond = step.condition || { kind: "always" };
    const kindMeta = this.conditionKinds.find(k => k.kind === cond.kind);
    const groups = {};
    this.conditionKinds.forEach(k => {
      (groups[k.group || "simple"] ||= []).push(k);
    });
    const groupOrder = ["simple", "ads", "page", "vars"];
    const optHtml = groupOrder
      .filter(g => groups[g])
      .map(g => `
        <optgroup label="${g}">
          ${groups[g].map(k =>
            `<option value="${escapeHtml(k.kind)}"
                     ${k.kind === cond.kind ? "selected" : ""}>${
              escapeHtml(k.label)}</option>`
          ).join("")}
        </optgroup>`).join("");
    const fields = kindMeta?.fields || [];
    const fieldsHtml = fields.map(f => {
      const val = cond[f.name] ?? f.default ?? "";
      const ph  = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
      const needsVars = f.type === "text";
      return `
        <div class="inspector-field ${needsVars ? 'field-has-vars' : ''}">
          <div class="inspector-field-label">${escapeHtml(f.label || f.name)}</div>
          <input type="${f.type === 'number' ? 'number' : 'text'}"
                 class="input" data-cond-field="${escapeHtml(f.name)}"
                 value="${escapeHtml(String(val))}" ${ph}>
        </div>`;
    }).join("");
    return `
      <div class="inspector-section">
        <div class="inspector-section-title">Condition</div>
        <div class="cond-builder">
          <div class="cond-kind-row">
            <select class="select" data-cond-kind>${optHtml}</select>
          </div>
          <label class="cond-negate-check">
            <input type="checkbox" data-cond-negate ${cond.negate ? "checked" : ""}>
            Negate (run when condition is FALSE)
          </label>
          ${fields.length ? `<div class="cond-fields">${fieldsHtml}</div>` : ""}
        </div>
      </div>`;
  },

  _renderParam(p, step) {
    const val = step[p.name] ?? p.default ?? "";
    const label = escapeHtml(p.label || p.name);
    const name  = escapeHtml(p.name);
    const hint  = p.hint
      ? `<div class="inspector-field-hint">${escapeHtml(p.hint)}</div>`
      : "";
    if (p.type === "bool") {
      return `
        <label class="inspector-check-row">
          <input type="checkbox" data-param="${name}" ${val ? "checked" : ""}>
          <div>${label}${hint}</div>
        </label>`;
    }
    if (p.type === "number" || p.type === "int" || p.type === "float") {
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <input type="number" class="input" data-param="${name}"
                 value="${escapeHtml(String(val))}"
                 ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>
          ${hint}
        </div>`;
    }
    // Extensions param type — picker populated from /api/extensions.
    // We render a placeholder select + lazy-fetch the list on first
    // mount; subsequent steps reuse the cached list. The picker keeps
    // the existing value selected even if the pool is loading so the
    // value isn't lost on re-render.
    if (p.type === "extension") {
      // Lazy cache shared across all extension fields in this session
      if (!ScriptsPage._extPoolCache) ScriptsPage._extPoolCache = null;
      const cache = ScriptsPage._extPoolCache;
      const id = `ext-pick-${Math.random().toString(36).slice(2, 8)}`;
      // If we don't have the cache yet, fire a load and re-render
      // this field once it lands. Other fields rendered in the same
      // pass will hook the same in-flight promise.
      if (!cache && !ScriptsPage._extPoolPromise) {
        ScriptsPage._extPoolPromise = fetch("/api/extensions")
          .then(r => r.json())
          .then(j => {
            ScriptsPage._extPoolCache = j?.extensions || [];
            // Re-render every pending picker by re-populating options
            document.querySelectorAll("[data-ext-picker]").forEach(sel => {
              const cur = sel.value;
              sel.innerHTML = ScriptsPage._renderExtensionOptions(cur);
            });
          })
          .catch(() => { ScriptsPage._extPoolCache = []; });
      }
      const optsHtml = cache
        ? ScriptsPage._renderExtensionOptions(val)
        : `<option value="${escapeHtml(String(val || ""))}">${val ? escapeHtml(String(val)) : "Loading…"}</option>`;
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <select class="select" data-param="${name}" data-ext-picker="1" id="${id}">
            ${optsHtml}
          </select>
          ${hint}
        </div>`;
    }
    if (p.type === "select" && Array.isArray(p.options)) {
      const opts = p.options.map(o => {
        const ov = o.value ?? o;
        const ol = o.label ?? o;
        return `<option value="${escapeHtml(String(ov))}"
                        ${String(ov) === String(val) ? "selected" : ""}>${escapeHtml(String(ol))}</option>`;
      }).join("");
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <select class="select" data-param="${name}">${opts}</select>
          ${hint}
        </div>`;
    }
    if (p.type === "textarea" || p.type === "textlist") {
      return `
        <div class="inspector-field field-has-vars">
          <div class="inspector-field-label">
            ${label}
            <button class="inspector-vault-btn" type="button"
                    data-vault-target="${name}"
                    title="Insert credential from Vault">\u{1F511}</button>
          </div>
          <textarea class="input" data-param="${name}" rows="${p.type === "textlist" ? 6 : 4}"
                    ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>${escapeHtml(String(val))}</textarea>
          ${hint}
        </div>`;
    }
    return `
      <div class="inspector-field field-has-vars">
        <div class="inspector-field-label">
          ${label}
          <button class="inspector-vault-btn" type="button"
                  data-vault-target="${name}"
                  title="Insert credential from Vault">\u{1F511}</button>
        </div>
        <input type="text" class="input" data-param="${name}"
               value="${escapeHtml(String(val))}"
               ${p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : ""}>
        ${hint}
      </div>`;
  },

  _wireInspectorInputs() {
    const body = $("#inspector-body");

    // Phase 5.1: 🔑 buttons in inspector. Same UX as inline-edit:
    // click -> picker -> inserts {vault.<id>.<field>} into the
    // matching [data-param=<name>] input. Replaces full content if
    // the existing value is empty/whitespace, otherwise inserts at
    // the cursor.
    body.querySelectorAll(".inspector-vault-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const param = btn.dataset.vaultTarget;
        const input = body.querySelector(`[data-param="${param}"]`);
        if (!input) return;
        this._openVaultPicker((placeholder) => {
          const cur = input.value || "";
          if (!cur || /^\s*$/.test(cur)) {
            input.value = placeholder;
          } else if (input.selectionStart != null) {
            const s = input.selectionStart, e2 = input.selectionEnd;
            input.value = cur.slice(0, s) + placeholder + cur.slice(e2);
            input.selectionStart = input.selectionEnd = s + placeholder.length;
          } else {
            input.value = cur + placeholder;
          }
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
      });
    });

    body.querySelectorAll("[data-insp]").forEach(input => {
      const update = () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        const key = input.dataset.insp;
        const val = input.type === "checkbox" ? input.checked
                  : (input.type === "range" ? parseFloat(input.value)
                  : input.value);
        if (val === "" || val === false) delete step[key];
        else step[key] = val;
        this._markDirty();
        this.renderFlow();
      };
      input.addEventListener("change", update);
      if (input.type === "range") {
        input.addEventListener("input", () => {
          const lbl = input.previousElementSibling;
          const num = lbl?.querySelector("span");
          if (num) num.textContent = Number(input.value).toFixed(2);
        });
      }
    });
    const kindSel = body.querySelector("[data-cond-kind]");
    if (kindSel) {
      kindSel.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = { kind: kindSel.value };
        this._markDirty();
        this.renderInspector();
        this.renderFlow();
      });
    }
    body.querySelectorAll("[data-cond-negate]").forEach(cb => {
      cb.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = step.condition || { kind: "always" };
        step.condition.negate = cb.checked;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll("[data-cond-field]").forEach(input => {
      input.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        step.condition = step.condition || { kind: "always" };
        const key = input.dataset.condField;
        step.condition[key] = input.type === "number"
          ? (input.value === "" ? "" : Number(input.value))
          : input.value;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll("[data-param]").forEach(input => {
      input.addEventListener("change", () => {
        const step = this._getAt(this.selection.path);
        if (!step) return;
        const key = input.dataset.param;
        let val;
        if (input.type === "checkbox") val = input.checked;
        else if (input.type === "number") val = input.value === "" ? "" : Number(input.value);
        else val = input.value;
        if (val === "" || val === null) delete step[key];
        else step[key] = val;
        this._markDirty();
        this.renderFlow();
      });
    });
    body.querySelectorAll(".field-has-vars input, .field-has-vars textarea")
      .forEach(input => {
        input.addEventListener("focus", () => this._openVarPicker(input));
        input.addEventListener("click", () => this._openVarPicker(input));
      });
  },

  // Var picker
  wireVarPicker() {
    const vp = $("#var-picker");
    if (!vp) return;
    document.addEventListener("mousedown", (e) => {
      if (vp.style.display === "none") return;
      if (vp.contains(e.target)) return;
      if (e.target.matches(
        ".field-has-vars input, .field-has-vars textarea")) return;
      vp.style.display = "none";
    });
  },
  _openVarPicker(input) {
    const vp = $("#var-picker");
    if (!vp) return;
    const rect = input.getBoundingClientRect();
    vp.style.left = `${Math.min(rect.left, window.innerWidth - 280)}px`;
    vp.style.top  = `${rect.bottom + 4}px`;
    vp.style.display = "";
    const vars = this._availableVarsForPath(this.selection?.path || []);
    const body = $("#var-picker-body");
    body.innerHTML = vars.map(g => `
      <div class="var-picker-group-label">${escapeHtml(g.label)}</div>
      ${g.items.map(v => `
        <div class="var-picker-item" data-var="${escapeHtml(v.path)}">
          <code>{${escapeHtml(v.path)}}</code>
          <span class="var-picker-item-desc">${escapeHtml(v.desc)}</span>
        </div>`).join("")}
    `).join("");
    body.onclick = (e) => {
      const item = e.target.closest(".var-picker-item");
      if (!item) return;
      const token = `{${item.dataset.var}}`;
      if (input.tagName === "INPUT" || input.tagName === "TEXTAREA") {
        const start = input.selectionStart ?? input.value.length;
        const end   = input.selectionEnd ?? input.value.length;
        input.value = input.value.slice(0, start) + token + input.value.slice(end);
        input.focus();
        input.selectionStart = input.selectionEnd = start + token.length;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
      vp.style.display = "none";
    };
  },
  _availableVarsForPath(path) {
    const groups = [];
    const ancestors = [];
    for (let i = 1; i < path.length; i++) {
      const pref = path.slice(0, i);
      const step = this._getAt(pref);
      if (step) ancestors.push(step);
    }
    const hasForeachAd = ancestors.some(s => s.type === "foreach_ad");
    const hasForeach = ancestors.filter(s => s.type === "foreach" || s.type === "loop");
    if (hasForeachAd) {
      groups.push({
        label: "Current ad",
        items: [
          { path: "ad.domain",          desc: "hostname of the ad" },
          { path: "ad.title",           desc: "ad headline" },
          { path: "ad.clean_url",       desc: "destination URL" },
          { path: "ad.display_url",     desc: "display URL shown on SERP" },
          { path: "ad.google_click_url",desc: "Google tracking URL" },
          { path: "ad.is_target",       desc: "true if target-domain" },
          { path: "ad.ad_format",       desc: "text/shopping_carousel/pla_grid" },
        ],
      });
    }
    if (hasForeach.length) {
      hasForeach.forEach(fe => {
        const v = fe.item_var || "item";
        groups.push({
          label: `Loop variable (${v})`,
          items: [{ path: v, desc: "current iteration value" }],
        });
      });
    }
    groups.push({
      label: "Ads list",
      items: [{ path: "ads.count", desc: "number of ads in context" }],
    });
    groups.push({
      label: "Context",
      items: [
        { path: "query",   desc: "current query string" },
        { path: "profile", desc: "running profile name" },
      ],
    });
    groups.push({
      label: "Saved variables",
      items: [{ path: "var.<n>", desc: "from save_var / extract_text" }],
    });
    return groups;
  },

  // Type picker
  _openTypePicker(target) {
    const modal = $("#type-picker-modal");
    const search = $("#type-picker-search");
    const list  = $("#type-picker-list");
    const render = (q = "") => {
      const qLo = q.trim().toLowerCase();
      const groups = { flow: [], ads: [], navigation: [], interaction: [],
                       timing: [], data: [], external: [], extensions: [],
                       input: [], power: [], other: [] };
      this.catalog.forEach(c => {
        const cat = groups[c.category] ? c.category : "other";
        if (!qLo ||
            (c.label || "").toLowerCase().includes(qLo) ||
            c.type.toLowerCase().includes(qLo)) {
          groups[cat].push(c);
        }
      });
      const order = ["flow", "ads", "navigation", "interaction", "timing",
                     "data", "external", "extensions",
                     "input", "power", "other"];
      list.innerHTML = order.filter(k => groups[k].length).map(k => `
        <div class="palette-group-label" style="padding: 12px 4px 4px;">${k}</div>
        ${groups[k].map(a => `
          <div class="palette-item" data-type="${escapeHtml(a.type)}"
               data-category="${escapeHtml(a.category)}">
            <div class="palette-item-icon">${this._iconFor(a)}</div>
            <div class="palette-item-body">
              <div class="palette-item-label">${escapeHtml(a.label || a.type)}</div>
              <div class="palette-item-desc">${escapeHtml(a.description || "")}</div>
            </div>
          </div>`).join("")}
      `).join("") || `<div class="palette-empty">No matches</div>`;
    };
    render();
    modal.style.display = "";
    search.value = "";
    setTimeout(() => search.focus(), 30);
    search.oninput = (e) => render(e.target.value);
    list.onclick = (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      this._addStep({
        containerPath: target.basePath,
        subKey:        target.subKey,
      }, item.dataset.type);
      this._closeTypePicker();
    };
    modal.querySelectorAll("[data-close]").forEach(el => {
      el.onclick = () => this._closeTypePicker();
    });
    const onKey = (e) => { if (e.key === "Escape") this._closeTypePicker(); };
    modal._pickerKey = onKey;
    document.addEventListener("keydown", onKey);
  },
  _closeTypePicker() {
    const modal = $("#type-picker-modal");
    modal.style.display = "none";
    if (modal._pickerKey) {
      document.removeEventListener("keydown", modal._pickerKey);
      modal._pickerKey = null;
    }
  },

  // Arrows

  // ════════════════════════════════════════════════════════════
  //   FLOW ARROWS (svg overlay)
  // ════════════════════════════════════════════════════════════

  _scheduleArrowsRedraw() {
    if (this._arrowsRAF) cancelAnimationFrame(this._arrowsRAF);
    this._arrowsRAF = requestAnimationFrame(() => {
      this._arrowsRAF = null;
      this._redrawArrows();
    });
  },
  _redrawArrows() {
    const svg  = $("#flow-arrows");
    const wrap = svg?.parentElement;
    if (!svg || !wrap) return;
    const wrapRect = wrap.getBoundingClientRect();
    svg.setAttribute("width",  wrap.scrollWidth);
    svg.setAttribute("height", wrap.scrollHeight);
    svg.innerHTML = "";
    const collectPairs = (parent) => {
      const kids = [...parent.children].filter(
        c => c.matches(".flow-step, .flow-container")
      );
      const pairs = [];
      for (let i = 0; i < kids.length - 1; i++) {
        pairs.push([kids[i], kids[i + 1]]);
      }
      const bodies = [...parent.querySelectorAll(":scope > .flow-container > .flow-container-body")];
      bodies.forEach(b => pairs.push(...collectPairs(b)));
      return pairs;
    };
    const canvas = $("#canvas-flow");
    const pairs = collectPairs(canvas);
    const svgNS = "http://www.w3.org/2000/svg";
    for (const [a, b] of pairs) {
      const r1 = a.getBoundingClientRect();
      const r2 = b.getBoundingClientRect();
      const x1 = r1.left + r1.width / 2 - wrapRect.left + wrap.scrollLeft;
      const y1 = r1.bottom - wrapRect.top + wrap.scrollTop;
      const x2 = r2.left + r2.width / 2 - wrapRect.left + wrap.scrollLeft;
      const y2 = r2.top - wrapRect.top + wrap.scrollTop;
      const midY = (y1 + y2) / 2;
      const d = `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2 - 6}`;
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "flow-arrow-line");
      svg.appendChild(path);
      const head = document.createElementNS(svgNS, "polygon");
      head.setAttribute("class", "flow-arrow-head");
      const hx = x2, hy = y2 - 2;
      head.setAttribute("points",
        `${hx - 4},${hy - 5} ${hx + 4},${hy - 5} ${hx},${hy}`);
      svg.appendChild(head);
    }
  },

  // ============================================================
  //  Phase 5: Hotkeys + command palette + schedule binding
  // ============================================================

  /** Read the hotkey map (digit -> scriptId) from localStorage. The
   *  map is shared across browser tabs but not across users / hosts. */
  _hotkeyMapRead() {
    try {
      const raw = localStorage.getItem("gs_script_hotkeys");
      const m = raw ? JSON.parse(raw) : {};
      return (m && typeof m === "object") ? m : {};
    } catch { return {}; }
  },
  _hotkeyMapWrite(map) {
    try { localStorage.setItem("gs_script_hotkeys", JSON.stringify(map)); }
    catch {}
  },

  async _setHotkeyForScript(sc) {
    const cur = this._hotkeyMapRead();
    // Show what's already taken so the user picks an empty slot
    const lines = [];
    for (let d = 1; d <= 9; d++) {
      const sid = cur[d];
      const s = sid && this.scripts.find(x => x.id === sid);
      lines.push(`Ctrl+${d}: ${s ? s.name : "(empty)"}`);
    }
    const picked = prompt(
      `Pick a digit 1-9 for "${sc.name}".\n` +
      `Empty slot is overwritten silently. To clear, leave blank.\n\n` +
      lines.join("\n"));
    if (picked === null) return;
    const trimmed = (picked || "").trim();
    if (!trimmed) {
      // Clear any existing mapping for this script
      for (const d of Object.keys(cur)) {
        if (cur[d] === sc.id) delete cur[d];
      }
      this._hotkeyMapWrite(cur);
      toast(`\u2713 Cleared hotkey for "${sc.name}"`);
      return;
    }
    if (!/^[1-9]$/.test(trimmed)) {
      toast("Pick a single digit 1..9", true);
      return;
    }
    // Remove this script from any other digit it may have been mapped to
    for (const d of Object.keys(cur)) {
      if (cur[d] === sc.id) delete cur[d];
    }
    cur[trimmed] = sc.id;
    this._hotkeyMapWrite(cur);
    toast(`\u2713 Ctrl+${trimmed} -> "${sc.name}"`);
  },

  async _triggerHotkey(digit) {
    const map = this._hotkeyMapRead();
    const sid = map[digit];
    if (!sid) {
      toast(`Ctrl+${digit} is not assigned. Assign via card \u22EF menu.`);
      return;
    }
    const sc = this.scripts.find(x => x.id === sid);
    if (!sc) {
      toast(`Hotkey ${digit} points at a deleted script -- cleared.`, true);
      delete map[digit]; this._hotkeyMapWrite(map);
      return;
    }
    // If pinned profiles exist, run on them. Otherwise open picker so
    // the user gets to choose -- "magic without surprise".
    const pinned = sc.pinned_profiles || [];
    if (pinned.length) {
      await this._runOnProfiles(sc, pinned);
    } else {
      await this._openProfilePicker(sc, "run");
    }
  },

  /** Ctrl+K: fuzzy-search palette over the entire library. Type to
   *  filter, ArrowUp/Down to navigate, Enter to open the profile
   *  picker for the highlighted script. Esc closes. */
  _openCommandPalette() {
    document.querySelectorAll(".gs-command-palette").forEach(m => m.remove());
    const all = (this.scripts || []).slice();
    if (!all.length) {
      toast("No scripts to pick from. Create one first.");
      return;
    }
    const modal = document.createElement("div");
    modal.className = "profile-modal gs-command-palette";
    modal.innerHTML = `
      <div class="profile-modal-backdrop" data-cancel></div>
      <div class="profile-modal-content cmdp-content" style="width:520px">
        <div class="cmdp-input-row">
          <span class="cmdp-prompt">\u203A</span>
          <input type="text" class="cmdp-input" id="cmdp-input"
                 placeholder="Type to filter scripts...">
        </div>
        <div class="cmdp-list" id="cmdp-list"></div>
        <div class="cmdp-foot">
          <span><kbd>\u2191</kbd><kbd>\u2193</kbd> navigate</span>
          <span><kbd>Enter</kbd> run</span>
          <span><kbd>Esc</kbd> close</span>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    const input = modal.querySelector("#cmdp-input");
    const list  = modal.querySelector("#cmdp-list");
    let active = 0;
    let filtered = all;

    const score = (s, q) => {
      const hay = (s.name + " " + (s.description || "") + " " +
                   (s.tags || []).join(" ")).toLowerCase();
      if (!q) return 1;
      // Cheap fuzzy: every char in q must appear in order in hay
      let i = 0;
      for (const c of q) { i = hay.indexOf(c, i); if (i < 0) return 0; i++; }
      // Bias toward matches in the name
      return s.name.toLowerCase().includes(q) ? 100 : 1;
    };

    const render = () => {
      const q = input.value.trim().toLowerCase();
      filtered = all
        .map(s => ({ s, w: score(s, q) }))
        .filter(x => x.w > 0)
        .sort((a, b) => b.w - a.w || a.s.name.localeCompare(b.s.name))
        .map(x => x.s);
      if (active >= filtered.length) active = Math.max(0, filtered.length - 1);
      list.innerHTML = filtered.length
        ? filtered.map((s, i) => `
            <div class="cmdp-row ${i === active ? 'is-active' : ''}"
                 data-idx="${i}">
              <span class="cmdp-row-name">${escapeHtml(s.name)}</span>
              <span class="cmdp-row-desc">${escapeHtml((s.description || "").slice(0, 60))}</span>
              <span class="cmdp-row-meta">${(s.tags || []).slice(0, 3).map(t => escapeHtml(t)).join(" \u00B7 ")}</span>
            </div>`).join("")
        : `<div class="cmdp-empty">No matches.</div>`;
      list.querySelectorAll(".cmdp-row").forEach(row => {
        row.addEventListener("mouseenter", () => {
          active = Number(row.dataset.idx);
          render();
        });
        row.addEventListener("click", () => {
          modal.remove();
          this._openProfilePicker(filtered[Number(row.dataset.idx)], "run");
        });
      });
    };

    const onKey = (ev) => {
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        active = Math.min(filtered.length - 1, active + 1); render();
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        active = Math.max(0, active - 1); render();
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        const sc = filtered[active];
        if (sc) {
          modal.remove();
          this._openProfilePicker(sc, "run");
        }
      } else if (ev.key === "Escape") {
        modal.remove();
      }
    };
    input.addEventListener("keydown", onKey);
    input.addEventListener("input", render);
    modal.querySelector("[data-cancel]").addEventListener("click", () => modal.remove());

    render();
    setTimeout(() => input.focus(), 30);
  },

  /** Phase 5 #96: schedule binding modal. Creates rows in
   *  scheduled_tasks table -- one row per cron+profiles binding for
   *  this script. The scheduler daemon picks them up on its next
   *  tick (when it is started). Multiple cron rows per script are
   *  supported (e.g. "weekday morning warmup" + "weekend evening
   *  recon" can coexist).
   *
   *  UI: a list of existing tasks at top, "+ Add new" row at bottom.
   *  Each task row has cron / profile-count / enabled-toggle / del.
   *  Profile picking reuses the existing _openProfilePicker pattern. */
  async _openScheduleModal(sc) {
    let tasks = [];
    let profiles = [];
    try {
      const tr = await api(`/api/scripts/${sc.id}/schedules`);
      tasks = tr.tasks || [];
      const pr = await api("/api/profiles");
      profiles = (pr.profiles || pr || []).map(p =>
        typeof p === "string" ? { name: p } : p);
    } catch (e) {
      toast(`Could not load schedule data: ${e.message}`, true);
      return;
    }

    document.querySelectorAll(".gs-schedule-modal").forEach(m => m.remove());
    const modal = document.createElement("div");
    modal.className = "profile-modal gs-schedule-modal";
    const renderTaskRow = (t) => {
      const profs = (t.profiles || []).join(", ") || "(no profiles)";
      return `<div class="sched-row" data-task-id="${t.id}">
        <input class="input sched-cron" data-field="cron_expr"
               value="${escapeHtml(t.cron_expr)}" placeholder="m h dom mon dow">
        <input class="input sched-name" data-field="name"
               value="${escapeHtml(t.name || "")}" placeholder="(optional name)">
        <span class="sched-profs" title="${escapeHtml(profs)}"
              data-profs='${escapeHtml(JSON.stringify(t.profiles || []))}'>
          \u{1F465} ${(t.profiles || []).length}
        </span>
        <label class="sched-toggle">
          <input type="checkbox" data-field="enabled" ${t.enabled ? "checked" : ""}>
          <span>on</span>
        </label>
        <button class="btn btn-tiny" data-act="pick-profs">Profiles\u2026</button>
        <button class="btn btn-tiny btn-danger-sm" data-act="del">\u2715</button>
      </div>`;
    };
    const tasksHtml = tasks.length
      ? tasks.map(renderTaskRow).join("")
      : `<div class="sched-empty">No schedules yet for this script.</div>`;

    modal.innerHTML = `
      <div class="profile-modal-backdrop" data-cancel></div>
      <div class="profile-modal-content" style="width:680px">
        <div class="profile-modal-header">
          <div class="profile-modal-title">\u23F0  Schedule "${escapeHtml(sc.name)}"</div>
          <button class="profile-modal-close" data-cancel>\u2715</button>
        </div>
        <div class="profile-modal-body">
          <div class="picker-help">
            Each row binds this script to a cron expression + a set of
            profiles. The scheduler daemon picks rows up on its next
            tick. Cron format: <code>m h dom mon dow</code> -- e.g.
            <code>0 9 * * 1-5</code> = 09:00 on weekdays.
          </div>
          <div class="sched-list" id="sched-list">${tasksHtml}</div>
          <div class="sched-add-row">
            <input class="input" id="sched-add-cron" placeholder="0 9 * * 1-5">
            <input class="input" id="sched-add-name" placeholder="(optional name)">
            <button class="btn btn-secondary" id="sched-add-pick">\u{1F465} Pick profiles\u2026</button>
            <span class="sched-add-prof-count" id="sched-add-prof-count">0</span>
            <button class="btn btn-primary" id="sched-add-btn">+ Add</button>
          </div>
        </div>
        <div class="profile-modal-footer">
          <button class="btn btn-secondary" data-cancel>Close</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    let newProfs = [];
    const profCount = modal.querySelector("#sched-add-prof-count");
    const list      = modal.querySelector("#sched-list");

    // Cancel handlers
    modal.querySelectorAll("[data-cancel]").forEach(b =>
      b.addEventListener("click", () => modal.remove()));

    // Profile-pick for the new row
    const pickProfilesInline = (current, onPick) => {
      const sub = document.createElement("div");
      sub.className = "profile-modal gs-profile-picker-modal";
      sub.style.zIndex = "1200";
      const sel = new Set(current);
      sub.innerHTML = `
        <div class="profile-modal-backdrop" data-subcancel></div>
        <div class="profile-modal-content" style="width:420px">
          <div class="profile-modal-header">
            <div class="profile-modal-title">Pick profiles</div>
            <button class="profile-modal-close" data-subcancel>\u2715</button>
          </div>
          <div class="profile-modal-body">
            <div class="picker-list" style="max-height:300px;overflow:auto;border:1px solid var(--border);border-radius:4px;background:var(--bg)">
              ${profiles.map(p => `
                <label class="picker-row">
                  <input type="checkbox" value="${escapeHtml(p.name)}" ${sel.has(p.name) ? "checked" : ""}>
                  <span class="picker-row-name">${escapeHtml(p.name)}</span>
                </label>
              `).join("")}
            </div>
          </div>
          <div class="profile-modal-footer">
            <button class="btn btn-secondary" data-subcancel>Cancel</button>
            <button class="btn btn-primary" data-subok>OK</button>
          </div>
        </div>`;
      document.body.appendChild(sub);
      sub.querySelectorAll("input[type='checkbox']").forEach(cb => {
        cb.addEventListener("change", () => {
          if (cb.checked) sel.add(cb.value); else sel.delete(cb.value);
        });
      });
      sub.querySelectorAll("[data-subcancel]").forEach(b =>
        b.addEventListener("click", () => sub.remove()));
      sub.querySelector("[data-subok]").addEventListener("click", () => {
        sub.remove();
        onPick(Array.from(sel));
      });
    };

    modal.querySelector("#sched-add-pick").addEventListener("click", () => {
      pickProfilesInline(newProfs, (chosen) => {
        newProfs = chosen;
        profCount.textContent = chosen.length;
      });
    });

    modal.querySelector("#sched-add-btn").addEventListener("click", async () => {
      const cron = modal.querySelector("#sched-add-cron").value.trim();
      const name = modal.querySelector("#sched-add-name").value.trim();
      if (!cron) {
        toast("cron_expr is required", true);
        return;
      }
      try {
        await api(`/api/scripts/${sc.id}/schedules`, {
          method: "POST",
          body: JSON.stringify({ cron_expr: cron, profiles: newProfs,
                                  name, enabled: true }),
        });
        toast("\u2713 Schedule added");
        modal.remove();
        this._openScheduleModal(sc);  // re-open to show new row
      } catch (e) {
        toast(`Add failed: ${e.message}`, true);
      }
    });

    // Per-row actions: pick-profs / del / field changes
    list.addEventListener("click", async (e) => {
      const row = e.target.closest(".sched-row");
      if (!row) return;
      const tid = Number(row.dataset.taskId);
      const act = e.target.dataset.act;
      if (act === "del") {
        if (!confirm("Delete this schedule?")) return;
        try {
          await api(`/api/schedules/${tid}`, { method: "DELETE" });
          toast("\u2713 Deleted");
          row.remove();
        } catch (er) { toast(`Delete failed: ${er.message}`, true); }
      } else if (act === "pick-profs") {
        const span = row.querySelector(".sched-profs");
        const current = JSON.parse(span.dataset.profs || "[]");
        pickProfilesInline(current, async (chosen) => {
          try {
            await api(`/api/schedules/${tid}`, {
              method: "PATCH",
              body: JSON.stringify({ profiles: chosen }),
            });
            span.dataset.profs = JSON.stringify(chosen);
            span.title = chosen.join(", ") || "(no profiles)";
            span.innerHTML = `\u{1F465} ${chosen.length}`;
            toast("\u2713 Profiles updated");
          } catch (er) { toast(`Update failed: ${er.message}`, true); }
        });
      }
    });

    list.addEventListener("change", async (e) => {
      const row = e.target.closest(".sched-row");
      if (!row) return;
      const tid = Number(row.dataset.taskId);
      const field = e.target.dataset.field;
      if (!field) return;
      const value = e.target.type === "checkbox"
        ? e.target.checked
        : e.target.value;
      try {
        await api(`/api/schedules/${tid}`, {
          method: "PATCH",
          body: JSON.stringify({ [field]: value }),
        });
      } catch (er) { toast(`Update failed: ${er.message}`, true); }
    });
  },

    // ============================================================
  //  Phase 2: card "..." menu + Apply/Run/Pin to N profiles
  // ============================================================

  /** Floating action menu opened by the ⋯ button on each library card.
   *  Positioned absolutely below the button; clicking outside closes it.
   *  Menu items dispatch to _act* methods; profile picker is opened on
   *  demand (Apply / Run / Pin).
   */
  _openCardMenu(btn, scriptId) {
    // Close any existing menu first -- single-instance UI
    document.querySelectorAll(".library-card-popmenu").forEach(m => m.remove());

    const sc = this.scripts.find(s => s.id === scriptId);
    if (!sc) return;

    const menu = document.createElement("div");
    menu.className = "library-card-popmenu";
    menu.innerHTML = `
      <button data-act="edit">\u270F\uFE0F  Edit</button>
      <button data-act="run">\u25B6\uFE0F  Run on profiles\u2026</button>
      <button data-act="run-pinned" ${sc.pinned_profiles && sc.pinned_profiles.length ? "" : "disabled"}>
        \u26A1  Run on pinned (${(sc.pinned_profiles||[]).length})
      </button>
      <button data-act="apply">\u{1F4CC}  Apply to profiles\u2026</button>
      <button data-act="pin">\u{1F516}  Pin profiles\u2026</button>
      <hr>
      <button data-act="duplicate">\u{1F4CB}  Duplicate</button>
      <button data-act="export">\u{1F4E4}  Export JSON</button>
      ${sc.is_default ? "" : `<button data-act="make-default">\u2605  Make default</button>`}
      <button data-act="set-hotkey">\u2328\uFE0F  Assign hotkey (Ctrl+1..9)\u2026</button>
      <button data-act="schedule">\u23F0  Schedule\u2026</button>
      <hr>
      <button data-act="delete" class="popmenu-danger">\u{1F5D1}  Delete</button>
    `;
    document.body.appendChild(menu);

    // Position: below the button, clamped to viewport
    const r = btn.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.top  = `${r.bottom + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 240))}px`;

    // Close on outside click
    const onAway = (e) => {
      if (!menu.contains(e.target)) {
        menu.remove();
        document.removeEventListener("click", onAway, true);
      }
    };
    setTimeout(() => document.addEventListener("click", onAway, true), 0);

    // Wire actions
    menu.querySelectorAll("button[data-act]").forEach(b => {
      b.addEventListener("click", async (e) => {
        e.stopPropagation();
        const act = b.dataset.act;
        menu.remove();
        document.removeEventListener("click", onAway, true);
        await this._dispatchCardAction(act, sc);
      });
    });
  },

  async _dispatchCardAction(act, sc) {
    switch (act) {
      case "edit":         return this._showEditor(sc.id);
      case "run":          return this._openProfilePicker(sc, "run");
      case "run-pinned":   return this._runOnProfiles(sc, sc.pinned_profiles || []);
      case "apply":        return this._openProfilePicker(sc, "apply");
      case "pin":          return this._openProfilePicker(sc, "pin");
      case "duplicate":    return this._duplicateScript(sc);
      case "export":       return this._exportScript(sc);
      case "make-default": return this._makeDefaultFromCard(sc);
      case "set-hotkey":   return this._setHotkeyForScript(sc);
      case "schedule":     return this._openScheduleModal(sc);
      case "delete":       return this._deleteFromCard(sc);
    }
  },

  // ----- profile picker modal -----------------------------------------

  /** Mode: "apply" | "run" | "pin". Determines title, primary-button
   *  label, default-checked set, and which API call fires on confirm. */
  async _openProfilePicker(sc, mode) {
    let profiles = [];
    try {
      const list = await api("/api/profiles");
      profiles = (list.profiles || list || []).map(p =>
        typeof p === "string" ? { name: p } : p);
    } catch (e) {
      toast(`Could not load profiles: ${e.message}`, true);
      return;
    }
    if (!profiles.length) {
      toast("No profiles yet -- create one on the Profiles page first.", true);
      return;
    }

    // Pre-select sensible defaults: pinned for "run" / "pin",
    // currently-assigned for "apply".
    let preselected = new Set();
    if (mode === "run" || mode === "pin") {
      (sc.pinned_profiles || []).forEach(n => preselected.add(n));
    } else if (mode === "apply") {
      try {
        const r = await api(`/api/scripts/${sc.id}`);
        // Existing assignments come from the script_profiles list.
        // The plain GET doesn't include them, so we fetch separately:
        const assigns = await api(`/api/scripts/${sc.id}/profiles`)
          .catch(() => null);
        if (assigns && Array.isArray(assigns.profiles)) {
          assigns.profiles.forEach(n => preselected.add(n));
        }
      } catch {}
    }

    this._renderProfilePicker(sc, mode, profiles, preselected);
  },

  _renderProfilePicker(sc, mode, profiles, preselected) {
    document.querySelectorAll(".gs-profile-picker-modal").forEach(m => m.remove());

    const titleMap = {
      apply: `Apply "${sc.name}" to profiles`,
      run:   `Run "${sc.name}" on profiles`,
      pin:   `Pin profiles to "${sc.name}"`,
    };
    const ctaMap = {
      apply: "Apply to selected",
      run:   "Run on selected",
      pin:   "Save pins",
    };
    const helpMap = {
      apply: "Selected profiles will be permanently bound to this script. Other scripts they had get unbound.",
      run:   "Selected profiles will START a run NOW (each on its own browser process). One-run-per-profile rule applies.",
      pin:   "Pinned profiles show as chips on the script card. Use \"Run on pinned\" for one-click bulk runs.",
    };

    const modal = document.createElement("div");
    modal.className = "profile-modal gs-profile-picker-modal";
    modal.innerHTML = `
      <div class="profile-modal-backdrop" data-cancel></div>
      <div class="profile-modal-content" style="width:520px">
        <div class="profile-modal-header">
          <div class="profile-modal-title">${escapeHtml(titleMap[mode])}</div>
          <button class="profile-modal-close" data-cancel>\u2715</button>
        </div>
        <div class="profile-modal-body">
          <div class="picker-help">${escapeHtml(helpMap[mode])}</div>
          <input type="text" class="input picker-search"
                 placeholder="Search profiles\u2026">
          <div class="picker-actions-row">
            <button class="btn btn-tiny" data-pick="all">All</button>
            <button class="btn btn-tiny" data-pick="none">None</button>
            <button class="btn btn-tiny" data-pick="invert">Invert</button>
            <span class="picker-count" data-count>0 selected</span>
          </div>
          <div class="picker-list" data-list></div>
        </div>
        <div class="profile-modal-footer">
          <button class="btn btn-secondary" data-cancel>Cancel</button>
          <button class="btn btn-primary" data-confirm>${escapeHtml(ctaMap[mode])}</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    const list  = modal.querySelector("[data-list]");
    const count = modal.querySelector("[data-count]");
    const search = modal.querySelector(".picker-search");

    const renderList = (filter) => {
      const f = (filter || "").toLowerCase();
      list.innerHTML = profiles
        .filter(p => !f || p.name.toLowerCase().includes(f))
        .map(p => {
          const checked = preselected.has(p.name) ? "checked" : "";
          return `<label class="picker-row">
            <input type="checkbox" value="${escapeHtml(p.name)}" ${checked}>
            <span class="picker-row-name">${escapeHtml(p.name)}</span>
            ${p.proxy ? `<span class="picker-row-meta">${escapeHtml(p.proxy)}</span>` : ""}
          </label>`;
        }).join("");
      // Re-bind change events on filtered rows
      list.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener("change", () => {
          if (cb.checked) preselected.add(cb.value);
          else preselected.delete(cb.value);
          count.textContent = `${preselected.size} selected`;
        });
      });
    };
    renderList("");
    count.textContent = `${preselected.size} selected`;

    search.addEventListener("input", () => renderList(search.value));

    modal.querySelectorAll("[data-pick]").forEach(b => {
      b.addEventListener("click", () => {
        const all = profiles.map(p => p.name);
        if (b.dataset.pick === "all")    all.forEach(n => preselected.add(n));
        if (b.dataset.pick === "none")   preselected.clear();
        if (b.dataset.pick === "invert") {
          all.forEach(n => preselected.has(n) ? preselected.delete(n) : preselected.add(n));
        }
        renderList(search.value);
        count.textContent = `${preselected.size} selected`;
      });
    });

    modal.querySelectorAll("[data-cancel]").forEach(b => {
      b.addEventListener("click", () => modal.remove());
    });

    modal.querySelector("[data-confirm]").addEventListener("click", async () => {
      const chosen = Array.from(preselected);
      modal.remove();
      if (mode === "apply") return this._applyToProfiles(sc, chosen);
      if (mode === "run")   return this._runOnProfiles(sc, chosen);
      if (mode === "pin")   return this._pinProfiles(sc, chosen);
    });
  },

  async _applyToProfiles(sc, profiles) {
    if (!profiles.length) {
      toast("No profiles selected.", true);
      return;
    }
    try {
      const r = await api(`/api/scripts/${sc.id}/assign`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ profiles }),
      });
      toast(`\u2713 Applied "${sc.name}" to ${profiles.length} profile${profiles.length > 1 ? "s" : ""}`);
      await this.loadLibrary();
    } catch (e) {
      toast(`Apply failed: ${e.message}`, true);
    }
  },

  async _runOnProfiles(sc, profiles) {
    if (!profiles.length) {
      toast("No profiles selected.", true);
      return;
    }
    try {
      const r = await api(`/api/scripts/${sc.id}/run`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ profiles, assign: true }),
      });
      const ok    = r.started || 0;
      const total = r.total   || profiles.length;
      const fails = (r.results || []).filter(x => !x.ok);
      if (fails.length) {
        const why = fails.slice(0, 3)
          .map(f => `${f.profile}: ${f.error}`).join("; ");
        toast(`Started ${ok}/${total}. Failed: ${why}${fails.length > 3 ? `; +${fails.length - 3} more` : ""}`, true);
      } else {
        toast(`\u2713 Started ${ok} run${ok > 1 ? "s" : ""}`);
      }
      // Library refresh will pick up the new run -> updated last_run_*
      // status badges on next reload.
      setTimeout(() => this.loadLibrary().catch(() => {}), 800);
    } catch (e) {
      toast(`Run failed: ${e.message}`, true);
    }
  },

  async _pinProfiles(sc, profiles) {
    try {
      await api(`/api/scripts/${sc.id}/pin`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ profiles }),
      });
      toast(`\u2713 Pinned ${profiles.length} profile${profiles.length === 1 ? "" : "s"} to "${sc.name}"`);
      await this.loadLibrary();
    } catch (e) {
      toast(`Could not pin: ${e.message}`, true);
    }
  },

  async _duplicateScript(sc) {
    try {
      const full = await api(`/api/scripts/${sc.id}`);
      const baseName = full.name + " (copy)";
      let name = baseName;
      let n = 2;
      while (this.scripts.some(x => x.name === name)) {
        name = `${baseName} ${n++}`;
      }
      await api(`/api/scripts`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          name,
          description: full.description || "",
          flow:        full.flow || [],
        }),
      });
      toast(`\u2713 Duplicated as "${name}"`);
      await this.loadLibrary();
    } catch (e) {
      toast(`Duplicate failed: ${e.message}`, true);
    }
  },

  async _exportScript(sc) {
    try {
      // Bug fix: endpoint returns {"script": {...}} (envelope) but
      // the previous code read fields directly off `full`, so
      // full.name was undefined -> "Cannot read properties of
      // undefined (reading 'replace')". Unwrap the envelope and
      // also guard against partial / null fields so the export
      // never crashes mid-download.
      const resp = await api(`/api/scripts/${sc.id}`);
      const full = (resp && resp.script) || resp || {};
      const name        = full.name || sc.name || `script_${sc.id}`;
      const description = full.description || "";
      const flow        = Array.isArray(full.flow) ? full.flow : [];
      const tags        = Array.isArray(full.tags) ? full.tags : [];
      const blob = new Blob(
        [JSON.stringify({
          // Match the format the importer expects (see /api/scripts
          // POST + the existing _exportScript at line 669 which uses
          // a _meta envelope). Add _meta here too for symmetry.
          _meta: {
            format:      "ghost-shell-flow",
            version:     1,
            name:        name,
            description: description,
            exported_at: new Date().toISOString(),
          },
          name:        name,
          description: description,
          tags:        tags,
          flow:        flow,
        }, null, 2)],
        { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const safeName = String(name).replace(/[^\w.-]+/g, "_") || `script_${sc.id}`;
      a.download = `${safeName}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast(`✓ Exported ${safeName}.json`);
    } catch (e) {
      toast(`Export failed: ${e.message || e}`, true);
    }
  },

  async _makeDefaultFromCard(sc) {
    try {
      await api(`/api/scripts/${sc.id}`, {
        method:  "PUT",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ is_default: true }),
      });
      toast(`\u2713 "${sc.name}" is now the default`);
      await this.loadLibrary();
    } catch (e) {
      toast(`Could not set default: ${e.message}`, true);
    }
  },

  async _deleteFromCard(sc) {
    if (!confirm(`Delete "${sc.name}"?\n\nProfiles using it will fall back to the default script.`)) {
      return;
    }
    try {
      await api(`/api/scripts/${sc.id}`, { method: "DELETE" });
      toast(`\u2713 Deleted "${sc.name}"`);
      await this.loadLibrary();
    } catch (e) {
      toast(`Delete failed: ${e.message}`, true);
    }
  },

  // ============================================================
  //  Phase 3: Templates library
  // ============================================================

  async _openTemplatesModal() {
    let templates = [];
    let errors = [];
    try {
      const r = await api("/api/scripts/templates");
      templates = r.templates || [];
      errors    = r.errors    || [];
    } catch (e) {
      toast(`Could not load templates: ${e.message}`, true);
      return;
    }

    document.querySelectorAll(".gs-templates-modal").forEach(m => m.remove());
    const modal = document.createElement("div");
    modal.className = "profile-modal gs-templates-modal";
    modal.innerHTML = `
      <div class="profile-modal-backdrop" data-cancel></div>
      <div class="profile-modal-content" style="width:840px">
        <div class="profile-modal-header">
          <div class="profile-modal-title">\u{1F4DA} Script templates</div>
          <button class="profile-modal-close" data-cancel>\u2715</button>
        </div>
        <div class="profile-modal-body">
          <div class="picker-help">
            Pick a starter recipe -- creates a new script seeded with the
            template's flow. Edit it like any other script after creation.
          </div>
          ${errors.length ? `<div class="picker-help" style="color:var(--accent-red)">
            ${errors.length} template${errors.length>1?"s":""} failed to load: ${escapeHtml(errors.join("; "))}
          </div>` : ""}
          <div class="templates-grid">
            ${templates.length ? templates.map(t => `
              <div class="template-card" data-tpl="${escapeHtml(t.filename || t.name)}">
                <div class="template-card-name">${escapeHtml(t.name)}</div>
                <div class="template-card-desc">${escapeHtml(t.description || "")}</div>
                <div class="template-card-meta">
                  <span class="template-card-tag">${escapeHtml(t.category || "general")}</span>
                  <span class="template-card-stat"><strong>${t.step_count || 0}</strong> steps</span>
                </div>
                ${(t.tags || []).length ? `<div class="template-card-tags">
                  ${t.tags.map(tag => `<span class="template-tag">${escapeHtml(tag)}</span>`).join("")}
                </div>` : ""}
                <button class="btn btn-primary btn-sm template-use-btn">Use this</button>
              </div>
            `).join("") : `<div class="library-empty">No templates available.</div>`}
          </div>
        </div>
        <div class="profile-modal-footer">
          <button class="btn btn-secondary" data-cancel>Close</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    modal.querySelectorAll("[data-cancel]").forEach(b => {
      b.addEventListener("click", () => modal.remove());
    });
    modal.querySelectorAll(".template-use-btn").forEach(b => {
      b.addEventListener("click", async () => {
        const card = b.closest(".template-card");
        const tplName = card.dataset.tpl;
        const tpl = templates.find(t => (t.filename || t.name) === tplName);
        if (!tpl) return;
        // Compute non-conflicting name
        let name = tpl.name;
        let n = 2;
        while (this.scripts.some(x => x.name === name)) {
          name = `${tpl.name} (${n++})`;
        }
        try {
          const r = await api("/api/scripts", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({
              name,
              description: tpl.description || "",
              flow:        tpl.flow || [],
            }),
          });
          modal.remove();
          toast(`\u2713 Created "${name}" from template`);
          await this.loadLibrary();
          if (r && r.id) {
            // Drop user straight into the editor for review/tweaks
            this._showEditor(r.id);
          }
        } catch (e) {
          toast(`Create from template failed: ${e.message}`, true);
        }
      });
    });
  },

    // Icons

  // ════════════════════════════════════════════════════════════
  //   ICONS
  // ════════════════════════════════════════════════════════════

  // ════════════════════════════════════════════════════════════
  //   INLINE PARAM EDITING (Phase 5 #98)
  // ════════════════════════════════════════════════════════════

  /** Render a single parameter as a compact inline form row. Output
   *  matches inspector visuals but is rendered INSIDE the step card
   *  rather than the right-side panel. Inputs carry `data-inline-param`
   *  and `data-inline-path` so a single delegated change listener
   *  (in wireCanvasInteractions) routes the value back to the step. */
  /** Strip transient UI state (e.g. _inlineOpen) before sending
   *  the flow to the server. Recursive over nested containers. */
  _cleanFlowForSave(flow) {
    const strip = (steps) => (steps || []).map(s => {
      const c = { ...s };
      delete c._inlineOpen;
      if (Array.isArray(c.steps))      c.steps      = strip(c.steps);
      if (Array.isArray(c.then_steps)) c.then_steps = strip(c.then_steps);
      if (Array.isArray(c.else_steps)) c.else_steps = strip(c.else_steps);
      return c;
    });
    return strip(flow);
  },

  _renderInlineParam(p, step, path) {
    const cur = step[p.name] ?? p.default ?? "";
    const enc = this._encodePath(path);
    const lab = escapeHtml(p.label || p.name);
    const hint = p.hint ? `<span class="inline-param-hint">${escapeHtml(p.hint)}</span>` : "";
    const required = p.required ? " *" : "";
    const ph = p.placeholder ? `placeholder="${escapeHtml(p.placeholder)}"` : "";

    if (p.type === "bool") {
      return `<label class="inline-param inline-param-bool">
        <input type="checkbox" data-inline-param="${escapeHtml(p.name)}"
               data-inline-path="${enc}" ${cur ? "checked" : ""}>
        <span class="inline-param-label">${lab}${required}</span>${hint}
      </label>`;
    }
    if (p.type === "number") {
      return `<div class="inline-param">
        <label class="inline-param-label">${lab}${required}</label>
        <input type="number" class="inline-param-input"
               data-inline-param="${escapeHtml(p.name)}"
               data-inline-path="${enc}" value="${escapeHtml(String(cur))}" ${ph}>
        ${hint}
      </div>`;
    }
    // Extensions param type — picker populated from /api/extensions.
    // We render a placeholder select + lazy-fetch the list on first
    // mount; subsequent steps reuse the cached list. The picker keeps
    // the existing value selected even if the pool is loading so the
    // value isn't lost on re-render.
    if (p.type === "extension") {
      // Lazy cache shared across all extension fields in this session
      if (!ScriptsPage._extPoolCache) ScriptsPage._extPoolCache = null;
      const cache = ScriptsPage._extPoolCache;
      const id = `ext-pick-${Math.random().toString(36).slice(2, 8)}`;
      // If we don't have the cache yet, fire a load and re-render
      // this field once it lands. Other fields rendered in the same
      // pass will hook the same in-flight promise.
      if (!cache && !ScriptsPage._extPoolPromise) {
        ScriptsPage._extPoolPromise = fetch("/api/extensions")
          .then(r => r.json())
          .then(j => {
            ScriptsPage._extPoolCache = j?.extensions || [];
            // Re-render every pending picker by re-populating options
            document.querySelectorAll("[data-ext-picker]").forEach(sel => {
              const cur = sel.value;
              sel.innerHTML = ScriptsPage._renderExtensionOptions(cur);
            });
          })
          .catch(() => { ScriptsPage._extPoolCache = []; });
      }
      const optsHtml = cache
        ? ScriptsPage._renderExtensionOptions(val)
        : `<option value="${escapeHtml(String(val || ""))}">${val ? escapeHtml(String(val)) : "Loading…"}</option>`;
      return `
        <div class="inspector-field">
          <div class="inspector-field-label">${label}</div>
          <select class="select" data-param="${name}" data-ext-picker="1" id="${id}">
            ${optsHtml}
          </select>
          ${hint}
        </div>`;
    }
    if (p.type === "select" && Array.isArray(p.options)) {
      const opts = p.options.map(o => {
        const v = typeof o === "string" ? o : o.value;
        const l = typeof o === "string" ? o : (o.label || o.value);
        return `<option value="${escapeHtml(v)}" ${String(v) === String(cur) ? "selected" : ""}>${escapeHtml(l)}</option>`;
      }).join("");
      return `<div class="inline-param">
        <label class="inline-param-label">${lab}${required}</label>
        <select class="inline-param-input"
                data-inline-param="${escapeHtml(p.name)}"
                data-inline-path="${enc}">${opts}</select>
        ${hint}
      </div>`;
    }
    if (p.type === "json" || p.type === "textarea") {
      const v = typeof cur === "string" ? cur : JSON.stringify(cur, null, 2);
      return `<div class="inline-param">
        <label class="inline-param-label">${lab}${required}
          <button class="inline-vault-btn-tiny" type="button"
                  data-vault-target-param="${escapeHtml(p.name)}"
                  data-vault-target-path="${enc}"
                  title="Insert from Credential Vault">\u{1F511}</button>
        </label>
        <textarea class="inline-param-input"
                  data-inline-param="${escapeHtml(p.name)}"
                  data-inline-path="${enc}"
                  data-inline-json="${p.type === 'json' ? '1' : '0'}"
                  rows="3" ${ph}>${escapeHtml(v)}</textarea>
        ${hint}
      </div>`;
    }
    // Default: text -- with a 🔑 button that opens the vault picker
    // and inserts {vault.<id>.<field>} into the input. Cleanly works
    // alongside the regular value -- the placeholder gets resolved at
    // runtime by the dashboard before the subprocess sees the flow.
    return `<div class="inline-param">
      <label class="inline-param-label">${lab}${required}</label>
      <div class="inline-param-row">
        <input type="text" class="inline-param-input"
               data-inline-param="${escapeHtml(p.name)}"
               data-inline-path="${enc}"
               value="${escapeHtml(String(cur))}" ${ph}>
        <button class="inline-vault-btn" type="button"
                data-vault-target-param="${escapeHtml(p.name)}"
                data-vault-target-path="${enc}"
                title="Insert from Credential Vault">\u{1F511}</button>
      </div>
      ${hint}
    </div>`;
  },

  /** Wire delegated input/change listener for inline-param fields.
   *  Called once from wireCanvasInteractions on each canvas re-render.
   *  Idempotency: we attach to the canvas root (which is regenerated)
   *  so re-render naturally drops old listeners.
   *
   *  Value extraction respects type:
   *    - checkbox          -> .checked (bool)
   *    - data-inline-json  -> JSON.parse, fail-graceful to raw string
   *    - number            -> Number()
   *    - everything else   -> .value (string)
   */
  _wireInlineParams() {
    const root = $("#canvas-flow");
    if (!root) return;
    const handler = (e) => {
      const el = e.target.closest("[data-inline-param]");
      if (!el) return;
      const name = el.dataset.inlineParam;
      const path = this._decodePath(el.dataset.inlinePath);
      const step = this._getAt(path);
      if (!step) return;
      let v;
      if (el.type === "checkbox") {
        v = el.checked;
      } else if (el.type === "number") {
        v = el.value === "" ? "" : Number(el.value);
      } else if (el.dataset.inlineJson === "1") {
        try { v = JSON.parse(el.value || "null"); }
        catch { v = el.value; }  // keep raw on parse fail; user sees the error on save
      } else {
        v = el.value;
      }
      step[name] = v;
      this._markDirty();
      // Re-render chips ONLY for this step -- avoid full canvas
      // rebuild which would steal focus from the input the user is
      // typing in. The body/chips are the only summary affected.
      const card = el.closest(".flow-step");
      if (card) {
        const meta = this.catalog.find(c => c.type === card.dataset.type);
        const chips = card.querySelector(".flow-step-body");
        if (chips && meta) chips.innerHTML = this._buildChips(step, meta);
      }
    };
    root.addEventListener("input",  handler);
    root.addEventListener("change", handler);

    // Phase 5.1: 🔑 button click -> open vault picker.
    root.addEventListener("click", (e) => {
      const btn = e.target.closest(".inline-vault-btn, .inline-vault-btn-tiny");
      if (!btn) return;
      e.stopPropagation();
      const card = btn.closest(".flow-step");
      const path = btn.dataset.vaultTargetPath;
      const param = btn.dataset.vaultTargetParam;
      const input = card?.querySelector(
        `[data-inline-param="${param}"][data-inline-path="${path}"]`);
      if (!input) return;
      this._openVaultPicker((placeholder) => {
        // Insert the placeholder at the cursor position; if the input
        // had a non-empty current value we replace it (most common case
        // for credential fields).
        const cur = input.value || "";
        if (!cur || /^\s*$/.test(cur)) {
          input.value = placeholder;
        } else if (input.selectionStart != null) {
          const s = input.selectionStart, e = input.selectionEnd;
          input.value = cur.slice(0, s) + placeholder + cur.slice(e);
          input.selectionStart = input.selectionEnd = s + placeholder.length;
        } else {
          input.value = cur + placeholder;
        }
        // Trigger change-event so step state gets updated
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
  },

  /** Phase 5.1: vault picker modal. Lists unlocked vault items;
   *  picking one + a field inserts a {vault.<id>.<field>} placeholder
   *  into the calling input. Vault must be unlocked at run-launch time
   *  for the placeholder to actually resolve to cleartext on runtime. */
  async _openVaultPicker(onPick) {
    let items = [];
    let vaultStatus = null;
    try {
      vaultStatus = await api("/api/vault/status");
    } catch (e) {
      toast(`Vault status check failed: ${e.message}`, true);
      return;
    }
    if (vaultStatus && vaultStatus.locked) {
      toast("Vault is locked. Open Vault page and unlock it first.", true);
      return;
    }
    try {
      const r = await api("/api/vault/items");
      items = r.items || [];
    } catch (e) {
      toast(`Vault items load failed: ${e.message}`, true);
      return;
    }
    if (!items.length) {
      toast("Vault is empty. Open the Vault page and add an account.");
      return;
    }
    document.querySelectorAll(".gs-vault-picker-modal").forEach(m => m.remove());
    const modal = document.createElement("div");
    modal.className = "profile-modal gs-vault-picker-modal";
    modal.innerHTML = `
      <div class="profile-modal-backdrop" data-cancel></div>
      <div class="profile-modal-content" style="width:520px">
        <div class="profile-modal-header">
          <div class="profile-modal-title">\u{1F511}  Insert from Credential Vault</div>
          <button class="profile-modal-close" data-cancel>\u2715</button>
        </div>
        <div class="profile-modal-body">
          <div class="picker-help">
            Pick an item, then choose which field to insert. The result
            is a placeholder like <code>{vault.42.username}</code> that
            gets resolved to cleartext at run-launch time. The
            subprocess never sees the master password -- the dashboard
            decrypts referenced items just-in-time and passes the
            resolved values via env.
          </div>
          <input type="text" class="input picker-search"
                 placeholder="Search items\u2026" id="vault-pick-search">
          <div class="picker-list" id="vault-pick-list"></div>
        </div>
        <div class="profile-modal-footer">
          <button class="btn btn-secondary" data-cancel>Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    const list = modal.querySelector("#vault-pick-list");
    const search = modal.querySelector("#vault-pick-search");

    const render = (filter) => {
      const f = (filter || "").toLowerCase();
      list.innerHTML = items
        .filter(it => !f || (it.name || "").toLowerCase().includes(f) ||
                            (it.service || "").toLowerCase().includes(f))
        .map(it => {
          const hasTotp = it.has_totp || (it.kind || "").includes("totp");
          return `<div class="vault-pick-item" data-id="${it.id}">
            <div class="vault-pick-name">
              <strong>${escapeHtml(it.name || "(no name)")}</strong>
              ${it.service ? `<span class="muted"> -- ${escapeHtml(it.service)}</span>` : ""}
            </div>
            <div class="vault-pick-fields">
              <button class="btn btn-tiny" data-field="username">username</button>
              <button class="btn btn-tiny" data-field="password">password</button>
              ${hasTotp ? `<button class="btn btn-tiny" data-field="totp_code">2FA code</button>` : ""}
            </div>
          </div>`;
        }).join("") || `<div class="library-empty">No matches.</div>`;
      list.querySelectorAll(".vault-pick-item").forEach(row => {
        const iid = row.dataset.id;
        row.querySelectorAll("button[data-field]").forEach(btn => {
          btn.addEventListener("click", () => {
            const field = btn.dataset.field;
            modal.remove();
            onPick(`{vault.${iid}.${field}}`);
          });
        });
      });
    };
    render("");
    search.addEventListener("input", () => render(search.value));
    modal.querySelectorAll("[data-cancel]").forEach(b =>
      b.addEventListener("click", () => modal.remove()));
    setTimeout(() => search.focus(), 30);
  },

  /** Toggle a step's inline-edit accordion. Mutates a transient flag
   *  on the step object (`_inlineOpen`) and re-renders the canvas. */
  _toggleInlineEdit(path) {
    const step = this._getAt(path);
    if (!step) return;
    step._inlineOpen = !step._inlineOpen;
    // No _markDirty here -- _inlineOpen is UI state, not script content
    this.renderFlow();
  },

  _iconFor(meta) {
    const type = (meta.type || "").toLowerCase();
    const map = {
      search_query: "🔎", catch_ads: "🎣", pause: "⏸", dwell: "⏳",
      rotate_ip: "🔄", refresh: "↻", click_ad: "🖱", click_selector: "🎯",
      visit: "🌐", visit_url: "🌐", new_tab: "🆕", close_tab: "✕",
      switch_tab: "⇆", back: "◀", read: "📖", hover: "👉",
      scroll: "📜", scroll_to_bottom: "⬇", type: "⌨", press_key: "⌨",
      fill_form: "📝", wait_for: "⏱", wait_for_url: "🧭",
      loop: "🔁", foreach: "🔁", foreach_ad: "🎯",
      if: "⎇", break: "🚫", continue: "↪",
      extract_text: "📋", save_var: "💾", http_request: "📡",
      move_random: "🖱", random_delay: "⏳", open_url: "🌐",
    };
    return map[type] || "·";
  },

  // Build <option> tags for the extension picker. Empty option first
  // so the user can clear the selection (then fall back to extension_name).
  _renderExtensionOptions(currentValue) {
    const pool = this._extPoolCache || [];
    const cur = String(currentValue || "");
    const escapeHtml = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const opts = [
      `<option value="" ${cur === "" ? "selected" : ""}>— pick an extension —</option>`,
    ];
    for (const x of pool) {
      const id = x.id || "";
      const label = `${x.name || "(unnamed)"}${x.version ? "  v" + x.version : ""}`;
      opts.push(
        `<option value="${escapeHtml(id)}" ${cur === id ? "selected" : ""}>${escapeHtml(label)}</option>`
      );
    }
    if (cur && !pool.some(x => x.id === cur)) {
      // Preserve a value that points at an extension no longer in
      // the pool (so we don't silently drop it on save).
      opts.push(`<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (missing)</option>`);
    }
    return opts.join("");
  },
};

const Scripts = ScriptsPage;
