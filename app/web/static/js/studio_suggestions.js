// Admin moderation queue for authoring-studio suggestions.
// Lists suggestions by status and wires approve/reject to the admin endpoints.

const $ = (id) => document.getElementById(id);
let currentStatus = "pending";

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const b = await r.json();
      if (b && b.detail) detail = JSON.stringify(b.detail);
    } catch (_) { /* non-JSON */ }
    throw new Error(detail);
  }
  if (r.status === 204 || r.headers.get("content-length") === "0") return null;
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtTime(t) {
  if (!t) return "";
  try { return new Date(t).toLocaleString(); } catch (_) { return String(t); }
}

function card(s) {
  const el = document.createElement("div");
  el.className = "sug-card";
  const payload = esc(JSON.stringify(s.payload, null, 2));
  let actions = "";
  if (s.status === "pending") {
    actions = `
      <div class="sug-actions">
        <button class="btn btn-primary" data-act="approve" data-id="${esc(s.id)}">Approve</button>
        <button class="btn btn-secondary" data-act="reject" data-id="${esc(s.id)}">Reject</button>
      </div>`;
  } else {
    actions = `<div class="sug-status">${esc(s.status)}${s.resolved_by ? " by " + esc(s.resolved_by) : ""}</div>`;
  }
  el.innerHTML = `
    <div class="sug-head">
      <span class="sug-domain">${esc(s.domain)}</span>
      <span class="sug-meta">${esc(s.created_by || "")} · ${esc(fmtTime(s.created_at))}</span>
    </div>
    <div class="sug-payload">${payload}</div>
    ${actions}`;
  return el;
}

async function load() {
  const list = $("sug-list");
  list.innerHTML = "";
  let rows = [];
  try {
    rows = await api(`/api/admin/authoring-suggestions?status=${encodeURIComponent(currentStatus)}`);
  } catch (e) {
    list.innerHTML = `<div class="sug-empty">Failed to load: ${esc(e.message)}</div>`;
    return;
  }
  if (!rows.length) {
    list.innerHTML = `<div class="sug-empty">No ${esc(currentStatus)} suggestions.</div>`;
    return;
  }
  for (const s of rows) list.appendChild(card(s));
}

async function resolve(id, act) {
  try {
    await api(`/api/admin/authoring-suggestions/${encodeURIComponent(id)}/${act}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (window.appToast) window.appToast(`${act === "approve" ? "Approved" : "Rejected"}: ${id}`);
    load();
  } catch (e) {
    if (window.appToast) window.appToast(`Failed: ${e.message}`);
  }
}

function init() {
  document.querySelectorAll(".sug-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".sug-tab").forEach((t) => t.setAttribute("aria-selected", "false"));
      tab.setAttribute("aria-selected", "true");
      currentStatus = tab.dataset.status;
      load();
    });
  });
  $("sug-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (btn) resolve(btn.dataset.id, btn.dataset.act);
  });
  const runBtn = $("sug-run-mining");
  if (runBtn) {
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      try {
        const r = await api("/api/admin/memory-mining/run", { method: "POST", body: JSON.stringify({}) });
        if (window.appToast) {
          window.appToast(`Mining run: ${r.created.length} candidate(s), ${r.skipped_pii} skipped (PII)`);
        }
        currentStatus = "pending";
        document.querySelectorAll(".sug-tab").forEach((t) =>
          t.setAttribute("aria-selected", t.dataset.status === "pending" ? "true" : "false"));
        load();
      } catch (e) {
        if (window.appToast) window.appToast(`Mining failed: ${e.message}`);
      } finally {
        runBtn.disabled = false;
      }
    });
  }
  load();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
