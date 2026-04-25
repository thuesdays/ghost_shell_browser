// ═══════════════════════════════════════════════════════════════
// pages/behavior.js
// ═══════════════════════════════════════════════════════════════

const Behavior = {
  // Default timing values — must match db.py DEFAULT_CONFIG
  DEFAULTS: {
    "behavior.initial_load_min":     2.0,
    "behavior.initial_load_max":     4.0,
    "behavior.serp_settle_min":      1.5,
    "behavior.serp_settle_max":      3.0,
    "behavior.post_refresh_min":     2.0,
    "behavior.post_refresh_max":     4.0,
    "behavior.post_rotate_min":      2.0,
    "behavior.post_rotate_max":      4.0,
    "behavior.fresh_google_min":     3.0,
    "behavior.fresh_google_max":     5.0,
    "behavior.post_consent_min":     2.0,
    "behavior.post_consent_max":     4.0,
    "behavior.between_queries_min":  6.0,
    "behavior.between_queries_max": 12.0,
    "search.refresh_max_attempts":   4,
    "search.refresh_min_sec":       10,
    "search.refresh_max_sec":       15,
  },

  async init() {
    if (!configCache) await loadConfig();
    bindConfigInputs($("#content"));

    const btn = document.getElementById("reset-timings-btn");
    if (btn) btn.addEventListener("click", () => this.resetToDefaults());

    this._wireTabs();
    this._wireSearch();

    // Restore last-active tab from sessionStorage so refresh keeps you
    // where you were. localStorage would persist across sessions which
    // surprises new users; sessionStorage is the right call.
    try {
      const last = sessionStorage.getItem("gs_behavior_tab");
      if (last) this._activateTab(last);
    } catch {}
  },

  // -- Tab strip ----------------------------------------------
  _wireTabs() {
    document.querySelectorAll(".behavior-tab-btn").forEach(btn => {
      btn.addEventListener("click", () => this._activateTab(btn.dataset.tab));
    });
  },

  _activateTab(name) {
    if (!name) return;
    document.querySelectorAll(".behavior-tab-btn").forEach(b =>
      b.classList.toggle("is-active", b.dataset.tab === name));
    document.querySelectorAll(".behavior-tab-content").forEach(c =>
      c.classList.toggle("is-visible", c.dataset.tab === name));
    try { sessionStorage.setItem("gs_behavior_tab", name); } catch {}
  },

  // -- Search filter -----------------------------------------
  // Greys out timing-row / checkbox-row / form-group items whose
  // label text doesn't match the query. If the active tab has zero
  // matches, auto-switch to the first tab that does.
  _wireSearch() {
    const input = document.getElementById("behavior-search");
    if (!input) return;
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      const tabsWithHits = new Set();

      document.querySelectorAll(
        ".behavior-tab-content .timing-row, " +
        ".behavior-tab-content .checkbox-row, " +
        ".behavior-tab-content .form-group"
      ).forEach(row => {
        const text = row.textContent.toLowerCase();
        const match = !q || text.includes(q);
        row.classList.toggle("is-search-hidden", !match);
        if (match) {
          const tab = row.closest(".behavior-tab-content");
          if (tab) tabsWithHits.add(tab.dataset.tab);
        }
      });

      // Update tab buttons: dim tabs with zero hits.
      document.querySelectorAll(".behavior-tab-btn").forEach(b => {
        const has = !q || tabsWithHits.has(b.dataset.tab);
        b.classList.toggle("is-search-empty", !has);
      });

      // If active tab has no matches but another does, jump to it.
      if (q) {
        const active = document.querySelector(".behavior-tab-btn.is-active")?.dataset.tab;
        if (active && !tabsWithHits.has(active) && tabsWithHits.size) {
          this._activateTab([...tabsWithHits][0]);
        }
      }
    });
  },

  async resetToDefaults() {
    if (!await confirmDialog({
      title: "Reset all timing values?",
      message: "Every delay, wait, and retry value on this page will go " +
        "back to the defaults that Ghost Shell ships with. Your action " +
        "pipelines and naturalness toggles are NOT affected.\n\n" +
        "Proceed?",
      confirmText: "Reset",
      confirmStyle: "danger",
    })) return;

    const btn = document.getElementById("reset-timings-btn");
    btn.disabled = true;
    btn.textContent = "⏳ Resetting…";

    try {
      // Save each default via the config API
      const writes = Object.entries(this.DEFAULTS).map(([k, v]) =>
        api("/api/config", {
          method: "POST",
          body: JSON.stringify({ key: k, value: v }),
        })
      );
      await Promise.all(writes);

      // Reload config cache and re-bind inputs so UI reflects new values
      await loadConfig(true);
      bindConfigInputs($("#content"));
      toast("✓ Timings reset to defaults");
    } catch (e) {
      toast("Reset failed: " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "↺ Reset timings to defaults";
    }
  },
};
