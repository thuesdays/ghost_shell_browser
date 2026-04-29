// ═══════════════════════════════════════════════════════════════
// pages/cookie-packs.js — Phase D / FU-3 (Apr 2026)
// Cookie Pool Marketplace UI
// ═══════════════════════════════════════════════════════════════
//
// Loads /api/cookie-packs and renders one card per pack. Cards
// expose Apply (open profile picker → POST /apply) and Delete
// buttons. Header has Export button to capture current state of
// a running profile as a new pack.
//
// Live profile list comes from /api/runs/active so users only see
// runnable targets.

const CookiePacks = (() => {

  let _activePackId = null;     // id of pack being applied (modal state)

  async function init() {
    await reload();
    document.getElementById("cp-reload-btn")
            .addEventListener("click", () => reload());
    document.getElementById("cp-export-btn")
            .addEventListener("click", () => openExportModal());

    // Modal close handlers (universal pattern used elsewhere)
    document.querySelectorAll('[data-close="cp-apply-modal"]').forEach(el => {
      el.addEventListener("click", () => closeModal("cp-apply-modal"));
    });
    document.querySelectorAll('[data-close="cp-export-modal"]').forEach(el => {
      el.addEventListener("click", () => closeModal("cp-export-modal"));
    });
    document.getElementById("cp-apply-confirm-btn")
            .addEventListener("click", applyConfirm);
    document.getElementById("cp-export-confirm-btn")
            .addEventListener("click", exportConfirm);
  }

  async function reload() {
    const grid = document.getElementById("cp-grid");
    if (!grid) return;
    grid.innerHTML = `<div class="empty-state" style="grid-column: 1/-1; padding: 40px; text-align: center;">Loading…</div>`;
    try {
      const r = await api("/api/cookie-packs");
      const packs = r.packs || [];
      if (!packs.length) {
        grid.innerHTML = `
          <div class="empty-state" style="grid-column: 1/-1; padding: 60px; text-align: center;">
            <div style="font-size: 48px; opacity: 0.3; margin-bottom: 12px;">📦</div>
            <div style="font-size: 14px;">No packs yet</div>
            <div class="muted" style="font-size: 12px; margin-top: 6px;">
              Click <strong>Export from running profile</strong> to create
              your first pack.
            </div>
          </div>`;
        return;
      }
      grid.innerHTML = packs.map(renderCard).join("");
      // Wire card buttons
      grid.querySelectorAll(".cp-apply-btn").forEach(btn => {
        btn.addEventListener("click", () => openApplyModal(parseInt(btn.dataset.id)));
      });
      grid.querySelectorAll(".cp-delete-btn").forEach(btn => {
        btn.addEventListener("click", () => deletePack(parseInt(btn.dataset.id)));
      });
    } catch (e) {
      grid.innerHTML = `<div class="empty-state" style="grid-column: 1/-1; padding: 40px; text-align: center; color: var(--critical);">
        Load failed: ${escapeHtml(e?.message || String(e))}
      </div>`;
    }
  }

  function renderCard(p) {
    // Quality tier by captcha_rate — lower = better.
    let qualityCls, qualityLabel;
    const cr = p.captcha_rate || 0;
    if (cr < 0.05) { qualityCls = "excellent"; qualityLabel = "✓ excellent"; }
    else if (cr < 0.15) { qualityCls = "good";      qualityLabel = "good"; }
    else if (cr < 0.30) { qualityCls = "fair";      qualityLabel = "fair"; }
    else                { qualityCls = "poor";      qualityLabel = "poor"; }

    const ageStr = p.age_days
      ? (p.age_days >= 30 ? `${Math.round(p.age_days / 30)}mo` : `${p.age_days}d`)
      : "fresh";
    const domains = (p.domains || []).slice(0, 4);
    const moreDomains = (p.domains || []).length - 4;

    return `
      <div class="cp-card cp-card-${qualityCls}" data-id="${p.id}">
        <div class="cp-card-header">
          <div class="cp-card-title">${escapeHtml(p.label || p.slug || "?")}</div>
          <span class="cp-quality-pill cp-quality-${qualityCls}">${qualityLabel}</span>
        </div>
        <div class="cp-card-meta">
          <span title="Approximate age of source profile">📅 ${ageStr}</span>
          <span title="${cr ? (cr * 100).toFixed(1) + '%' : 'n/a'} captcha rate when this pack was active">🛡 ${cr ? (cr * 100).toFixed(1) + '%' : 'n/a'}</span>
        </div>
        <div class="cp-card-domains">
          ${domains.map(d => `<span class="cp-domain-chip">${escapeHtml(d)}</span>`).join("")}
          ${moreDomains > 0 ? `<span class="cp-domain-chip muted">+${moreDomains}</span>` : ""}
        </div>
        <div class="cp-card-stats">
          <div><span class="muted">Cookies:</span> <strong>${p.cookies_count || 0}</strong></div>
          <div><span class="muted">Storage:</span> <strong>${p.storage_count || 0}</strong></div>
        </div>
        <div class="cp-card-actions">
          <button class="btn btn-primary btn-small cp-apply-btn"
                  data-id="${p.id}">Apply →</button>
          <button class="btn btn-secondary btn-small cp-delete-btn"
                  data-id="${p.id}"
                  title="Delete pack">🗑</button>
        </div>
      </div>`;
  }

  function closeModal(id) {
    const m = document.getElementById(id);
    if (m) m.style.display = "none";
  }

  async function openApplyModal(packId) {
    _activePackId = packId;
    // Load pack details to show in info banner
    const info = document.getElementById("cp-apply-pack-info");
    info.innerHTML = "Loading pack details…";
    try {
      const p = await api(`/api/cookie-packs/${packId}`);
      info.innerHTML = `
        <strong>${escapeHtml(p.label || p.slug)}</strong><br>
        <span class="muted">${p.cookies_count} cookies, ${p.storage_count} storage entries · ${(p.domains || []).join(", ") || "no domains"}</span>
      `;
    } catch (e) {
      info.textContent = "Pack details unavailable";
    }
    // Load active runs
    await _populateProfileSelect("cp-apply-profile-select");
    document.getElementById("cp-apply-modal").style.display = "flex";
  }

  async function applyConfirm() {
    if (_activePackId == null) return;
    const sel = document.getElementById("cp-apply-profile-select");
    const profile = sel.value;
    if (!profile) {
      toast("Pick a running profile first", true);
      return;
    }
    const btn = document.getElementById("cp-apply-confirm-btn");
    btn.disabled = true; btn.textContent = "Applying…";
    try {
      const r = await api(`/api/cookie-packs/${_activePackId}/apply`, {
        method: "POST",
        body:   JSON.stringify({ profile_name: profile }),
      });
      if (r.ok) {
        toast(`✓ Applied: ${r.stats.cookies_set} cookies, ${r.stats.storage_set} storage entries`);
        closeModal("cp-apply-modal");
      } else {
        toast(r.error || "apply failed", true);
      }
    } catch (e) {
      toast("apply failed: " + (e?.message || e), true);
    } finally {
      btn.disabled = false; btn.textContent = "Apply";
    }
  }

  async function openExportModal() {
    await _populateProfileSelect("cp-export-profile-select");
    document.getElementById("cp-export-modal").style.display = "flex";
  }

  async function exportConfirm() {
    const profile = document.getElementById("cp-export-profile-select").value;
    if (!profile) { toast("Pick a running profile", true); return; }
    const label  = document.getElementById("cp-export-label").value.trim();
    const domains = (document.getElementById("cp-export-domains").value || "")
      .split("\n").map(s => s.trim()).filter(Boolean);
    const ageDays = parseInt(document.getElementById("cp-export-age-days").value || "0");
    const btn = document.getElementById("cp-export-confirm-btn");
    btn.disabled = true; btn.textContent = "Exporting…";
    try {
      const r = await api("/api/cookie-packs/export", {
        method: "POST",
        body: JSON.stringify({
          profile_name: profile,
          label:        label || `Pack from ${profile}`,
          domains,
          age_days:     ageDays,
        }),
      });
      if (r.ok) {
        toast(`✓ Exported: ${r.cookies_count} cookies, ${r.storage_count} storage`);
        closeModal("cp-export-modal");
        await reload();
      } else {
        toast(r.error || "export failed", true);
      }
    } catch (e) {
      toast("export failed: " + (e?.message || e), true);
    } finally {
      btn.disabled = false; btn.textContent = "Export";
    }
  }

  async function deletePack(id) {
    if (!confirm("Delete this pack? Cannot be undone.")) return;
    try {
      const r = await api(`/api/cookie-packs/${id}`, { method: "DELETE" });
      if (r.ok) {
        toast("✓ Pack deleted");
        await reload();
      } else {
        toast(r.error || "delete failed", true);
      }
    } catch (e) {
      toast("delete failed: " + (e?.message || e), true);
    }
  }

  async function _populateProfileSelect(selectId) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    sel.innerHTML = `<option value="">(loading…)</option>`;
    try {
      const r = await api("/api/runs/active");
      const runs = r.runs || [];
      if (!runs.length) {
        sel.innerHTML = `<option value="">(no profiles running — start one first)</option>`;
        return;
      }
      sel.innerHTML = `<option value="">— pick a profile —</option>` +
        runs.map(rn => `<option value="${escapeHtml(rn.profile_name)}">${escapeHtml(rn.profile_name)} (run #${rn.run_id})</option>`).join("");
    } catch (e) {
      sel.innerHTML = `<option value="">(load error: ${escapeHtml(e?.message || "?")})</option>`;
    }
  }

  return { init };
})();
