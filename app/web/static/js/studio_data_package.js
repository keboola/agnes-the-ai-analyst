// Data Package Builder studio (authoring agents Slice 0).
//
// Form-based builder with an embedded assistant panel. The Create action
// calls the existing /api/admin/data-packages endpoint directly (so it works
// — and is testable — independent of the LLM). The assistant panel opens a
// chat session bound to the data-package-builder profile and streams the
// agent's suggestions; if chat is disabled it degrades gracefully.

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
    } catch (_) { /* non-JSON body */ }
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

// --- Create package (LLM-independent) --------------------------------------

async function createPackage() {
  const result = $("studio-result");
  const name = $("dp-name").value.trim();
  const slug = $("dp-slug").value.trim();
  const description = $("dp-description").value.trim();
  if (!name || !slug) {
    result.textContent = "Name and slug are required.";
    return;
  }
  result.textContent = "Creating…";
  try {
    const pkg = await api("/api/admin/data-packages", {
      method: "POST",
      body: JSON.stringify({ name, slug, description }),
    });
    const id = pkg && (pkg.id || pkg.slug) ? (pkg.id || pkg.slug) : slug;
    result.textContent = `Created: ${id}`;
    if (window.appToast) window.appToast(`Data package created: ${id}`);
  } catch (e) {
    result.textContent = `Failed: ${e.message}`;
  }
}

// --- Assistant panel (profiled chat session) -------------------------------

async function openAssistant() {
  try {
    const session = await api("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ surface: "web", profile: window.STUDIO_PROFILE }),
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
  if (createBtn) createBtn.addEventListener("click", createPackage);
  openAssistant();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
