// Generic authoring-agent studio (data-package / mcp / marketplace / corporate-memory).
//
// Driven by window.STUDIO = { profile, endpoint, fields }. The Create action
// POSTs the form values to the domain's existing admin endpoint (so it works —
// and is testable — independent of the LLM). The assistant panel opens a chat
// session bound to the domain profile and streams suggestions; if chat is
// disabled it degrades gracefully.

const $ = (id) => document.getElementById(id);
const CFG = window.STUDIO || { profile: "", endpoint: "", fields: [] };

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
  if (r.status === 204 || r.headers.get("content-length") === "0") return null;
  return r.json();
}

function appendStream(text) {
  const el = $("studio-stream");
  if (!el) return;
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

function collectPayload() {
  const payload = {};
  for (const key of CFG.fields) {
    const el = $(`studio-f-${key}`);
    if (el && el.value.trim()) payload[key] = el.value.trim();
  }
  return payload;
}

// Guards double-submit and tracks the lint gate for lintable domains: the
// first click runs an advisory dry-run and flips the button to "Publish
// anyway"; the second click publishes for real. Findings never block.
let inFlight = false;
let lintPassed = false;

function renderLint(report) {
  const panel = $("studio-lint");
  const list = $("studio-lint-list");
  if (!panel || !list) return;
  list.innerHTML = "";
  const findings = (report && report.findings) || [];
  if (findings.length === 0) {
    const li = document.createElement("li");
    li.className = "st-lint-clean";
    li.textContent = "No advisory findings — looks good.";
    list.appendChild(li);
  } else {
    for (const f of findings) {
      const li = document.createElement("li");
      li.className = "st-lint-row";
      const sev = document.createElement("span");
      sev.className = "st-lint-sev";
      sev.textContent = f.severity;
      const msg = document.createElement("span");
      msg.className = "st-lint-msg";
      msg.textContent = f.message;
      li.appendChild(sev);
      li.appendChild(msg);
      if (f.doc_url) {
        const a = document.createElement("a");
        a.className = "st-lint-doc";
        a.href = f.doc_url;
        a.textContent = "Guideline →";
        li.appendChild(a);
      }
      list.appendChild(li);
    }
  }
  panel.hidden = false;
}

async function createEntity() {
  const result = $("studio-result");
  const btn = $("studio-create");
  if (inFlight) return;
  const payload = collectPayload();
  if (!payload.name && !payload.slug) {
    result.textContent = "Fill in the required fields.";
    return;
  }
  // Admins create directly; direct-submit domains (the store has its own
  // review pipeline) publish directly for everyone; otherwise non-admins
  // go to the moderation queue.
  if (!CFG.isAdmin && !CFG.submitDirect) return submitSuggestion(payload);

  // Lintable domains (skill): first click = advisory dry-run, second =
  // publish. The dry-run never blocks; it just surfaces findings.
  if (CFG.lintable && !lintPassed) {
    inFlight = true;
    result.textContent = "Checking…";
    try {
      const preview = await api(CFG.endpoint, {
        method: "POST",
        body: JSON.stringify({ ...payload, dry_run: true }),
      });
      renderLint(preview && preview.lint);
      result.textContent = "Reviewed — publish when ready.";
    } catch (e) {
      // Advisory-only: a failed pre-check must never block publishing. Let the
      // next click go straight to the real publish rather than re-checking.
      result.textContent = `Advisory check unavailable — publish anyway. (${e.message})`;
    } finally {
      // Flip regardless of dry-run outcome so publish is never gated on the
      // advisory pre-check succeeding.
      lintPassed = true;
      if (btn) btn.textContent = "Publish anyway";
      inFlight = false;
    }
    return;
  }

  inFlight = true;
  result.textContent = "Creating…";
  try {
    const created = await api(CFG.endpoint, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const id =
      created && (created.id || created.slug)
        ? created.id || created.slug
        : payload.slug || payload.name;
    result.textContent = `Created: ${id}`;
    if (window.appToast) window.appToast(`Created: ${id}`);
  } catch (e) {
    result.textContent = `Failed: ${e.message}`;
  } finally {
    inFlight = false;
  }
}

async function submitSuggestion(payload) {
  const result = $("studio-result");
  result.textContent = "Submitting…";
  try {
    const r = await api("/api/studio/suggestions", {
      method: "POST",
      body: JSON.stringify({ domain: CFG.domain, payload }),
    });
    result.textContent = `Submitted for approval: ${r.id}`;
    if (window.appToast) window.appToast("Submitted for admin approval");
  } catch (e) {
    result.textContent = `Failed: ${e.message}`;
  }
}

async function openAssistant() {
  try {
    const session = await api("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ surface: "web", profile: CFG.profile }),
    });
    const ws = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") + location.host + session.ws_url,
    );
    ws.onmessage = (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch (_) { return; }
      if (frame.type === "token") appendStream(frame.text || "");
      else if (frame.type === "assistant_message") appendStream("\n");
      else if (frame.type === "error") appendStream(`\n[error] ${frame.message || ""}\n`);
    };
    const msg = $("studio-msg");
    const send = () => {
      const v = msg.value.trim();
      if (v && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "user_msg", text: v }));
        appendStream(`\n> ${v}\n`);
        msg.value = "";
      }
    };
    msg.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
    });
    const sendBtn = $("studio-send");
    if (sendBtn) sendBtn.addEventListener("click", send);
  } catch (e) {
    appendStream(`Assistant unavailable: ${e.message}\n`);
  }
}

function init() {
  const createBtn = $("studio-create");
  if (createBtn) createBtn.addEventListener("click", createEntity);
  openAssistant();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
