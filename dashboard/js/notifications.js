// ═══════════════════════════════════════════════════════════════
// notifications.js — global attention drawer (Phase 3, Apr 2026)
// ═══════════════════════════════════════════════════════════════
//
// Wires the sidebar bell button + slide-out drawer to /api/notifications.
// The bell shows a badge with the total count (auto-hidden on 0); the
// drawer lists every item grouped by severity (critical → warning → info).
// Items are clickable: dispatch via item.action ("open_profile",
// "open_proxy", etc.) routes the user to the right page.
//
// Polling: every 30s refreshes the badge silently. Opening the drawer
// triggers an immediate refresh so the user always sees fresh state.

const Notifications = (() => {

  const POLL_MS = 30000;
  let pollHandle = null;
  let lastItems = [];
  // Client-side dismissal set. /api/notifications is a live aggregator
  // (no server-side read state), so once a user clicks an item we hide
  // it locally so the badge actually drops. The Set is keyed by a
  // stable composite of (action, action_arg, title). On the next poll
  // we filter by this set before rendering — items that still match
  // the underlying signal stay hidden until reload, but truly new
  // events (different title or arg) come through fine.
  const dismissed = new Set();
  function _itemKey(it) {
    return `${it.action || ""}|${it.action_arg || ""}|${it.title || ""}`;
  }
  function _recomputeCounts(items) {
    const by = { critical: 0, warning: 0, info: 0 };
    for (const it of items) {
      const sev = it.severity || "info";
      by[sev] = (by[sev] || 0) + 1;
    }
    return { count: items.length, by_severity: by };
  }

  function init() {
    const bell  = document.getElementById("notifications-bell");
    const close = document.getElementById("notifications-close-btn");
    const refresh = document.getElementById("notifications-refresh-btn");
    const backdrop = document.getElementById("notifications-backdrop");

    if (bell)  bell.addEventListener("click", open);
    if (close) close.addEventListener("click", closeDrawer);
    if (refresh) refresh.addEventListener("click", () => fetchNow(true));
    if (backdrop) backdrop.addEventListener("click", closeDrawer);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });

    // Initial fetch + start poll. Don't open drawer — just keep
    // the badge accurate from the start.
    fetchNow(false);
    pollHandle = setInterval(() => fetchNow(false), POLL_MS);
  }

  async function fetchNow(showLoading) {
    if (showLoading) {
      const body = document.getElementById("notifications-drawer-body");
      if (body) body.innerHTML = `
        <div class="empty-state" style="padding:40px 20px;text-align:center;">
          Loading…
        </div>`;
    }
    let payload;
    try {
      payload = await api("/api/notifications");
    } catch (e) {
      console.warn("[notifications] fetch failed:", e);
      return;
    }
    const raw = payload.items || [];
    // Drop anything the user has already clicked through this session.
    lastItems = raw.filter(it => !dismissed.has(_itemKey(it)));
    const counts = _recomputeCounts(lastItems);
    updateBadge(counts.count, counts.by_severity);
    renderDrawer(lastItems);
  }

  function updateBadge(count, bySeverity) {
    const badge = document.getElementById("notifications-bell-badge");
    if (!badge) return;
    if (!count) {
      badge.style.display = "none";
      badge.textContent = "0";
      badge.classList.remove("is-critical", "is-warning");
      return;
    }
    badge.style.display = "";
    badge.textContent = count > 99 ? "99+" : String(count);
    // Tint the badge by worst severity present.
    const sb = bySeverity || {};
    badge.classList.toggle("is-critical", (sb.critical || 0) > 0);
    badge.classList.toggle("is-warning",
      !((sb.critical || 0) > 0) && (sb.warning || 0) > 0);

    // Update drawer-header count too.
    const drawerCount = document.getElementById("notifications-drawer-count");
    if (drawerCount) drawerCount.textContent = String(count);
  }

  function renderDrawer(items) {
    const body = document.getElementById("notifications-drawer-body");
    if (!body) return;
    if (!items.length) {
      body.innerHTML = `
        <div class="empty-state" style="padding:60px 20px;text-align:center;">
          <div style="font-size: 48px; opacity: 0.3; margin-bottom: 12px;">✓</div>
          <div style="font-size: 14px;">All caught up</div>
          <div class="muted" style="font-size: 12px; margin-top: 4px;">
            No attention items right now.
          </div>
        </div>`;
      return;
    }

    body.innerHTML = items.map(it => _renderItem(it)).join("");

    // Wire clicks — each item dispatches to the right page based on
    // item.action / item.action_arg. Idempotent per render.
    body.querySelectorAll(".notification-item").forEach(el => {
      el.addEventListener("click", () => {
        const action = el.dataset.action;
        const arg    = el.dataset.actionArg;
        const key    = el.dataset.itemKey;
        // Mark dismissed so the badge drops immediately and the item
        // doesn't reappear on the next poll. Update local state + UI
        // synchronously before navigating away.
        if (key) {
          dismissed.add(key);
          lastItems = lastItems.filter(it => _itemKey(it) !== key);
          const counts = _recomputeCounts(lastItems);
          updateBadge(counts.count, counts.by_severity);
        }
        _dispatchAction(action, arg);
        closeDrawer();
      });
    });
  }

  function _renderItem(it) {
    const sev = it.severity || "info";
    const created = it.created_at
      ? _relativeTime(it.created_at)
      : "";

    return `
      <div class="notification-item notification-item-${sev}"
           data-action="${escapeHtml(it.action || '')}"
           data-action-arg="${escapeHtml(it.action_arg || '')}"
           data-item-key="${escapeHtml(_itemKey(it))}"
           role="button" tabindex="0">
        <div class="notification-item-icon">
          ${sev === "critical" ? "🔴" : sev === "warning" ? "🟡" : "🔵"}
        </div>
        <div class="notification-item-content">
          <div class="notification-item-title">${escapeHtml(it.title || '')}</div>
          <div class="notification-item-body">${escapeHtml(it.body || '')}</div>
          <div class="notification-item-meta">
            <span class="notification-item-severity">${escapeHtml(sev)}</span>
            <span class="notification-item-time">${escapeHtml(created)}</span>
          </div>
        </div>
        ${it.action ? `<div class="notification-item-arrow">→</div>` : ""}
      </div>`;
  }

  function _dispatchAction(action, arg) {
    // Bug-fix Apr 2026: setting `location.hash` alone does NOT trigger
    // the SPA router — app.js binds `navigate()` to sidebar clicks +
    // [data-nav] element clicks, NOT to hashchange events. Setting
    // hash updates the URL but the page never reloads. The pattern
    // used elsewhere (overview.js:742, profiles.js:801, etc.) is:
    //   1. set location.hash so init() can parse the query string
    //   2. CALL navigate(page) to actually swap the content area
    // We follow that here so notification clicks do navigate.
    if (!action) return;
    let page = null;
    switch (action) {
      case "open_profile":
        location.hash = "#profile?name=" + encodeURIComponent(arg || "");
        page = "profile";
        break;
      case "open_proxy":
        location.hash = "#proxy";
        page = "proxy";
        break;
      case "open_runs":
        location.hash = "#runs";
        page = "runs";
        break;
      case "open_scheduler":
        location.hash = "#scheduler";
        page = "scheduler";
        break;
      default:
        console.warn("[notifications] unknown action:", action);
        return;
    }
    if (page && typeof navigate === "function") {
      navigate(page);
    }
  }

  function open() {
    const drawer = document.getElementById("notifications-drawer");
    const backdrop = document.getElementById("notifications-backdrop");
    if (drawer)   { drawer.classList.add("is-open"); drawer.setAttribute("aria-hidden", "false"); }
    if (backdrop) { backdrop.style.display = ""; }
    // Always refresh on open — ensure current state.
    fetchNow(true);
  }

  function closeDrawer() {
    const drawer = document.getElementById("notifications-drawer");
    const backdrop = document.getElementById("notifications-backdrop");
    if (drawer)   { drawer.classList.remove("is-open"); drawer.setAttribute("aria-hidden", "true"); }
    if (backdrop) { backdrop.style.display = "none"; }
  }

  /** Compact relative time for the meta line (e.g. "5m", "2h", "3d"). */
  function _relativeTime(iso) {
    try {
      const d = new Date(iso);
      const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
      if (sec < 60) return `${sec}s ago`;
      const m = Math.floor(sec / 60);
      if (m < 60) return `${m}m ago`;
      const h = Math.floor(m / 60);
      if (h < 24) return `${h}h ago`;
      return `${Math.floor(h / 24)}d ago`;
    } catch {
      return iso;
    }
  }

  return { init, open, close: closeDrawer, refresh: () => fetchNow(true) };
})();

// Boot when DOM is ready (other modules use immediate init in app.js;
// notifications has its own bell listener so it's safe to init now).
document.addEventListener("DOMContentLoaded", () => {
  try { Notifications.init(); } catch (e) { console.warn(e); }
});
