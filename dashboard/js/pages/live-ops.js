// ═══════════════════════════════════════════════════════════════
// pages/live-ops.js — Realtime active-run dashboard
// ═══════════════════════════════════════════════════════════════
//
// Polls /api/runs/live every 2 seconds. Renders one card per active
// run with: profile name, status badge (healthy/warn/dead/attention),
// duration, current query + current ad domain, exit IP + country,
// ads/captchas/queries counters, and the last 5 log lines.
//
// teardown() must clear the interval — without it, navigating away
// from this page keeps polling forever.

const LiveOps = {
  // Polling interval. 2s is fast enough to feel realtime, slow enough
  // not to hammer the dashboard when 20+ runs are active.
  POLL_MS: 2000,
  pollHandle: null,
  lastFetchAt: null,

  async init() {
    // Initial paint: render once immediately, then every POLL_MS.
    await this.tick();
    this.pollHandle = setInterval(() => this.tick(), this.POLL_MS);

    // "as of" relative-time label updates every 1s independently of
    // the actual fetch — so the user sees the timer move smoothly
    // even if a fetch takes 800ms.
    this._asOfHandle = setInterval(() => this._refreshAsOfLabel(), 1000);
  },

  teardown() {
    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
    if (this._asOfHandle) {
      clearInterval(this._asOfHandle);
      this._asOfHandle = null;
    }
  },

  async tick() {
    let payload;
    try {
      payload = await api("/api/runs/live");
    } catch (e) {
      // Network/server error — show banner once, keep polling silently.
      console.warn("[live-ops] fetch failed:", e);
      return;
    }
    this.lastFetchAt = Date.now();
    this._render(payload);
  },

  _render(payload) {
    const grid = document.getElementById("live-ops-grid");
    const empty = document.getElementById("live-ops-empty");
    const countPill = document.getElementById("live-ops-count-pill");
    if (!grid) return;

    const runs = (payload && payload.runs) || [];
    countPill.textContent =
      runs.length === 1 ? "1 running" : `${runs.length} running`;

    if (runs.length === 0) {
      grid.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";

    // Render. Diff-aware would be nice but innerHTML is simple and
    // fast enough at <100 cards. Cards have stable IDs anyway in
    // case we ever want to morph instead of replace.
    grid.innerHTML = runs.map(r => this._renderCard(r)).join("");
  },

  _renderCard(r) {
    // Status: color + label
    let statusClass = "healthy";
    let statusLabel = "running";
    if (r.needs_attention) {
      statusClass = "attention";
      statusLabel = "needs attention";
    } else if (r.healthy === false) {
      // Heartbeat stale or missing
      const age = r.heartbeat_age == null ? "?" : `${r.heartbeat_age}s`;
      statusClass = "warn";
      statusLabel = `stale heartbeat (${age})`;
    }

    const dur = LiveOps._formatDuration(r.duration_sec);
    const country = r.country || "—";
    const ip = r.exit_ip || "?";
    const flag = LiveOps._countryFlag(country);

    // Recent log lines — show last 5, newest at the bottom.
    // Levels rendered as small dots colored by severity.
    const recent = (r.recent_lines || [])
      .map(l => {
        const lvl = (l.level || "info").toLowerCase();
        const dotClass = lvl === "error" || lvl === "warning" ? lvl : "info";
        const ts = escapeHtml(l.ts || "");
        const msg = escapeHtml((l.message || "").slice(0, 200));
        return `
          <div class="live-ops-log-line">
            <span class="live-ops-log-dot ${dotClass}"></span>
            <span class="live-ops-log-ts">${ts}</span>
            <span class="live-ops-log-msg">${msg}</span>
          </div>`;
      })
      .join("");

    const profileLink =
      `#profile?name=${encodeURIComponent(r.profile_name || "")}`;

    // Current activity row — query / ad / nothing
    let activity = "";
    if (r.current_ad_domain) {
      activity = `
        <div class="live-ops-activity">
          <span class="live-ops-activity-label">clicking</span>
          <span class="live-ops-activity-value">${escapeHtml(r.current_ad_domain)}</span>
        </div>`;
    } else if (r.current_query) {
      activity = `
        <div class="live-ops-activity">
          <span class="live-ops-activity-label">searching</span>
          <span class="live-ops-activity-value">${escapeHtml(r.current_query)}</span>
        </div>`;
    } else {
      activity = `
        <div class="live-ops-activity muted">
          <span class="live-ops-activity-label">init</span>
          <span class="live-ops-activity-value">starting up…</span>
        </div>`;
    }

    return `
      <div class="live-ops-card live-ops-card-${statusClass}"
           data-run-id="${r.run_id || ""}"
           role="article"
           aria-label="Run ${r.run_id || '?'} for profile ${escapeHtml(r.profile_name || 'unknown')}">
        <div class="live-ops-card-header">
          <div class="live-ops-card-title">
            <a href="${profileLink}"
               data-nav="profile"
               data-nav-arg="name=${encodeURIComponent(r.profile_name || "")}"
               class="live-ops-card-name">
              ${escapeHtml(r.profile_name || "(unknown)")}
            </a>
            <span class="live-ops-card-runid">#${r.run_id || "?"}</span>
          </div>
          <div class="live-ops-card-status live-ops-status-${statusClass}">
            <span class="live-ops-status-dot"></span>
            ${escapeHtml(statusLabel)}
          </div>
        </div>

        ${r.needs_attention && r.needs_attention_reason ? `
          <div class="live-ops-attention-banner"
               title="${escapeHtml(r.needs_attention_reason)}">
            ⚠ ${escapeHtml(r.needs_attention_reason).slice(0, 120)}
          </div>` : ""}

        ${activity}

        <div class="live-ops-meta">
          <div class="live-ops-meta-cell" title="Time elapsed since run started">
            <span class="live-ops-meta-label">duration</span>
            <span class="live-ops-meta-value">${dur}</span>
          </div>
          <div class="live-ops-meta-cell" title="${escapeHtml(r.org || ip)}">
            <span class="live-ops-meta-label">exit IP</span>
            <span class="live-ops-meta-value">
              <span class="live-ops-flag">${flag}</span>
              ${escapeHtml(ip)}
            </span>
          </div>
          <div class="live-ops-meta-cell">
            <span class="live-ops-meta-label">queries</span>
            <span class="live-ops-meta-value">${r.total_queries || 0}</span>
          </div>
          <div class="live-ops-meta-cell">
            <span class="live-ops-meta-label">ads</span>
            <span class="live-ops-meta-value">${r.total_ads || 0}</span>
          </div>
          <div class="live-ops-meta-cell ${(r.total_captchas || 0) > 0 ? 'critical' : ''}">
            <span class="live-ops-meta-label">captchas</span>
            <span class="live-ops-meta-value">${r.total_captchas || 0}</span>
          </div>
        </div>

        <div class="live-ops-recent-label">recent</div>
        <div class="live-ops-recent">
          ${recent || '<div class="muted" style="font-size:12px;">no log lines yet</div>'}
        </div>
      </div>`;
  },

  _refreshAsOfLabel() {
    const el = document.getElementById("live-ops-as-of");
    if (!el || !this.lastFetchAt) return;
    const ageMs = Date.now() - this.lastFetchAt;
    const age = Math.max(0, Math.round(ageMs / 1000));
    el.textContent = age <= 1 ? "updated just now" : `updated ${age}s ago`;
  },

  _formatDuration(sec) {
    if (sec == null) return "—";
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m < 60) return `${m}m ${r}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  },

  // Map common country names → flag emoji. Falls back to a globe.
  // Adding new countries here is one-line — no codepoint-math needed
  // because we rely on a small explicit map; cleaner for the
  // small set of countries actually seen in proxy diagnostics.
  _countryFlag(country) {
    if (!country) return "🌐";
    const map = {
      "Ukraine":         "🇺🇦",
      "United States":   "🇺🇸",
      "Germany":         "🇩🇪",
      "Netherlands":     "🇳🇱",
      "United Kingdom":  "🇬🇧",
      "Poland":          "🇵🇱",
      "France":          "🇫🇷",
      "Italy":           "🇮🇹",
      "Spain":           "🇪🇸",
      "Russia":          "🇷🇺",
      "Belarus":         "🇧🇾",
      "Kazakhstan":      "🇰🇿",
      "Turkey":          "🇹🇷",
      "Romania":         "🇷🇴",
      "Czech Republic":  "🇨🇿",
      "Czechia":         "🇨🇿",
    };
    return map[country] || "🌐";
  },
};
