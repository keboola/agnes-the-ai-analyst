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

async function createEntity() {
  const result = $("studio-result");
  const payload = {};
  for (const key of CFG.fields) {
    const el = $(`studio-f-${key}`);
    if (el && el.value.trim()) payload[key] = el.value.trim();
  }
  if (!payload.name && !payload.slug) {
    result.textContent = "Fill in the required fields.";
    return;
  }
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
    msg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && msg.value.trim() && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "user_msg", content: msg.value.trim() }));
        appendStream(`\n> ${msg.value.trim()}\n`);
        msg.value = "";
      }
    });
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
