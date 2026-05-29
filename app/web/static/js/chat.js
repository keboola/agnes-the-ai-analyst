// app/web/static/js/chat.js
const $ = (id) => document.getElementById(id);

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();

// --- capability empty-state panel ---------------------------------
// Shown when no chat is open; hides as soon as a session is created
// or opened from the sidebar. Counts (tables, plugins) are loaded
// asynchronously via the existing REST API so we don't block render.
function hideCapabilities() {
  const panel = $("chat-capabilities");
  if (panel) panel.hidden = true;
}
function showCapabilities() {
  const panel = $("chat-capabilities");
  if (panel) panel.hidden = false;
}

/** Set the chat-status banner with a visual tone.
 *  ``kind`` is one of "info" | "ok" | "warn" | "error". CSS maps each
 *  to a colored variant so a "Disconnected." line stands out from a
 *  "Connected." one. Clears any prior class when ``text`` is empty. */
function setStatus(text, kind = "info") {
  const el = $("chat-status");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("is-info", "is-ok", "is-warn", "is-error");
  if (text) el.classList.add(`is-${kind}`);
}

async function loadCapabilityCounts() {
  // Catalog: count tables the caller can see (RBAC pre-filtered server-side).
  try {
    const cat = await api("/api/catalog?json=1").catch(() => null) || await api("/api/catalog");
    const tables = Array.isArray(cat) ? cat : (cat?.tables || cat?.items || []);
    const total = tables.length;
    const bySource = {};
    for (const t of tables) {
      const src = t.source_name || t.source_type || t.source || "unknown";
      bySource[src] = (bySource[src] || 0) + 1;
    }
    const summary = $("cap-data-summary");
    if (summary) {
      summary.textContent = total > 0
        ? `You can query ${total} table${total === 1 ? "" : "s"} across ${Object.keys(bySource).length} data source${Object.keys(bySource).length === 1 ? "" : "s"}.`
        : "No tables in your catalog yet — an admin grants access via /admin/access.";
    }
    const ul = $("cap-data-sources");
    if (ul && total > 0) {
      ul.innerHTML = "";
      for (const [src, n] of Object.entries(bySource)) {
        const li = document.createElement("li");
        li.innerHTML = `<code>${src}</code> — ${n} table${n === 1 ? "" : "s"}`;
        ul.appendChild(li);
      }
    }
  } catch (err) {
    const summary = $("cap-data-summary");
    if (summary) summary.textContent = "Catalog unavailable — try `agnes catalog` once a chat is open.";
  }

  // Marketplace plugins.
  try {
    const mp = await api("/api/marketplaces").catch(() => []);
    const items = Array.isArray(mp) ? mp : (mp?.marketplaces || []);
    const plugins = items.flatMap(m => m.plugins || []);
    const summary = $("cap-marketplace-summary");
    if (summary) {
      summary.textContent = plugins.length > 0
        ? `${plugins.length} plugin${plugins.length === 1 ? "" : "s"} installed across ${items.length} marketplace${items.length === 1 ? "" : "s"}.`
        : "No marketplace plugins installed yet.";
    }
    const ul = $("cap-marketplace-list");
    if (ul && plugins.length > 0) {
      ul.innerHTML = "";
      for (const p of plugins.slice(0, 5)) {
        const li = document.createElement("li");
        li.innerHTML = `<code>${p.name || p.id}</code>${p.tagline ? " — " + p.tagline : ""}`;
        ul.appendChild(li);
      }
      if (plugins.length > 5) {
        const li = document.createElement("li");
        li.textContent = `… and ${plugins.length - 5} more`;
        ul.appendChild(li);
      }
    }
  } catch (err) {
    const summary = $("cap-marketplace-summary");
    if (summary) summary.textContent = "Marketplace info unavailable.";
  }
}

// Suggested-prompt clicks pre-fill the textarea + submit.
function wireSuggestionButtons() {
  document.querySelectorAll(".cloud-chat-cap-suggest").forEach(btn => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.prompt;
      if (!text) return;
      const ta = $("chat-input");
      if (!ta) return;
      ta.value = text;
      ta.focus();
      // Auto-submit on click to keep the empty-state flow fast.
      const form = $("chat-form");
      if (form) form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
    });
  });
}

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function loadSidebar() {
  const list = await api("/api/chat/sessions");
  const ul = $("chat-list");
  ul.innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    // Raw chat_<hex> ids are noise to a human — fall back to "Untitled
    // chat" when the backend hasn't titled the session yet (typical for
    // a session that's empty or where the first user turn didn't seed
    // an auto-title). Real titles still render verbatim.
    li.textContent = s.title || "Untitled chat";
    li.title = s.title || `Untitled · ${s.id}`;
    li.dataset.id = s.id;
    if (s.id === currentChatId) li.classList.add("is-active");
    li.onclick = () => openSession(s.id);
    ul.appendChild(li);
  }
}

/** Toggle the `.is-active` class on the sidebar item matching ``chatId``.
 *  Called from openSession + newChat so the sidebar always reflects the
 *  conversation currently visible in the main panel. Safe to call when
 *  ``chatId`` is null — clears every highlight. */
function markActiveSidebar(chatId) {
  const ul = document.getElementById("chat-list");
  if (!ul) return;
  for (const li of ul.querySelectorAll("li")) {
    li.classList.toggle("is-active", li.dataset.id === chatId);
  }
}

async function newChat() {
  const created = await api("/api/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ surface: "web" }),
  });
  await loadSidebar();
  openSession(created.id, created.ws_url);
}

async function openSession(chatId, wsUrlOverride) {
  if (ws) { ws.close(); ws = null; }
  currentChatId = chatId;
  markActiveSidebar(chatId);
  $("chat-messages").innerHTML = "";
  setStatus("");

  // Hydrate history. Show the capability/intro panel only when this
  // session has no messages yet — otherwise the chat-main area is a
  // blank rectangle and the user has no visual guidance about what
  // they can ask.
  const history = await api(`/api/chat/sessions/${chatId}/messages`);
  if (history.length === 0) {
    showCapabilities();
  } else {
    hideCapabilities();
    for (const m of history) renderMessage(m);
  }

  // Open WS; if no override, mint a fresh ticket via POST
  let wsUrl = wsUrlOverride;
  if (!wsUrl) {
    const created = await api("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ surface: "web", title: null }),
    });
    if (created.id !== chatId) {
      // server returned a deduped session — re-open that one
      currentChatId = created.id;
    }
    wsUrl = created.ws_url;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${wsUrl}`);
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
  ws.onclose = () => { setStatus("Disconnected.", "warn"); };
}

function handleFrame(frame) {
  switch (frame.type) {
    case "ready":
    case "runner_ready":
      setStatus("Connected.", "ok");
      break;
    case "token":
      appendToken(frame.text);
      break;
    case "tool_call":
      renderToolCallStart(frame);
      break;
    case "tool_result":
      renderToolCallEnd(frame);
      break;
    case "assistant_message":
      finalizeAssistantMessage(frame);
      break;
    case "cancelled":
      setStatus(`Cancelled tool: ${frame.tool || ""}`, "warn");
      break;
    case "error":
      setStatus(`Error: ${frame.kind} (${frame.message || ""})`, "error");
      break;
    case "done":
      $("cancel-btn").hidden = true;
      break;
  }
}

function renderMessage(m) {
  const div = document.createElement("div");
  div.className = `msg msg-${m.role}`;
  div.innerHTML = marked.parse(m.content || "");
  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>tool: ${tc.tool}</summary>
        <pre><code>${JSON.stringify(tc.args, null, 2)}</code></pre>`;
      div.appendChild(det);
    }
  }
  $("chat-messages").appendChild(div);
}

let currentAssistantDiv = null;
function appendToken(text) {
  if (!currentAssistantDiv) {
    currentAssistantDiv = document.createElement("div");
    currentAssistantDiv.className = "msg msg-assistant streaming";
    $("chat-messages").appendChild(currentAssistantDiv);
  }
  currentAssistantDiv.textContent += text;
  currentAssistantDiv.scrollIntoView({ block: "end" });
}

function finalizeAssistantMessage(frame) {
  if (currentAssistantDiv) {
    currentAssistantDiv.classList.remove("streaming");
    currentAssistantDiv.innerHTML = marked.parse(frame.content || currentAssistantDiv.textContent);
    currentAssistantDiv = null;
  } else {
    renderMessage({ role: "assistant", content: frame.content, tool_calls: frame.tool_calls });
  }
}

function renderToolCallStart(frame) {
  const det = document.createElement("details");
  det.open = false;
  det.dataset.tool = frame.tool;
  det.innerHTML = `<summary>⏳ tool: ${frame.tool}</summary>
    <pre><code>${JSON.stringify(frame.args, null, 2)}</code></pre>`;
  $("chat-messages").appendChild(det);
  inFlightToolCalls.set(frame.tool, det);
  $("cancel-btn").hidden = false;
}

function renderToolCallEnd(frame) {
  const det = inFlightToolCalls.get(frame.tool);
  if (det) {
    det.querySelector("summary").textContent = `✓ tool: ${frame.tool}`;
    const pre = document.createElement("pre");
    pre.innerHTML = `<code>${JSON.stringify(frame.result, null, 2).slice(0, 4000)}</code>`;
    det.appendChild(pre);
    inFlightToolCalls.delete(frame.tool);
  }
}

// Wait until the WebSocket is ready (auto-create chat if none open).
async function ensureWsReady() {
  if (ws && ws.readyState === 1) return;
  // No active WS — create a new chat session. newChat() opens the WS
  // and resolves when openSession completes, but openSession doesn't
  // currently return a "WS open" promise, so we poll briefly.
  if (!currentChatId) await newChat();
  for (let i = 0; i < 60; i++) {
    if (ws && ws.readyState === 1) return;
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error("WebSocket did not open within 6 s");
}

async function submitUserMessage(text) {
  if (!text) return;
  hideCapabilities();
  try {
    await ensureWsReady();
  } catch (err) {
    setStatus(`Could not start chat: ${err.message}`, "error");
    showCapabilities();
    return;
  }
  renderMessage({ role: "user", content: text });
  ws.send(JSON.stringify({ type: "user_msg", text }));
  $("chat-input").value = "";
}

$("new-chat").onclick = async () => {
  hideCapabilities();
  await newChat();
};

$("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  await submitUserMessage(text);
};

$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

(async () => {
  await loadSidebar();
  // Capability empty-state visible until first chat starts. Loading
  // counts async — page renders immediately, panel fills in.
  wireSuggestionButtons();
  loadCapabilityCounts();
})();
