// ═══════════════════════════════════════════════════════════════
// pages/logs.js — live SSE buffer + historical viewer with filters
//
// With multi-run, the SSE stream is a merge of many concurrent runs.
// Each log entry carries run_id + profile_name so the user can narrow
// the view to just one profile or one run without losing context.
//
// Filters are AND-combined: profile × level × free-text substring.
// ═══════════════════════════════════════════════════════════════

const Logs = {
  _unsubscribe:     null,
  _filterProfile:   "",
  _filterLevel:     "",
  _filterText:      "",
  _knownProfiles:   new Set(),

  async init() {
    if (this._unsubscribe) { this._unsubscribe(); this._unsubscribe = null; }

    const mode = window.LOGS_MODE || { type: "live" };
    this.applyMode(mode);

    // Seed the filter dropdown from whatever's in the buffer right now.
    this._rescanKnownProfiles();
    this._renderProfileDropdown();
    this.render();

    if (mode.type === "live") {
      this._unsubscribe = onLogEntry((entry) => {
        const pn = entry?.profile_name;
        if (pn && !this._knownProfiles.has(pn)) {
          this._knownProfiles.add(pn);
          this._renderProfileDropdown();
        }
        this.render();
      });
    }

    $("#clear-logs-btn").addEventListener("click", () => {
      LOG_BUFFER.length = 0;
      this._knownProfiles.clear();
      this._renderProfileDropdown();
      this.render();
      toast("✓ Cleared");
    });
    $("#back-to-runs-btn").addEventListener("click", () => navigate("runs"));
    $("#switch-to-live-btn").addEventListener("click", () => {
      window.LOGS_MODE = { type: "live" };
      navigate("logs");
    });

    // Filter wiring
    $("#logs-filter-profile")?.addEventListener("change", (e) => {
      this._filterProfile = e.target.value;
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-level")?.addEventListener("change", (e) => {
      this._filterLevel = e.target.value;
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-text")?.addEventListener("input", (e) => {
      this._filterText = (e.target.value || "").toLowerCase();
      this._updateFilterSummary();
      this.render();
    });
    $("#logs-filter-reset")?.addEventListener("click", () => this._resetFilters());
  },

  applyMode(mode) {
    if (mode.type === "history") {
      $("#logs-title").textContent = `Logs for run #${mode.runId}`;
      $("#logs-subtitle").textContent = "Historical — stored in the database";
      $("#back-to-runs-btn").style.display = "inline-flex";
      $("#switch-to-live-btn").style.display = "inline-flex";
    } else {
      $("#logs-title").textContent = "Live logs";
      $("#logs-subtitle").textContent = "Merged output from all active runs (SSE)";
      $("#back-to-runs-btn").style.display = "none";
      $("#switch-to-live-btn").style.display = "none";
    }
  },

  _rescanKnownProfiles() {
    this._knownProfiles.clear();
    for (const l of LOG_BUFFER) {
      if (l.profile_name) this._knownProfiles.add(l.profile_name);
    }
  },

  _renderProfileDropdown() {
    const sel = $("#logs-filter-profile");
    if (!sel) return;
    const current = sel.value;
    const names = Array.from(this._knownProfiles).sort();
    sel.innerHTML = `<option value="">All profiles</option>` +
      names.map(n => {
        const isSel = n === current ? "selected" : "";
        return `<option value="${escapeHtml(n)}" ${isSel}>${escapeHtml(n)}</option>`;
      }).join("");
  },

  _updateFilterSummary() {
    const bar = $("#logs-filter-summary");
    const chipsEl = $("#logs-filter-chips");
    if (!bar || !chipsEl) return;

    const chips = [];
    if (this._filterProfile) chips.push(`profile: ${escapeHtml(this._filterProfile)}`);
    if (this._filterLevel)   chips.push(`level: ${escapeHtml(this._filterLevel)}`);
    if (this._filterText)    chips.push(`text: "${escapeHtml(this._filterText)}"`);

    if (!chips.length) {
      bar.style.display = "none";
    } else {
      bar.style.display = "";
      chipsEl.innerHTML = chips
        .map(c => `<span class="profile-tag-chip active">${c}</span>`)
        .join(" ");
    }
  },

  _resetFilters() {
    this._filterProfile = "";
    this._filterLevel   = "";
    this._filterText    = "";
    $("#logs-filter-profile").value = "";
    $("#logs-filter-level").value   = "";
    $("#logs-filter-text").value    = "";
    this._updateFilterSummary();
    this.render();
  },

  _passesFilter(l) {
    if (this._filterProfile && l.profile_name !== this._filterProfile) return false;
    if (this._filterLevel   && l.level         !== this._filterLevel)   return false;
    if (this._filterText) {
      const hay = (l.message || "").toLowerCase();
      if (!hay.includes(this._filterText)) return false;
    }
    return true;
  },

  render() {
    const box = $("#logs-box");
    if (!box) return;

    const visible = LOG_BUFFER.filter(l => this._passesFilter(l));

    if (!LOG_BUFFER.length) {
      box.innerHTML = '<div class="muted">No log entries</div>';
      return;
    }
    if (!visible.length) {
      box.innerHTML = `<div class="muted">
        No entries match the current filter (${LOG_BUFFER.length} total in buffer)
      </div>`;
      return;
    }

    // Only show the per-line profile chip when "All profiles" is active
    // AND we've seen >1 profile — otherwise it's noise.
    const showProfileChip = !this._filterProfile && this._knownProfiles.size > 1;

    box.innerHTML = visible.map(l => {
      const pn = l.profile_name;
      const chip = (showProfileChip && pn)
        ? `<span class="log-profile-chip">${escapeHtml(pn)}</span>`
        : "";
      return `<div class="log-line ${l.level || 'info'}">` +
             `<span class="ts">${escapeHtml(l.ts || '')}</span>` +
             chip +
             `<span class="msg">${escapeHtml(l.message || '')}</span>` +
             `</div>`;
    }).join("");
    box.scrollTop = box.scrollHeight;
  },

  teardown() {
    if (this._unsubscribe) {
      this._unsubscribe();
      this._unsubscribe = null;
    }
  },
};
