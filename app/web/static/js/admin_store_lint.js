// Admin skill-lint page — "Audit now" + per-row Dismiss.
// The findings list is rendered server-side; this only drives the two
// mutating actions against the admin lint API.

const $ = (id) => document.getElementById(id);

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const body = await r.json();
      if (body && body.detail) detail = JSON.stringify(body.detail);
    } catch (_) { /* non-JSON */ }
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

async function runAudit() {
  const btn = $("lint-audit");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "Auditing…";
  try {
    const res = await api("/api/admin/store/lint-audit", {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    const note = res && res.skipped
      ? "Skipped — a recent audit already ran."
      : `Audited ${res.entities_linted ?? 0} skills, ${res.findings_count ?? 0} findings.`;
    if (window.appToast) window.appToast(note);
    // Reload so the freshly persisted findings render.
    location.reload();
  } catch (e) {
    if (window.appToast) window.appToast(`Audit failed: ${e.message}`);
    btn.disabled = false;
    btn.textContent = "Audit now";
  }
}

async function dismiss(ev) {
  const btn = ev.currentTarget;
  const entityId = btn.dataset.entity;
  const ruleId = btn.dataset.rule;
  btn.disabled = true;
  try {
    await api("/api/admin/store/lint-dismiss", {
      method: "POST",
      body: JSON.stringify({ entity_id: entityId, rule_id: ruleId }),
    });
    // Remove the row; if its card is now empty, remove the card too.
    const row = btn.closest(".lint-row");
    const card = btn.closest(".lint-card");
    if (row) row.remove();
    if (card && !card.querySelector(".lint-row")) card.remove();
    if (window.appToast) window.appToast("Dismissed.");
  } catch (e) {
    btn.disabled = false;
    if (window.appToast) window.appToast(`Dismiss failed: ${e.message}`);
  }
}

function init() {
  const auditBtn = $("lint-audit");
  if (auditBtn) auditBtn.addEventListener("click", runAudit);
  for (const b of document.querySelectorAll(".lint-dismiss")) {
    b.addEventListener("click", dismiss);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
