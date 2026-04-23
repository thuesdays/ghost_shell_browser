// ═══════════════════════════════════════════════════════════════
// pages/scheduler.js
// ═══════════════════════════════════════════════════════════════

const Scheduler = {
  _pollTimer: null,

  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    // Wire buttons
    $("#sched-start-btn").addEventListener("click", () => this.start());
    $("#sched-stop-btn").addEventListener("click", () => this.stop());
    $("#sched-refresh-btn").addEventListener("click", () => this.refresh());

    await Promise.all([
      this.loadProfiles(),
      this.loadGroups(),
      this.refresh(),
    ]);

    // Poll status every 4s while page is active
    clearInterval(this._pollTimer);
    this._pollTimer = setInterval(() => {
      if (currentPage === "scheduler") {
        this.refresh();
      } else {
        clearInterval(this._pollTimer);
      }
    }, 4000);
  },

  /** Populate the "Run as group" dropdown with every defined group.
   *  Current selection comes from configCache.scheduler.group_id; we
   *  preserve it if the list refreshes (e.g. after group creation). */
  async loadGroups() {
    const sel = document.getElementById("sched-group-select");
    if (!sel) return;
    try {
      const groups = await api("/api/groups");
      const current = getByPath(configCache, "scheduler.group_id");
      sel.innerHTML = `<option value="">— none (cycle profiles instead) —</option>` +
        groups.map(g => {
          const isSel = String(current) === String(g.id) ? "selected" : "";
          return `<option value="${g.id}" ${isSel}>
            ${escapeHtml(g.name)} (${g.member_count} member${g.member_count === 1 ? "" : "s"})
          </option>`;
        }).join("");
    } catch (e) {
      console.warn("Could not load groups for scheduler:", e);
    }
  },

  async loadProfiles() {
    try {
      const profiles = await api("/api/profiles");
      const selected = new Set(configCache?.scheduler?.profile_names || []);

      const list = $("#sched-profiles-list");
      if (!profiles.length) {
        list.innerHTML = '<div class="muted" style="padding: 12px;">No profiles yet — create one on the Profiles page.</div>';
        return;
      }

      list.innerHTML = profiles.map(p => `
        <label style="display: flex; align-items: center; gap: 8px;
                      padding: 8px 12px; background: var(--card-alt);
                      border: 1px solid var(--border); border-radius: 7px;
                      cursor: pointer; font-size: 13px;">
          <input type="checkbox" data-profile="${escapeHtml(p.name)}"
                 ${selected.has(p.name) ? "checked" : ""}
                 style="width: 16px; height: 16px; cursor: pointer;">
          <span><strong>${escapeHtml(p.name)}</strong></span>
          <span class="muted" style="margin-left: auto; font-size: 11px;">
            ${p.status || ""}
          </span>
        </label>
      `).join("");

      // Update selection count + wire change events
      const refreshCount = () => {
        const checked = list.querySelectorAll("input[type=checkbox]:checked");
        $("#sched-profile-count").textContent = checked.length;
      };
      refreshCount();

      list.querySelectorAll("input[type=checkbox]").forEach(cb => {
        cb.addEventListener("change", () => {
          const names = Array.from(
            list.querySelectorAll("input[type=checkbox]:checked")
          ).map(c => c.dataset.profile);
          configCache.scheduler = configCache.scheduler || {};
          configCache.scheduler.profile_names = names;
          scheduleConfigSave();
          refreshCount();
        });
      });
    } catch (e) {
      console.error("loadProfiles:", e);
    }
  },

  async refresh() {
    try {
      const s = await api("/api/scheduler/status");
      this.renderStatus(s);
    } catch (e) {
      console.error("scheduler status:", e);
    }
  },

  renderStatus(s) {
    const running = s.is_running;

    $("#sched-start-btn").style.display = running ? "none" : "inline-flex";
    $("#sched-stop-btn").style.display  = running ? "inline-flex" : "none";

    $("#sched-status-value").textContent = running ? "Running" : "Stopped";
    $("#sched-status-value").style.color = running ? "var(--healthy)" : "var(--text-muted)";

    if (running) {
      const startedAgo = s.started_at
        ? this.relativeTime(s.started_at)
        : "—";
      $("#sched-status-sub").textContent = `since ${startedAgo}`;
    } else {
      $("#sched-status-sub").textContent = "idle";
    }

    $("#sched-runs-today").textContent   = s.runs_today ?? 0;
    $("#sched-runs-target").textContent  = `of ${s.target_runs_per_day ?? '—'}`;

    if (s.next_run_at) {
      const d = new Date(s.next_run_at);
      $("#sched-next-run").textContent = d.toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit",
      });
      $("#sched-next-in").textContent = this.inFutureText(s.next_run_at);
    } else {
      $("#sched-next-run").textContent = "—";
      $("#sched-next-in").textContent  = "—";
    }

    $("#sched-last-profile").textContent = s.last_run_profile || "—";
  },

  relativeTime(iso) {
    try {
      const diff = (new Date() - new Date(iso)) / 1000;
      if (diff < 60)   return `${Math.floor(diff)}s ago`;
      if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
      return `${Math.floor(diff/3600)}h ago`;
    } catch { return iso; }
  },

  inFutureText(iso) {
    try {
      const diff = (new Date(iso) - new Date()) / 1000;
      if (diff < 0)    return "overdue";
      if (diff < 60)   return `in ${Math.floor(diff)}s`;
      if (diff < 3600) return `in ${Math.floor(diff/60)}m`;
      return `in ${Math.floor(diff/3600)}h`;
    } catch { return iso; }
  },

  async start() {
    const btn = $("#sched-start-btn");
    // Guard against double-click: disable immediately, don't wait for
    // refresh() to update the UI — the HTTP round-trip can take 500ms+
    // and users hit the button twice during that window.
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Starting...";
    }
    try {
      await api("/api/scheduler/start", { method: "POST" });
      toast("✓ Scheduler started");
      await this.refresh();
    } catch (e) {
      toast("Error: " + e.message, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        // refresh() toggles display:none when running, so the label
        // reset only shows briefly on start-failure.
        btn.innerHTML = "<span>▶</span> <span>Start scheduler</span>";
      }
    }
  },

  async stop() {
    const ok = await confirmDialog({
      title: "Stop scheduler",
      message: "The scheduler will stop after the current iteration completes. Running browser instances will not be killed.",
      confirmText: "Stop scheduler",
      confirmStyle: "warning",
    });
    if (!ok) return;
    const btn = $("#sched-stop-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Stopping...";
    }
    try {
      await api("/api/scheduler/stop", { method: "POST" });
      toast("✓ Scheduler stopped");
      await this.refresh();
    } catch (e) {
      toast("Error: " + e.message, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = "<span>■</span> <span>Stop scheduler</span>";
      }
    }
  },
};
