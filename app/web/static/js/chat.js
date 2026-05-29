// app/web/static/js/chat.js
const $ = (id) => document.getElementById(id);

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();

// --- capability empty-state panel ---------------------------------
// Populated from a server-embedded JSON blob
// (``<script type="application/json" id="chat-capabilities-data">``).
// The previous shape fetched ``/api/catalog`` + ``/api/marketplaces``
// from JS, but those URLs were wrong / admin-only, so the panel always
// rendered "Catalog unavailable" / "No plugins" regardless of what the
// caller actually had access to. The server now resolves the RBAC-
// filtered view via ``_chat_capability_snapshot`` in
// ``app/web/router.py``, embeds the result here, and we render
// synchronously — no round-trip, no auth races.

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

function readCapabilitySnapshot() {
  const blob = document.getElementById("chat-capabilities-data");
  if (!blob) return null;
  try {
    return JSON.parse(blob.textContent);
  } catch (err) {
    console.warn("chat-capabilities-data parse failed", err);
    return null;
  }
}

function renderCapabilities() {
  const snap = readCapabilitySnapshot();
  if (!snap) return;

  // --- Data card ---
  const total = snap.tables_total || 0;
  const bySource = snap.tables_by_source || {};
  const sourceCount = Object.keys(bySource).length;
  const dataSummary = $("cap-data-summary");
  if (dataSummary) {
    dataSummary.textContent = total > 0
      ? `You can query ${total} table${total === 1 ? "" : "s"} across ${sourceCount} data source${sourceCount === 1 ? "" : "s"}.`
      : "No tables in your catalog yet — an admin grants access via /admin/access.";
  }
  const dataUl = $("cap-data-sources");
  if (dataUl && total > 0) {
    dataUl.innerHTML = "";
    for (const [src, n] of Object.entries(bySource)) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = src;
      li.appendChild(code);
      li.appendChild(document.createTextNode(` — ${n} table${n === 1 ? "" : "s"}`));
      dataUl.appendChild(li);
    }
  }

  // --- Marketplace card ---
  const plugins = snap.plugins || [];
  const mpCount = snap.marketplace_count || 0;
  const mpSummary = $("cap-marketplace-summary");
  if (mpSummary) {
    mpSummary.textContent = plugins.length > 0
      ? `${plugins.length} plugin${plugins.length === 1 ? "" : "s"} installed across ${mpCount} marketplace${mpCount === 1 ? "" : "s"}.`
      : "No marketplace plugins installed yet.";
  }
  const mpUl = $("cap-marketplace-list");
  if (mpUl && plugins.length > 0) {
    mpUl.innerHTML = "";
    for (const p of plugins.slice(0, 5)) {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = p.name || "?";
      li.appendChild(code);
      if (p.tagline) li.appendChild(document.createTextNode(" — " + p.tagline));
      mpUl.appendChild(li);
    }
    if (plugins.length > 5) {
      const li = document.createElement("li");
      li.textContent = `… and ${plugins.length - 5} more`;
      mpUl.appendChild(li);
    }
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
  // 204 No Content (and any empty 2xx) — DELETE /sessions/{id} returns
  // this. Calling .json() on an empty body throws "unexpected end of
  // data", which is what surfaced as `Could not delete: JSON.parse: …`.
  if (r.status === 204 || r.headers.get("content-length") === "0") return null;
  return r.json();
}

async function loadSidebar() {
  const list = await api("/api/chat/sessions");
  const ul = $("chat-list");
  ul.innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    if (s.id === currentChatId) li.classList.add("is-active");
    li.dataset.id = s.id;
    li.title = s.title || `Untitled · ${s.id}`;
    li.onclick = () => openSession(s.id);

    // Title label — separate span so the delete button doesn't share
    // the click target. Raw ``chat_<hex>`` ids are noise — fall back
    // to "Untitled chat" when the backend hasn't titled the session
    // yet (typical for a session that's empty or where the first
    // user turn didn't seed an auto-title).
    const label = document.createElement("span");
    label.className = "cloud-chat-list-label";
    label.textContent = s.title || "Untitled chat";
    li.appendChild(label);

    // Hover-revealed delete button — stopPropagation so clicking the
    // button doesn't also open the conversation in the main panel.
    const del = document.createElement("button");
    del.type = "button";
    del.className = "cloud-chat-list-del";
    del.setAttribute("aria-label", `Delete ${s.title || "this conversation"}`);
    del.innerHTML = "&times;";
    del.onclick = async (e) => {
      e.stopPropagation();
      await deleteSession(s.id);
    };
    li.appendChild(del);

    ul.appendChild(li);
  }
  const empty = $("cloud-chat-empty-state");
  if (empty) empty.hidden = list.length > 0;
}

/** Soft-archive a session via DELETE /api/chat/sessions/{id}. If the
 *  caller is currently viewing the session they're deleting, swap them
 *  out to the empty-state shell so the main panel doesn't keep
 *  showing a dead conversation. */
async function deleteSession(chatId) {
  try {
    await api(`/api/chat/sessions/${chatId}`, { method: "DELETE" });
  } catch (err) {
    setStatus(`Could not delete: ${err.message}`, "error");
    return;
  }
  await loadSidebar();
  if (currentChatId === chatId) {
    currentChatId = null;
    markActiveSidebar(null);
    if (ws) { ws.close(); ws = null; }
    $("chat-messages").innerHTML = "";
    setStatus("");
    showCapabilities();
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

/** Open (or resume) a chat session.
 *
 * For an existing ``chatId`` we POST ``/sessions/{id}/ticket`` to mint a
 * fresh WS ticket against the SAME session — preserves ``chat_id``,
 * history context, message threading. (``POST /sessions`` creates a NEW
 * session each time, which used to be the path here and caused "click
 * on old chat shows old history but routes new messages to a brand-new
 * session" confusion.)
 */
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
  let history = [];
  try {
    history = await api(`/api/chat/sessions/${chatId}/messages`);
  } catch (err) {
    setStatus(`Could not load history: ${err.message}`, "warn");
  }
  if (history.length === 0) {
    showCapabilities();
  } else {
    hideCapabilities();
    for (const m of history) renderMessage(m);
  }

  // Mint a fresh WS ticket for THIS chat_id (unless caller already has one).
  let wsUrl = wsUrlOverride;
  if (!wsUrl) {
    try {
      const t = await api(`/api/chat/sessions/${chatId}/ticket`, { method: "POST" });
      wsUrl = t.ws_url;
    } catch (err) {
      setStatus(`Could not resume chat: ${err.message}`, "error");
      return;
    }
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${wsUrl}`);
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
  ws.onclose = () => {
    setStatus("Disconnected — click the conversation again to resume.", "warn");
  };
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

// ---------- Bubble + avatar + actions ------------------------------------
// Each turn renders as a <article class="msg msg-<role>"> with an
// avatar, a bubble (body + optional tool details + hover actions row).
// Streaming uses the same shell — appendToken appends to the body,
// finalize re-renders it through marked.parse and attaches the
// actions row (timestamp + copy button) once the content is stable.

function userInitial() {
  const email = document.body.dataset.userEmail || "";
  return (email[0] || "?").toUpperCase();
}

function formatTime(d) {
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** Build the empty bubble shell — avatar + bubble + body, no content
 *  yet. ``createdAt`` accepts an ISO string or a Date; falls back to
 *  ``new Date()`` for live turns. The article is NOT yet attached to
 *  the DOM. */
function createMessageShell({ role, createdAt }) {
  const article = document.createElement("article");
  article.className = `msg msg-${role}`;
  const ts = createdAt
    ? (createdAt instanceof Date ? createdAt : new Date(createdAt))
    : new Date();
  article.dataset.createdAt = ts.toISOString();

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? userInitial() : "A";
  article.appendChild(avatar);

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  const body = document.createElement("div");
  body.className = "msg-body";
  bubble.appendChild(body);
  article.appendChild(bubble);
  return article;
}

const _COPY_ICON_SVG =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">' +
  '<rect x="4.25" y="4.25" width="9" height="9" rx="1.5"/>' +
  '<path d="M2.75 11V3.25C2.75 2.7 3.2 2.25 3.75 2.25H10"/>' +
  "</svg>";

/** Attach (or replace) the actions row on an existing message
 *  article. ``copyText`` is the raw text the copy button writes to
 *  the clipboard — usually the same markdown that built the body. */
function attachMessageActions(article, copyText) {
  const bubble = article.querySelector(".msg-bubble");
  if (!bubble) return;
  const existing = bubble.querySelector(".msg-actions");
  if (existing) existing.remove();

  const wrap = document.createElement("div");
  wrap.className = "msg-actions";

  const ts = article.dataset.createdAt
    ? new Date(article.dataset.createdAt)
    : new Date();
  const time = document.createElement("time");
  time.className = "msg-time";
  time.dateTime = ts.toISOString();
  time.textContent = formatTime(ts);
  time.title = ts.toLocaleString();
  wrap.appendChild(time);

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "msg-copy";
  copy.title = "Copy message";
  copy.setAttribute("aria-label", "Copy message");
  copy.innerHTML = _COPY_ICON_SVG;
  copy.onclick = async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(copyText || "");
      copy.classList.add("is-copied");
      setTimeout(() => copy.classList.remove("is-copied"), 1400);
    } catch (err) {
      console.warn("clipboard write failed", err);
    }
  };
  wrap.appendChild(copy);
  bubble.appendChild(wrap);
}

function renderMessage(m) {
  const article = createMessageShell({ role: m.role, createdAt: m.created_at });
  const bubble = article.querySelector(".msg-bubble");
  const body = bubble.querySelector(".msg-body");
  body.innerHTML = marked.parse(m.content || "");

  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>tool: ${tc.tool}</summary>
        <pre><code>${JSON.stringify(tc.args, null, 2)}</code></pre>`;
      bubble.appendChild(det);
    }
  }

  attachMessageActions(article, m.content || "");
  $("chat-messages").appendChild(article);
}

// Streaming state — captured per turn so finalize knows what to
// re-render and what raw text to hand the copy button.
let currentAssistantArticle = null;
let currentAssistantBody = null;
let currentAssistantText = "";

function appendToken(text) {
  if (!currentAssistantArticle) {
    currentAssistantArticle = createMessageShell({ role: "assistant" });
    currentAssistantArticle.classList.add("is-streaming");
    currentAssistantBody = currentAssistantArticle.querySelector(".msg-body");
    currentAssistantText = "";
    $("chat-messages").appendChild(currentAssistantArticle);
  }
  currentAssistantText += text;
  currentAssistantBody.textContent = currentAssistantText;
  currentAssistantArticle.scrollIntoView({ block: "end" });
}

function finalizeAssistantMessage(frame) {
  const content = (frame && frame.content) || currentAssistantText;
  if (currentAssistantArticle && currentAssistantBody) {
    currentAssistantArticle.classList.remove("is-streaming");
    currentAssistantBody.innerHTML = marked.parse(content);
    attachMessageActions(currentAssistantArticle, content);
    currentAssistantArticle = null;
    currentAssistantBody = null;
    currentAssistantText = "";
  } else {
    renderMessage({
      role: "assistant",
      content,
      tool_calls: frame && frame.tool_calls,
      created_at: new Date().toISOString(),
    });
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

/** Make sure a WebSocket is live, or open one.
 *
 *  - Already open → no-op.
 *  - Have a ``currentChatId`` but no live WS → call ``openSession`` to
 *    re-mint a ticket against the SAME chat (resume after disconnect).
 *  - No current chat at all → create a brand-new one.
 *
 *  After whichever path runs, poll briefly for ``ws.readyState === 1``
 *  before resolving so callers can ``ws.send`` immediately. */
async function ensureWsReady() {
  if (ws && ws.readyState === 1) return;
  if (currentChatId) {
    await openSession(currentChatId);
  } else {
    await newChat();
  }
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

// Enter sends, Shift+Enter inserts a newline. IME composition is left
// alone (``isComposing`` is true while a CJK candidate is open —
// submitting then would eat the user's in-progress input). The textarea
// retains its native newline behavior for Shift+Enter so multi-line
// prompts stay possible.
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("chat-form").dispatchEvent(new SubmitEvent("submit", { cancelable: true }));
  }
});

$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

(async () => {
  renderCapabilities();
  wireSuggestionButtons();
  await loadSidebar();
})();
