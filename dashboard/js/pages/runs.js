// ═══════════════════════════════════════════════════════════════
// pages/runs.js — history + filters + pagination + clear/mark-failed
// ═══════════════════════════════════════════════════════════════
//
// Apr 2026 redesign: server returns up to 500 recent runs; the page
// filters and paginates client-side. That keeps the API trivial and
// gives the user instant filter response without a round-trip per
// keystroke. The server still respects ?status / ?profile_name /
// ?since_hours when other callers (CLI, scripts) want filtered lists,
// but this page stays simple and asks for "the last N rows, plain".

const Runs = {
  // -------------- internal state --------------
  _all:    [],   // last fetched rows (unfiltered)
  _view:   [],   // filtered subset currently being paged through
  _page:   1,
  _pageSize: 50,
  _fetchLimit: 500,
  // Re-entrancy guard so a click on Reload while still loading doesn't
  // race two parallel fetches and overwrite each other.
  _loading: false,

  async init() {
    $("#reload-runs-btn").addEventListener("click", () => this.reload());
    $("#btn-clear-runs").addEventListener("click", () => this.clearRuns());

    // Filter handlers — re-render on every change without re-fetching
    const onFilterChange = () => { this._page = 1; this._applyFiltersAndRender(); };
    $("#runs-filter-status").addEventListener("change", onFilterChange);
    $("#runs-filter-profile").addEventListener("change", onFilterChange);
    $("#runs-filter-range").addEventListener("change", onFilterChange);
    $("#runs-filter-search").addEventListener("input", onFilterChange);
    $("#runs-filter-pagesize").addEventListener("change", () => {
      this._pageSize = parseInt($("#runs-filter-pagesize").value, 10) || 50;
      this._page = 1;
      this._applyFiltersAndRender();
    });
    $("#btn-filter-reset").addEventListener("click", () => this._resetFilters());

    // Pagination
    $("#btn-page-first").addEventListener("click", () => this._goToPage(1));
    $("#btn-page-prev").addEventListener("click",
      () => this._goToPage(this._page - 1));
    $("#btn-page-next").addEventListener("click",
      () => this._goToPage(this._page + 1));
    $("#btn-page-last").addEventListener("click",
      () => this._goToPage(Number.MAX_SAFE_INTEGER));

    this._pageSize = parseInt($("#runs-filter-pagesize").value, 10) || 50;
    await this.reload();
  },

  // ---------------- fetch + cache ----------------
  async reload() {
    if (this._loading) return;
    this._loading = true;
    try {
      const runs = await api(`/api/runs?limit=${this._fetchLimit}`);
      this._all = Array.isArray(runs) ? runs : [];
      this._populateProfileFilter(this._all);
      this._applyFiltersAndRender();
    } catch (e) {
      console.error(e);
      const tbody = $("#runs-tbody");
      if (tbody) {
        tbody.innerHTML =
          `<tr><td colspan="9" class="runs-empty-cell">
             Error loading runs: ${escapeHtml(e.message || String(e))}
           </td></tr>`;
      }
    } finally {
      this._loading = false;
    }
  },

  // ---------------- filtering ----------------
  /**
   * Reads UI values, returns the filtered+sorted view.
   * We always sort by started_at desc, but the server already returns
   * in that order so this is mostly a defensive pass.
   */
  _computeView() {
    const status  = $("#runs-filter-status").value;
    const profile = $("#runs-filter-profile").value;
    const rangeH  = parseInt($("#runs-filter-range").value, 10) || 0;
    const search  = ($("#runs-filter-search").value || "").trim().toLowerCase();

    // Time-range cutoff in ms. 0 means no cutoff.
    const cutoffMs = rangeH > 0 ? Date.now() - rangeH * 3600 * 1000 : 0;

    return this._all.filter(r => {
      // Status
      if (status === "ok"      && r.exit_code !== 0) return false;
      if (status === "failed"  && (r.exit_code == null || r.exit_code === 0)) return false;
      if (status === "running" && !(r.exit_code == null && r.finished_at == null)) return false;

      // Profile
      if (profile && r.profile_name !== profile) return false;

      // Time range
      if (cutoffMs && r.started_at) {
        const t = Date.parse(r.started_at);
        if (Number.isFinite(t) && t < cutoffMs) return false;
      }

      // Free-text search — matches run id, profile name, or exit code
      if (search) {
        const hay = [
          `#${r.id}`, String(r.id || ""),
          (r.profile_name || "").toLowerCase(),
          r.exit_code != null ? `exit:${r.exit_code}` : "",
        ].join(" ");
        if (!hay.includes(search)) return false;
      }
      return true;
    });
  },

  _applyFiltersAndRender() {
    this._view = this._computeView();
    this._renderStats();
    this._renderTable();
    this._renderPager();
  },

  _resetFilters() {
    $("#runs-filter-status").value = "all";
    $("#runs-filter-profile").value = "";
    $("#runs-filter-range").value = "0";
    $("#runs-filter-search").value = "";
    $("#runs-filter-pagesize").value = "50";
    this._pageSize = 50;
    this._page = 1;
    this._applyFiltersAndRender();
  },

  _populateProfileFilter(runs) {
    const sel = $("#runs-filter-profile");
    if (!sel) return;
    const prev = sel.value;
    const seen = new Set();
    for (const r of runs) {
      if (r.profile_name) seen.add(r.profile_name);
    }
    const names = Array.from(seen).sort((a, b) =>
      a.localeCompare(b, undefined, { sensitivity: "base" }));
    sel.innerHTML =
      `<option value="">All profiles</option>` +
      names.map(n =>
        `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`
      ).join("");
    // Preserve the user's selection across reloads if still valid.
    if (prev && seen.has(prev)) sel.value = prev;
  },

  // ---------------- pagination ----------------
  _goToPage(p) {
    const total = Math.max(1, Math.ceil(this._view.length / this._pageSize));
    this._page = Math.max(1, Math.min(p, total));
    this._renderTable();
    this._renderPager();
  },

  _renderPager() {
    const pager = $("#runs-pager");
    const info  = $("#runs-pager-info");
    const curr  = $("#runs-pager-curr");
    const totalPages = Math.max(1, Math.ceil(this._view.length / this._pageSize));
    const startIdx = (this._page - 1) * this._pageSize;
    const endIdx   = Math.min(startIdx + this._pageSize, this._view.length);

    if (this._view.length === 0) {
      info.textContent = "No matching runs";
    } else {
      info.textContent =
        `Showing ${startIdx + 1}–${endIdx} of ${this._view.length}` +
        (this._view.length < this._all.length
          ? ` (${this._all.length} loaded)` : "");
    }
    curr.textContent = `${this._page} / ${totalPages}`;

    const prevDisabled = this._page <= 1;
    const nextDisabled = this._page >= totalPages;
    $("#btn-page-first").disabled = prevDisabled;
    $("#btn-page-prev").disabled  = prevDisabled;
    $("#btn-page-next").disabled  = nextDisabled;
    $("#btn-page-last").disabled  = nextDisabled;

    pager.style.display = this._view.length > 0 ? "" : "flex";
  },

  // ---------------- rendering ----------------
  _renderStats() {
    const all = this._all;
    $("#runs-total").textContent   = all.length;
    $("#runs-success").textContent =
      all.filter(r => r.exit_code === 0).length;
    $("#runs-failed").textContent  =
      all.filter(r => r.exit_code != null && r.exit_code !== 0).length;
    const runningEl = $("#runs-running");
    if (runningEl) {
      runningEl.textContent =
        all.filter(r => r.exit_code == null && r.finished_at == null).length;
    }
    const shownEl = $("#runs-shown");
    if (shownEl) shownEl.textContent = this._view.length;
  },

  _renderTable() {
    const tbody = $("#runs-tbody");
    if (!tbody) return;
    if (!this._view.length) {
      const msg = this._all.length
        ? "No runs match the current filters."
        : "No runs yet.";
      tbody.innerHTML =
        `<tr><td colspan="9" class="runs-empty-cell">${msg}</td></tr>`;
      return;
    }
    const start = (this._page - 1) * this._pageSize;
    const slice = this._view.slice(start, start + this._pageSize);
    tbody.innerHTML = slice.map(r => this.renderRow(r)).join("");
  },

  renderRow(r) {
    const started = r.started_at ? r.started_at.replace("T", " ") : "—";
    const duration = fmtDuration(r.started_at, r.finished_at)
      || '<span class="pill pill-running">running</span>';

    let exitBadge;
    if (r.exit_code === 0) {
      exitBadge = '<span class="pill pill-healthy">OK</span>';
    } else if (r.exit_code == null) {
      exitBadge = '<span class="pill pill-idle">—</span>';
    } else {
      exitBadge = `<span class="pill pill-critical">${r.exit_code}</span>`;
    }

    const stuck = r.finished_at == null && r.exit_code == null;

    const actions = [
      `<button class="btn-sm" onclick="Runs.viewLogs(${r.id})">View logs</button>`,
    ];
    if (stuck) {
      actions.push(
        `<button class="btn-sm btn-danger" onclick="Runs.markFailed(${r.id})">Mark failed</button>`
      );
    }

    return `
      <tr>
        <td><strong>#${r.id}</strong></td>
        <td class="muted">${escapeHtml(started)}</td>
        <td>${escapeHtml(r.profile_name || "—")}</td>
        <td>${duration}</td>
        <td>${r.total_queries || 0}</td>
        <td>${r.total_ads || 0}</td>
        <td>${r.captchas || 0}</td>
        <td>${exitBadge}</td>
        <td><div class="btn-group">${actions.join("")}</div></td>
      </tr>
    `;
  },

  // ---------------- actions (clear / view / mark-failed) ----------------
  async clearRuns() {
    const scope = $("#clear-runs-scope").value;
    const isAll = scope === "all";
    const days  = isAll ? null : parseInt(scope, 10);

    const title   = isAll
      ? "🗑 Clear ALL run history?"
      : `🗑 Clear runs older than ${days} days?`;
    const message = isAll
      ? "This permanently removes every run record. You'll lose all " +
        "historical metrics. Profile fingerprints and config are kept.\n\n" +
        "This cannot be undone."
      : `Run records older than ${days} day(s) will be permanently deleted. ` +
        `This frees up DB space but means you won't be able to review them.`;

    if (!await confirmDialog({
      title, message,
      confirmText: "Clear",
      confirmStyle: "danger",
    })) return;

    const btn = $("#btn-clear-runs");
    const labelEl = btn.querySelector(".btn-runs-clear-label");
    const iconEl  = btn.querySelector(".btn-runs-clear-icon");
    btn.disabled = true;
    if (labelEl) labelEl.textContent = "Clearing…";
    if (iconEl)  iconEl.textContent  = "⏳";

    try {
      const r = await api("/api/runs/clear", {
        method: "POST",
        body: JSON.stringify(days == null ? {} : { older_than_days: days }),
      });
      if (r.ok) {
        toast(`✓ Deleted ${r.deleted} run(s)`);
        await this.reload();
      } else {
        toast(r.error || "clear failed", true);
      }
    } catch (e) {
      toast(e.message || "clear failed", true);
    } finally {
      btn.disabled = false;
      if (labelEl) labelEl.textContent = "Clear";
      if (iconEl)  iconEl.textContent  = "🗑";
    }
  },

  async viewLogs(runId) {
    try {
      const logs = await api(`/api/logs/history?run_id=${runId}&limit=500`);
      LOG_BUFFER.length = 0;
      logs.reverse().forEach(l => {
        LOG_BUFFER.push({
          ts:      (l.timestamp || "").substring(11, 19),
          level:   l.level || "info",
          message: l.message || "",
        });
      });
      // Tell the logs page we're in history mode
      window.LOGS_MODE = { type: "history", runId };
      toast(`Loaded ${logs.length} log entries for run #${runId}`);
      navigate("logs");
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },

  async markFailed(runId) {
    const ok = await confirmDialog({
      title: `Mark run #${runId} as failed`,
      message:
        `This sets exit_code=-99 and finished_at=now.\n\n` +
        `Use this to clean up stuck "running" entries when you know ` +
        `the process is dead.`,
      confirmText: "Mark failed",
      confirmStyle: "warning",
    });
    if (!ok) return;
    try {
      await api(`/api/runs/${runId}/mark-failed`, { method: "POST" });
      toast(`✓ Run #${runId} marked as failed`);
      await this.reload();
    } catch (e) {
      toast("Error: " + e.message, true);
    }
  },
};
