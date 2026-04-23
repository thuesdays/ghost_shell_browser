// ═══════════════════════════════════════════════════════════════
// pages/competitors.js
// ═══════════════════════════════════════════════════════════════

const Competitors = {
  async init() {
    try {
      const data = await api("/api/competitors");

      $("#comp-total").textContent = data.total_records;
      $("#comp-domains").textContent = data.unique_domains;
      $("#badge-competitors").textContent = data.unique_domains;

      // Aggregate actions totals across all domains for summary card
      const by_domain = data.by_domain || [];
      let totalRan = 0, totalSkipped = 0, totalErrored = 0;
      for (const d of by_domain) {
        totalRan     += d.actions_ran     || 0;
        totalSkipped += d.actions_skipped || 0;
        totalErrored += d.actions_errored || 0;
      }
      const actionsEl = $("#comp-actions");
      if (actionsEl) actionsEl.textContent = totalRan;
      const subEl = $("#comp-actions-sub");
      if (subEl) {
        const parts = [];
        if (totalSkipped) parts.push(`${totalSkipped} skipped`);
        if (totalErrored) parts.push(`${totalErrored} errored`);
        subEl.textContent = parts.length ? parts.join(" · ") : "no skipped / errored";
      }

      this.renderByDomain(by_domain);
      this.renderRecent(data.recent || []);
    } catch (e) {
      console.error(e);
    }
  },

  renderByDomain(domains) {
    const tbody = $("#competitors-tbody");
    if (!domains.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No competitors found yet</td></tr>`;
      return;
    }
    tbody.innerHTML = domains.map(d => {
      const mentions = d.mentions ?? d.count ?? 0;
      const ran      = d.actions_ran     || 0;
      const skipd    = d.actions_skipped || 0;
      const err      = d.actions_errored || 0;

      // Mentions — count-chip styled cell. Zero mentions get a muted
      // variant so they don't visually compete with high-count rows.
      const mentionsCell = `
        <span class="count-chip ${mentions ? "" : "zero"}">${mentions}</span>`;

      // Actions cell — bold number + optional skipped/errored sub-line.
      let actionsCell;
      if (ran === 0 && skipd === 0 && err === 0) {
        actionsCell = `<span class="count-chip zero">0</span>`;
      } else {
        const subs = [];
        if (skipd) subs.push(`${skipd} skipped`);
        if (err)   subs.push(`${err} err`);
        actionsCell = `
          <span class="count-chip">${ran}</span>` +
          (subs.length
            ? `<div class="muted" style="font-size: 10.5px; margin-top: 2px;">${subs.join(" · ")}</div>`
            : "");
      }

      const queriesJoined = (d.queries || []).join(" · ");
      // Link to the external domain so users can scan competitor sites
      // directly. target=_blank + rel=noopener for safety.
      const domainHref = `https://${d.domain}`;

      return `
        <tr>
          <td class="domain-cell">
            <a href="${escapeHtml(domainHref)}" target="_blank" rel="noopener">
              ${escapeHtml(d.domain)}
            </a>
          </td>
          <td class="num">${mentionsCell}</td>
          <td class="num">${actionsCell}</td>
          <td class="truncate" title="${escapeHtml(queriesJoined)}">
            ${escapeHtml(queriesJoined)}
          </td>
          <td class="ts-cell">${escapeHtml(d.first_seen || "—")}</td>
          <td class="ts-cell">${escapeHtml(d.last_seen || "—")}</td>
        </tr>`;
    }).join("");
  },

  renderRecent(recent) {
    // Reverse so newest is first
    const rows = recent.slice().reverse();
    $("#recent-badge").textContent = rows.length;

    const tbody = $("#recent-tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">No data</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(r => {
      const url = r.google_click_url || "";
      const urlShort = url.length > 90 ? url.substring(0, 90) + "…" : url;
      return `
        <tr>
          <td class="ts-cell">${escapeHtml(r.timestamp)}</td>
          <td class="truncate" title="${escapeHtml(r.query || "")}">${escapeHtml(r.query || "")}</td>
          <td class="domain-cell">
            <a href="https://${escapeHtml(r.domain)}" target="_blank" rel="noopener">
              ${escapeHtml(r.domain)}
            </a>
          </td>
          <td class="truncate" title="${escapeHtml(url)}">
            <a href="${escapeHtml(url || "#")}" target="_blank" rel="noopener"
               class="muted" style="font-size: 11.5px;">
              ${escapeHtml(urlShort)}
            </a>
          </td>
        </tr>`;
    }).join("");
  },
};
