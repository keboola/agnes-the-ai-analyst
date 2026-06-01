// app/web/static/js/chat.js
const $ = (id) => document.getElementById(id);

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();

// Promise that resolves on the first ``ready`` / ``runner_ready`` frame from
// the server after we open a WebSocket. ``ws.readyState === 1`` (the TCP/HTTP
// handshake) does NOT mean the server-side ``ChatManager.attach`` has finished
// spawning the runner and populated ``live[chat_id]`` — that takes ~5 s for
// E2B sandbox creation. If we send ``user_msg`` during that window the server
// raises ``SessionNotFound``, closes the WS with 4404, and the user sees
// "Disconnected — click the conversation again to resume." with no idea why.
// All ``user_msg`` sends now ``await`` this promise first.
let serverReadyPromise = null;
let resolveServerReady = null;
function resetServerReady() {
  serverReadyPromise = new Promise((r) => { resolveServerReady = r; });
}
resetServerReady();

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

/** Show an ephemeral toast at the bottom-right. ``kind`` of "ok" /
 *  "warn" / "error" tints the chip. Auto-dismisses after 2.4s; can
 *  be dismissed early with a click. Multiple toasts stack. */
function showToast(text, kind = "ok", { durationMs = 2400 } = {}) {
  const stack = $("chat-toasts");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `cloud-chat-toast is-${kind}`;
  // No per-toast role="status" — the parent #chat-toasts already
  // carries aria-live="polite" which announces any appended child.
  // Stacking both was belt-and-suspenders that caused some screen
  // readers to double-announce.
  toast.textContent = text;
  const dismiss = () => {
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 160);
  };
  toast.onclick = dismiss;
  stack.appendChild(toast);
  setTimeout(dismiss, durationMs);
}

/** Set the title strip above the messages area. Pass ``null`` to
 *  hide it (empty-state / new-chat shell), pass a string to show it.
 *  Long titles ellipsis via CSS. */
function setThreadTitle(title) {
  const header = $("chat-thread-header");
  const node = $("chat-thread-title");
  if (!header || !node) return;
  if (title) {
    node.textContent = title;
    header.hidden = false;
  } else {
    header.hidden = true;
  }
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

// In-memory cache of the last sidebar fetch so the Cmd+K palette can
// filter without a round-trip and openSession can resolve titles.
let _sessionsCache = [];

async function loadSidebar() {
  const list = await api("/api/chat/sessions");
  _sessionsCache = list;
  const ul = $("chat-list");
  ul.innerHTML = "";
  // Group by recency before rendering — see _groupSessionsByDate. The
  // groups come back in display order with a label per non-empty
  // bucket; we inject a small section header above each.
  for (const group of _groupSessionsByDate(list)) {
    const header = document.createElement("li");
    header.className = "cloud-chat-list-group-header";
    header.setAttribute("role", "presentation");
    header.textContent = group.label;
    ul.appendChild(header);
    for (const s of group.items) ul.appendChild(_makeSidebarItem(s));
  }
  const empty = $("cloud-chat-empty-state");
  if (empty) empty.hidden = list.length > 0;
  // If the user collapsed the sidebar earlier, re-swap each newly
  // rendered label for its initial. ``applySidebarCollapse`` is a
  // no-op when the persisted state is "expanded", so safe to always
  // call here. Defined later in the file but hoisted by ``function``
  // declaration so the call works at load time.
  if (typeof applySidebarCollapse === "function") {
    applySidebarCollapse(isSidebarCollapsed());
  }
}

/** Single sidebar <li> for a session. Pulled out so the date-group
 *  loop above stays readable.
 *
 *  Keyboard-accessible: ``role="button"`` + ``tabindex="0"`` + Enter
 *  and Space handlers. Without these, Tab skips every conversation
 *  (the `<li onclick>` pattern doesn't put the element in the focus
 *  ring) — a hard a11y bug that left screen-reader and
 *  keyboard-only users unable to open a session. */
function _makeSidebarItem(s) {
  const li = document.createElement("li");
  if (s.id === currentChatId) li.classList.add("is-active");
  li.dataset.id = s.id;
  li.title = s.title || `Untitled · ${s.id}`;
  li.setAttribute("role", "button");
  li.tabIndex = 0;
  li.setAttribute("aria-label", `Open ${s.title || "untitled conversation"}`);
  li.onclick = () => openSession(s.id);
  li.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openSession(s.id);
    }
  });

  const label = document.createElement("span");
  label.className = "cloud-chat-list-label";
  label.textContent = s.title || "Untitled chat";
  li.appendChild(label);

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
  return li;
}

/** Group a flat sessions list into [{label, items}, …] buckets
 *  ordered most-recent-first. Bucket boundaries are local-time
 *  midnight (Today / Yesterday), the current ISO week's Monday
 *  ("Earlier this week"), 30 days ago ("Earlier this month"), and
 *  anything older ("Older").
 *
 *  Buckets with no items are dropped so the sidebar doesn't render
 *  an empty header. Sort within each bucket is by last_message_at
 *  (server-side already sorts most-recent-first across the whole
 *  list; we just preserve that order). */
function _groupSessionsByDate(sessions) {
  const now = new Date();
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  // ISO week — Monday is day 1; getDay() returns 0=Sun … 6=Sat.
  const dow = (startOfWeek.getDay() + 6) % 7;
  startOfWeek.setDate(startOfWeek.getDate() - dow);
  const startOfMonth = new Date(startOfToday);
  startOfMonth.setDate(startOfMonth.getDate() - 30);

  const groups = [
    { label: "Today",              items: [], threshold: startOfToday },
    { label: "Yesterday",          items: [], threshold: startOfYesterday },
    { label: "Earlier this week",  items: [], threshold: startOfWeek },
    { label: "Earlier this month", items: [], threshold: startOfMonth },
    { label: "Older",              items: [], threshold: new Date(0) },
  ];
  for (const s of sessions) {
    const ts = s.last_message_at || s.started_at;
    const d = ts ? new Date(ts) : new Date(0);
    for (const g of groups) {
      if (d >= g.threshold) { g.items.push(s); break; }
    }
  }
  return groups.filter(g => g.items.length > 0);
}

/** Soft-archive a session via DELETE /api/chat/sessions/{id}. If the
 *  caller is currently viewing the session they're deleting, swap them
 *  out to the empty-state shell so the main panel doesn't keep
 *  showing a dead conversation. */
async function deleteSession(chatId) {
  try {
    await api(`/api/chat/sessions/${chatId}`, { method: "DELETE" });
  } catch (err) {
    showToast(`Could not delete: ${err.message}`, "error");
    return;
  }
  await loadSidebar();
  if (currentChatId === chatId) {
    currentChatId = null;
    markActiveSidebar(null);
    if (ws) { ws.close(); ws = null; }
    $("chat-messages").innerHTML = "";
    setStatus("");
    setThreadTitle(null);
    showCapabilities();
  }
  showToast("Conversation deleted", "ok");
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
  // Reset the thread title — a brand-new session has no real title
  // yet, so the empty-state should show the capability panel and not
  // a stale label from the previous conversation.
  setThreadTitle(null);
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
  // Sidebar cache holds the title — look it up so the header reads
  // correctly the moment the session opens, before history hydrates.
  const meta = _sessionsCache.find(s => s.id === chatId);
  setThreadTitle(meta && meta.title ? meta.title : "Untitled chat");
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
    lastAssistantArticle = null;
    lastUserText = "";
    for (const m of history) {
      renderMessage(m);
      if (m.role === "user") lastUserText = m.content || "";
    }
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
  resetServerReady();
  ws = new WebSocket(`${proto}://${location.host}${wsUrl}`);
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
  ws.onclose = () => {
    setStatus("Disconnected — click the conversation again to resume.", "warn");
    // Re-arm so the next openSession starts with an unresolved promise;
    // resolveServerReady is replaced fresh in resetServerReady().
    resetServerReady();
  };
}

function handleFrame(frame) {
  switch (frame.type) {
    case "ready":
    case "runner_ready":
      setStatus("Connected.", "ok");
      // Unblock any in-flight ``submitUserMessage`` that's awaiting the
      // server's confirmation that the runner is alive. Two frames fire
      // (``ready`` once after WS open, ``runner_ready`` after subprocess
      // boot) but the first one is enough — manager.attach has populated
      // self._live by the time ``ready`` goes out.
      if (resolveServerReady) resolveServerReady();
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
    case "session_renamed":
      applySessionRename(frame);
      break;
    case "cancelled":
      setStatus(`Cancelled tool: ${frame.tool || ""}`, "warn");
      $("cancel-btn").hidden = true;
      clearThinkingPlaceholder();
      break;
    case "error":
      setStatus(`Error: ${frame.kind} (${frame.message || ""})`, "error");
      $("cancel-btn").hidden = true;
      clearThinkingPlaceholder();
      break;
    case "done":
      $("cancel-btn").hidden = true;
      break;
  }
}

/** Apply a server-pushed title update for a session — fires when the
 *  Haiku auto-title (or any future inline rename) lands. We update:
 *
 *  - the in-memory sidebar cache so Cmd+K picks up the new title;
 *  - the sidebar <li>'s visible label + aria/title attributes;
 *  - the main-panel thread header, if the renamed session is active.
 *
 *  No-op if the frame is malformed or for a session we don't know
 *  about (e.g. the user already deleted it). */
function applySessionRename(frame) {
  const { chat_id: id, title } = frame || {};
  if (!id || !title) return;
  // Cache update — Cmd+K palette reads from here.
  const cached = _sessionsCache.find(s => s.id === id);
  if (cached) cached.title = title;
  // Live sidebar item.
  const li = document.querySelector(`#chat-list li[data-id="${id}"]`);
  if (li) {
    const label = li.querySelector(".cloud-chat-list-label");
    if (label) {
      // When the sidebar is collapsed, the visible content is the
      // initial — store the new full title in data-full-title so the
      // expand-back round-trip is lossless. Otherwise just paint the
      // new title in directly.
      if (typeof isSidebarCollapsed === "function" && isSidebarCollapsed()) {
        label.dataset.fullTitle = title;
        label.textContent = _firstInitial({ title });
      } else {
        label.textContent = title;
      }
    }
    li.title = title;
    li.setAttribute("aria-label", `Open ${title}`);
    const del = li.querySelector(".cloud-chat-list-del");
    if (del) del.setAttribute("aria-label", `Delete ${title}`);
  }
  // Main-panel header.
  if (id === currentChatId) setThreadTitle(title);
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

// Tracks the most recent user message text so the "↻ Ask again"
// button on the latest assistant turn can re-fire it. Updated by
// submitUserMessage() on every send.
let lastUserText = "";

// Tracks the most recent assistant message so the "Ask again"
// affordance + any other "latest only" UI can be moved as the
// conversation progresses. _markLatestAssistant clears the prior
// .is-latest-assistant marker before applying the new one — CSS
// hides ".msg-regenerate" outside the latest article so we don't
// have to scrub the button from old turns.
let lastAssistantArticle = null;
function _markLatestAssistant(article) {
  if (lastAssistantArticle && lastAssistantArticle !== article) {
    lastAssistantArticle.classList.remove("is-latest-assistant");
  }
  lastAssistantArticle = article;
  if (article) article.classList.add("is-latest-assistant");
}

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
      showToast("Message copied", "ok");
    } catch (err) {
      console.warn("clipboard write failed", err);
      showToast("Couldn't copy to clipboard", "error");
    }
  };
  wrap.appendChild(copy);

  // "↻ Ask again" — only meaningful on assistant turns; CSS keeps
  // it hidden on every assistant message except .is-latest-assistant
  // so the user sees one button at a time at the bottom of the
  // thread (ChatGPT pattern). Re-fires lastUserText via the same
  // submitUserMessage path so streaming, status, and toast logic
  // all run identically.
  if (article.classList.contains("msg-assistant")) {
    const regen = document.createElement("button");
    regen.type = "button";
    regen.className = "msg-regenerate";
    regen.title = "Ask the same question again";
    regen.setAttribute("aria-label", "Ask the same question again");
    regen.innerHTML =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M2 8a6 6 0 1 1 1.76 4.24"/>' +
      '<path d="M2 12V8h4"/>' +
      "</svg>" +
      '<span>Ask again</span>';
    regen.onclick = (e) => {
      e.stopPropagation();
      if (!lastUserText) return;
      submitUserMessage(lastUserText);
    };
    wrap.appendChild(regen);
  }

  bubble.appendChild(wrap);
}

function renderMessage(m) {
  const article = createMessageShell({ role: m.role, createdAt: m.created_at });
  const bubble = article.querySelector(".msg-bubble");
  const body = bubble.querySelector(".msg-body");
  body.innerHTML = marked.parse(m.content || "");
  enhanceCodeBlocks(body);
  enhanceTables(body);

  if (m.tool_calls && m.tool_calls.length) {
    for (const tc of m.tool_calls) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>tool: ${tc.tool}</summary>
        <pre><code>${JSON.stringify(tc.args, null, 2)}</code></pre>`;
      bubble.appendChild(det);
      enhanceCodeBlocks(det);
    }
  }

  attachMessageActions(article, m.content || "");
  $("chat-messages").appendChild(article);
  if (m.role === "assistant") _markLatestAssistant(article);
  maybeMakeCollapsible(article);
  maybeScrollToBottom();
}

// ---------- Result table enhancement -------------------------------------
// marked.parse() produces a vanilla <table> for every markdown table
// the agent writes. We post-process: wrap in a horizontal-scroll
// container so wide tables don't blow up the bubble width, mark the
// table so chat.css applies the sticky-header styling, and add a
// click-to-sort handler on each <th>.
//
// Sort is column-local: clicking cycles between asc / desc, with the
// other <th>s reset. Numeric columns are sorted as numbers (parsed
// from the cell's text); everything else falls back to a
// localeCompare so accented strings sort correctly. aria-sort + a
// visual indicator (↑/↓) mirror the state so screen readers and
// sighted users agree on what's sorted.

function enhanceTables(root) {
  if (!root) return;
  for (const table of root.querySelectorAll("table")) {
    if (table.dataset.tblEnhanced === "1") continue;
    table.dataset.tblEnhanced = "1";
    // ``.ds-table`` is the canonical Agnes table family (sticky header,
    // surface-dim row hover, tabular-nums, --text-xs UPPERCASE header
    // type per system.md). The ``.cloud-chat-table`` class only
    // forwards the sort-arrow + click-to-sort styling on top.
    table.classList.add("ds-table", "cloud-chat-table");

    // Wrap for horizontal scroll on narrow viewports.
    if (!table.parentElement.classList.contains("cloud-chat-table-wrap")) {
      const wrap = document.createElement("div");
      wrap.className = "cloud-chat-table-wrap";
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }

    const thead = table.querySelector("thead");
    const tbody = table.querySelector("tbody");
    if (!thead || !tbody) continue;

    const headers = [...thead.querySelectorAll("th")];
    headers.forEach((th, idx) => {
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.setAttribute("aria-sort", "none");
      const label = th.textContent;
      // Wrap the text + indicator so the indicator stays anchored
      // right while the label can ellipsis if a column is narrow.
      th.innerHTML = `<span class="cloud-chat-th-label">${label}</span>
        <span class="cloud-chat-th-arrow" aria-hidden="true"></span>`;
      const sortRows = () => _sortTableByColumn(table, headers, idx);
      th.addEventListener("click", sortRows);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sortRows();
        }
      });
    });
  }
}

function _sortTableByColumn(table, headers, columnIdx) {
  const tbody = table.querySelector("tbody");
  if (!tbody) return;
  const th = headers[columnIdx];
  const currentDir = th.getAttribute("aria-sort");
  const nextDir = currentDir === "ascending" ? "descending" : "ascending";
  // Reset every other column's state, set this column's.
  headers.forEach(h => h.setAttribute("aria-sort", "none"));
  th.setAttribute("aria-sort", nextDir);

  const rows = [...tbody.querySelectorAll("tr")];
  // Detect numeric column — if every non-empty cell parses as a
  // finite number, sort numerically.
  const cells = rows.map(r => (r.children[columnIdx]?.textContent ?? "").trim());
  const numericVals = cells.map(s => parseFloat(s.replace(/,/g, "")));
  const isNumeric = cells.length > 0 &&
    cells.every((s, i) => s === "" || Number.isFinite(numericVals[i]));
  const cmp = (a, b) => {
    if (isNumeric) {
      const av = parseFloat((a.cellText || "").replace(/,/g, ""));
      const bv = parseFloat((b.cellText || "").replace(/,/g, ""));
      const ax = Number.isFinite(av) ? av : Infinity;
      const bx = Number.isFinite(bv) ? bv : Infinity;
      return ax - bx;
    }
    return (a.cellText || "").localeCompare(b.cellText || "", undefined, { sensitivity: "base" });
  };
  const tagged = rows.map(row => ({
    row,
    cellText: (row.children[columnIdx]?.textContent ?? "").trim(),
  }));
  tagged.sort(cmp);
  if (nextDir === "descending") tagged.reverse();
  // Re-attach in new order — DOM appendChild moves existing nodes.
  for (const { row } of tagged) tbody.appendChild(row);
}

// ---------- Collapsible long messages ------------------------------------
// When an assistant turn renders content taller than COLLAPSE_THRESHOLD
// pixels (typically a long code block or a wide table), we cap the
// body height with a fade-out gradient and surface a "Show more"
// toggle. Keeps the scroll feed scannable; expanded state is per-
// message-element so it doesn't bleed across re-renders.

const COLLAPSE_THRESHOLD_PX = 480;

function maybeMakeCollapsible(article) {
  if (!article) return;
  const body = article.querySelector(".msg-body");
  if (!body) return;
  const bubble = article.querySelector(".msg-bubble");
  if (!bubble) return;
  // Run AFTER the next paint so scrollHeight reflects the rendered
  // content. requestAnimationFrame is enough for marked-rendered
  // markdown which doesn't paint async.
  requestAnimationFrame(() => {
    if (body.scrollHeight <= COLLAPSE_THRESHOLD_PX) return;
    bubble.classList.add("is-collapsible");
    if (bubble.querySelector(".msg-toggle-collapse")) return;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "msg-toggle-collapse";
    toggle.textContent = "Show more";
    toggle.onclick = (e) => {
      e.stopPropagation();
      const expanded = bubble.classList.toggle("is-expanded");
      toggle.textContent = expanded ? "Show less" : "Show more";
    };
    // Insert the toggle before the actions row so the actions sit
    // beneath it visually.
    const actions = bubble.querySelector(".msg-actions");
    if (actions) bubble.insertBefore(toggle, actions);
    else bubble.appendChild(toggle);
  });
}

// ---------- Code-block enhancement ---------------------------------------
// Two improvements baked into the same pass over `<pre><code>` blocks:
//
//   1. syntax highlighting via the already-vendored highlight.js — the
//      <link rel="stylesheet" href="/static/vendor/highlight.min.css">
//      in chat.html ships its CSS, and the bundled JS attaches `hljs`
//      on window. We just call `highlightElement` per block after
//      marked.parse() drops the raw HTML in.
//   2. per-block copy buttons — a tiny `.code-block-copy` ghost
//      button absolutely positioned in the top-right of each <pre>,
//      with hover-reveal so it doesn't compete with the code itself.
//
// Safe to call repeatedly: bails out if the block has already been
// processed (data-cb-enhanced attribute).

function enhanceCodeBlocks(root) {
  if (!root) return;
  for (const code of root.querySelectorAll("pre > code")) {
    const pre = code.parentElement;
    if (!pre || pre.dataset.cbEnhanced === "1") continue;
    pre.dataset.cbEnhanced = "1";
    pre.classList.add("code-block-wrap");

    if (window.hljs) {
      try { window.hljs.highlightElement(code); }
      catch (_) { /* unknown language / corrupted markup — fall through */ }
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-block-copy";
    btn.title = "Copy code";
    btn.setAttribute("aria-label", "Copy code block");
    btn.innerHTML = _COPY_ICON_SVG;
    btn.onclick = async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(code.innerText);
        btn.classList.add("is-copied");
        setTimeout(() => btn.classList.remove("is-copied"), 1400);
        showToast("Code copied", "ok");
      } catch (err) {
        console.warn("clipboard write failed", err);
        showToast("Couldn't copy code", "error");
      }
    };
    pre.appendChild(btn);
  }
}

// ---------- Smart auto-scroll --------------------------------------------
// We only scroll the chat-messages container down on a new token / new
// turn if the user was already near the bottom — otherwise scrolling
// would yank them away from a paragraph they're actively reading
// further up. `SCROLL_STICK_PX` is the slack zone counted as "near
// bottom" (8 lines or so).

const SCROLL_STICK_PX = 120;

function isNearBottom(el) {
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_STICK_PX;
}

function maybeScrollToBottom() {
  const el = $("chat-messages");
  if (!el) return;
  // Capture stickiness BEFORE the next paint. The caller has already
  // appended the new node so scrollHeight has grown; we approximate
  // "was near bottom" by comparing post-append minus the typical
  // bubble height (~80px). Conservative: if uncertain, scroll.
  if (el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_STICK_PX + 200) {
    el.scrollTop = el.scrollHeight;
  }
}

// ---------- "Agnes is thinking…" placeholder -----------------------------
// Rendered the moment the user submits, removed as soon as the first
// server frame (token / tool_call / assistant_message) arrives. Bridges
// the gap between "I sent a message" and "the agent has started".

let thinkingEl = null;

function showThinkingPlaceholder() {
  if (thinkingEl) return;
  thinkingEl = createMessageShell({ role: "assistant" });
  thinkingEl.classList.add("is-thinking");
  const body = thinkingEl.querySelector(".msg-body");
  body.innerHTML =
    '<span class="msg-thinking-dot"></span>' +
    '<span class="msg-thinking-dot"></span>' +
    '<span class="msg-thinking-dot"></span>';
  $("chat-messages").appendChild(thinkingEl);
  maybeScrollToBottom();
}

function clearThinkingPlaceholder() {
  if (!thinkingEl) return;
  thinkingEl.remove();
  thinkingEl = null;
}

// Streaming state — captured per turn so finalize knows what to
// re-render and what raw text to hand the copy button.
let currentAssistantArticle = null;
let currentAssistantBody = null;
let currentAssistantText = "";

function appendToken(text) {
  clearThinkingPlaceholder();
  if (!currentAssistantArticle) {
    currentAssistantArticle = createMessageShell({ role: "assistant" });
    currentAssistantArticle.classList.add("is-streaming");
    currentAssistantBody = currentAssistantArticle.querySelector(".msg-body");
    currentAssistantText = "";
    $("chat-messages").appendChild(currentAssistantArticle);
  }
  currentAssistantText += text;
  currentAssistantBody.textContent = currentAssistantText;
  maybeScrollToBottom();
}

function finalizeAssistantMessage(frame) {
  clearThinkingPlaceholder();
  const content = (frame && frame.content) || currentAssistantText;
  if (currentAssistantArticle && currentAssistantBody) {
    currentAssistantArticle.classList.remove("is-streaming");
    currentAssistantBody.innerHTML = marked.parse(content);
    enhanceCodeBlocks(currentAssistantBody);
    enhanceTables(currentAssistantBody);
    attachMessageActions(currentAssistantArticle, content);
    _markLatestAssistant(currentAssistantArticle);
    maybeMakeCollapsible(currentAssistantArticle);
    currentAssistantArticle = null;
    currentAssistantBody = null;
    currentAssistantText = "";
    maybeScrollToBottom();
  } else {
    renderMessage({
      role: "assistant",
      content,
      tool_calls: frame && frame.tool_calls,
      created_at: new Date().toISOString(),
    });
  }
}

// ---------- Inline tool-call blocks --------------------------------------
// Each tool call renders as a self-contained block in the message stream:
//
//   ┌─ ⏳ run_query ························ args ─┐    while running
//   ├─ ✓ run_query · 1.2s ······························┤    once result arrives
//   │   <result preview — first N rows as a table, or  │
//   │    a short text snippet, or a JSON code block>    │
//   └────────────────────────────────────────────────────┘
//
// Args + full result are always reachable behind "Show args" / "Show
// full result" toggles so power users can dig in. Tabular results
// (the most common — `agnes catalog`, `agnes query`, `agnes describe`)
// get a real <table> preview so the user sees what came back without
// having to expand.
//
// Status icons: ⏳ = running, ✓ = done, ⚠ = error, ⊘ = cancelled. The
// status class on the wrapper tints the left border accordingly so a
// failed tool call is unmistakable at a glance.

const _TOOL_RESULT_PREVIEW_ROWS = 5;
const _TOOL_RESULT_TEXT_PREVIEW_CHARS = 280;

function _toolCallId(frame) {
  // Prefer a unique id (e.g. tool_use_id) so two concurrent calls to
  // the same tool don't share a DOM node; fall back to the tool name.
  return frame.id || frame.tool_use_id || frame.tool;
}

function _summarizeArgs(args) {
  if (args == null) return "";
  if (typeof args === "string") return args.length > 80 ? args.slice(0, 78) + "…" : args;
  if (typeof args !== "object") return String(args);
  const keys = Object.keys(args);
  if (keys.length === 0) return "";
  // Heuristic: prefer the SQL arg if present (run_query, agnes query)
  // — that's what the user actually wants to see. Otherwise show the
  // first scalar value or a "k=v, k=v" sketch.
  if (typeof args.sql === "string") {
    const sql = args.sql.replace(/\s+/g, " ").trim();
    return sql.length > 100 ? sql.slice(0, 98) + "…" : sql;
  }
  if (typeof args.table === "string") return args.table;
  if (typeof args.name === "string") return args.name;
  const parts = [];
  for (const k of keys.slice(0, 3)) {
    const v = args[k];
    if (v == null) continue;
    const text = typeof v === "object" ? JSON.stringify(v) : String(v);
    parts.push(`${k}=${text.length > 30 ? text.slice(0, 28) + "…" : text}`);
  }
  return parts.join(", ");
}

function renderToolCallStart(frame) {
  clearThinkingPlaceholder();
  const wrap = document.createElement("section");
  wrap.className = "cloud-chat-tool is-running";
  wrap.dataset.tool = frame.tool;
  wrap.dataset.startedAt = String(performance.now());

  // Header line — icon + tool name + args summary. Always visible.
  const head = document.createElement("div");
  head.className = "cloud-chat-tool-head";
  const icon = document.createElement("span");
  icon.className = "cloud-chat-tool-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⏳";
  head.appendChild(icon);

  const name = document.createElement("span");
  name.className = "cloud-chat-tool-name";
  name.textContent = frame.tool || "tool";
  head.appendChild(name);

  const summary = document.createElement("span");
  summary.className = "cloud-chat-tool-summary";
  summary.textContent = _summarizeArgs(frame.args);
  head.appendChild(summary);

  const meta = document.createElement("span");
  meta.className = "cloud-chat-tool-meta";
  meta.textContent = "running…";
  head.appendChild(meta);

  wrap.appendChild(head);

  // Args panel — collapsed by default. Surfaced as a small <details>
  // so the noise is one click away when needed.
  if (frame.args && Object.keys(frame.args).length > 0) {
    const argsDet = document.createElement("details");
    argsDet.className = "cloud-chat-tool-args";
    const argsSum = document.createElement("summary");
    argsSum.textContent = "Show args";
    argsDet.appendChild(argsSum);
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = JSON.stringify(frame.args, null, 2);
    pre.appendChild(code);
    argsDet.appendChild(pre);
    wrap.appendChild(argsDet);
    enhanceCodeBlocks(argsDet);
  }

  $("chat-messages").appendChild(wrap);
  inFlightToolCalls.set(_toolCallId(frame), wrap);
  maybeScrollToBottom();
  $("cancel-btn").hidden = false;
}

function renderToolCallEnd(frame) {
  const id = _toolCallId(frame);
  const wrap = inFlightToolCalls.get(id);
  if (!wrap) return;
  inFlightToolCalls.delete(id);

  // Status update — error/cancel surfaced; otherwise success.
  const result = frame.result;
  const isError = _looksLikeToolError(result);
  wrap.classList.remove("is-running");
  wrap.classList.add(isError ? "is-error" : "is-done");
  const icon = wrap.querySelector(".cloud-chat-tool-icon");
  if (icon) icon.textContent = isError ? "⚠" : "✓";

  // Timing meta — "running…" → "1.2s" if we tracked startedAt.
  const meta = wrap.querySelector(".cloud-chat-tool-meta");
  if (meta) {
    const startedAt = parseFloat(wrap.dataset.startedAt || "");
    if (Number.isFinite(startedAt)) {
      const elapsedMs = performance.now() - startedAt;
      meta.textContent = elapsedMs > 1000
        ? `${(elapsedMs / 1000).toFixed(1)}s`
        : `${Math.round(elapsedMs)}ms`;
    } else {
      meta.textContent = isError ? "failed" : "done";
    }
  }

  // Result body — the new bit. Picks a preview shape based on the
  // payload: tabular → mini-table; string → snippet; everything else
  // → JSON code block. Full payload is always reachable via the
  // "Show full result" toggle even if the preview is truncated.
  const body = _renderToolResultPreview(result);
  if (body) wrap.appendChild(body);

  maybeScrollToBottom();
}

/** Heuristic: a stringified tool error coming back from the agent SDK
 *  often starts with "error:" / "Error:" or contains "is_error":true
 *  when it's a JSON object. Best-effort — we just need a signal to
 *  switch the icon. */
function _looksLikeToolError(result) {
  if (result == null) return false;
  if (typeof result === "string") {
    const head = result.trim().slice(0, 12).toLowerCase();
    return head.startsWith("error") || head.startsWith("traceback");
  }
  if (typeof result === "object") {
    if (result.is_error === true) return true;
    if (typeof result.error === "string" && result.error.length > 0) return true;
  }
  return false;
}

/** Build the preview block for a tool result. The CLI tools route
 *  most JSON / table output via agnes which speaks Markdown — so
 *  result strings often contain `|---|---|` table markup that
 *  marked.parse() can render natively. We:
 *
 *  1. attempt to extract a tabular preview from a parsed JSON result
 *     (array of objects, or a {columns, rows} shape);
 *  2. fall back to running ``marked.parse`` over a string result so
 *     embedded Markdown tables get rendered as real <table>s with the
 *     `.ds-table` sort+sticky-header enhancement; and
 *  3. fall back to a JSON code block for everything else.
 *
 *  Returns a DOM element ready to append, or null if the result is
 *  empty.
 */
function _renderToolResultPreview(result) {
  if (result == null || result === "") return null;

  // Already-tabular JSON shapes — render a real <table> preview.
  const table = _coerceToTablePreview(result);
  if (table) return table;

  // String result. Most agnes CLI tool output is Markdown-ish; let
  // marked.parse() try to render it.
  if (typeof result === "string") {
    const wrap = document.createElement("div");
    wrap.className = "cloud-chat-tool-result is-text";

    const preview = result.length > _TOOL_RESULT_TEXT_PREVIEW_CHARS
      ? result.slice(0, _TOOL_RESULT_TEXT_PREVIEW_CHARS) + "…"
      : result;

    const previewBody = document.createElement("div");
    previewBody.className = "cloud-chat-tool-result-preview";
    try {
      previewBody.innerHTML = marked.parse(preview);
      enhanceCodeBlocks(previewBody);
      enhanceTables(previewBody);
    } catch (_) {
      previewBody.textContent = preview;
    }
    wrap.appendChild(previewBody);

    if (result.length > _TOOL_RESULT_TEXT_PREVIEW_CHARS) {
      const det = document.createElement("details");
      det.className = "cloud-chat-tool-result-full";
      const sum = document.createElement("summary");
      sum.textContent = "Show full result";
      det.appendChild(sum);
      const full = document.createElement("div");
      full.className = "cloud-chat-tool-result-full-body";
      try {
        full.innerHTML = marked.parse(result);
        enhanceCodeBlocks(full);
        enhanceTables(full);
      } catch (_) {
        const pre = document.createElement("pre");
        pre.textContent = result;
        full.appendChild(pre);
      }
      det.appendChild(full);
      wrap.appendChild(det);
    }
    return wrap;
  }

  // Everything else — pretty-printed JSON inside a <pre>.
  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-tool-result is-json";
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = JSON.stringify(result, null, 2).slice(0, 4000);
  pre.appendChild(code);
  wrap.appendChild(pre);
  enhanceCodeBlocks(wrap);
  return wrap;
}

/** Try to coerce a tool result into a [{col: val}…] shape and render
 *  the first N rows as a real <table>. Returns null if the result
 *  doesn't look tabular. Recognised shapes:
 *
 *    - ``[{a: 1, b: 2}, {a: 3, b: 4}]``  — array of homogeneous objects
 *    - ``{columns: ["a","b"], rows: [[1,2],[3,4]]}`` — DuckDB-ish
 *    - ``{data: [{...}, {...}]}`` — wrapping envelope used by some tools
 */
function _coerceToTablePreview(result) {
  let rows = null;
  let columns = null;

  if (Array.isArray(result) && result.length > 0 && typeof result[0] === "object" && result[0] !== null) {
    rows = result.map(r => ({ ...r }));
    columns = Object.keys(result[0]);
  } else if (result && typeof result === "object") {
    if (Array.isArray(result.rows) && Array.isArray(result.columns)) {
      columns = result.columns.map(String);
      rows = result.rows.map(r => {
        const obj = {};
        for (let i = 0; i < columns.length; i++) obj[columns[i]] = r[i];
        return obj;
      });
    } else if (Array.isArray(result.data) && result.data.length > 0
               && typeof result.data[0] === "object") {
      rows = result.data.map(r => ({ ...r }));
      columns = Object.keys(result.data[0]);
    }
  }

  if (!rows || !columns || rows.length === 0) return null;

  const total = rows.length;
  const preview = rows.slice(0, _TOOL_RESULT_PREVIEW_ROWS);

  const wrap = document.createElement("div");
  wrap.className = "cloud-chat-tool-result is-table";

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const c of columns) {
    const th = document.createElement("th");
    th.textContent = c;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const r of preview) {
    const tr = document.createElement("tr");
    for (const c of columns) {
      const td = document.createElement("td");
      const v = r[c];
      td.textContent = v == null ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  const tableWrap = document.createElement("div");
  tableWrap.className = "cloud-chat-table-wrap";
  tableWrap.appendChild(table);
  wrap.appendChild(tableWrap);

  // "Show full result" reveals the entire JSON below.
  if (total > preview.length) {
    const meta = document.createElement("p");
    meta.className = "cloud-chat-tool-result-meta";
    meta.textContent = `Showing ${preview.length} of ${total} rows.`;
    wrap.appendChild(meta);
    const det = document.createElement("details");
    det.className = "cloud-chat-tool-result-full";
    const sum = document.createElement("summary");
    sum.textContent = "Show all rows (JSON)";
    det.appendChild(sum);
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = JSON.stringify(rows, null, 2);
    pre.appendChild(code);
    det.appendChild(pre);
    wrap.appendChild(det);
    enhanceCodeBlocks(det);
  }

  enhanceTables(wrap);
  return wrap;
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
  // 1. Clear the composer + hide the dashboard SYNCHRONOUSLY so the user
  //    gets immediate visual feedback that their submit was accepted.
  //    Without this they sit watching their typed "ahoj" + the
  //    capability cards for the ~5 s it takes the runner to boot, then
  //    everything flips at once — feels like the page is frozen.
  hideCapabilities();
  const ta = $("chat-input");
  if (ta) {
    ta.value = "";
    autosizeComposer();
  }

  // 2. Make sure we have an open WS. For a brand-new chat this calls
  //    newChat() -> openSession(), and openSession wipes
  //    ``#chat-messages`` ``innerHTML`` on entry — so we deliberately
  //    DO NOT render the user bubble or the thinking placeholder yet,
  //    or they'd be gone in 50 ms. openSession also re-shows the
  //    dashboard when the (fresh, empty) session has no history; we
  //    hide it again after ensureWsReady so that side effect doesn't
  //    undo step 1.
  try {
    await ensureWsReady();
    hideCapabilities();
  } catch (err) {
    setStatus(`Could not start chat: ${err.message}`, "error");
    showCapabilities();
    return;
  }
  // 3. Now ``#chat-messages`` is stable — render the user bubble and
  //    the thinking placeholder so the user sees their submit landed
  //    and the agent is working on it.
  renderMessage({ role: "user", content: text });
  lastUserText = text;
  showThinkingPlaceholder();
  $("cancel-btn").hidden = false;

  // 4. Wait for the server's ``ready`` frame before sending the first
  //    ``user_msg`` — see ``serverReadyPromise`` definition for why.
  //    After the first ready of a session this promise is already
  //    resolved, so subsequent messages flow through with zero added
  //    latency.
  try {
    await Promise.race([
      serverReadyPromise,
      new Promise((_, rej) => setTimeout(() => rej(new Error("server-ready timeout 30 s")), 30000)),
    ]);
  } catch (err) {
    setStatus(`Runner did not become ready: ${err.message}`, "error");
    clearThinkingPlaceholder();
    return;
  }
  if (!ws || ws.readyState !== 1) {
    setStatus("WebSocket dropped before runner became ready.", "error");
    clearThinkingPlaceholder();
    return;
  }
  ws.send(JSON.stringify({ type: "user_msg", text }));
}

/** Resize the composer textarea to fit its content, capped at 220px
 *  (matches max-height in chat.css). Reset to ``auto`` first so the
 *  scrollHeight calculation isn't dragged down by the last value. */
function autosizeComposer() {
  const ta = $("chat-input");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
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
  } else if (e.key === "Escape") {
    // Esc inside the composer drops focus so the global N hotkey
    // becomes available without yanking the cursor mid-thought.
    e.target.blur();
  }
});
$("chat-input").addEventListener("input", autosizeComposer);

// Global keyboard shortcuts. ``targetIsTypeable`` keeps shortcuts
// inert while the user is typing in any input / textarea /
// contenteditable so a sentence like "no good" doesn't fire 'N'.
function _targetIsTypeable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
}
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (_targetIsTypeable(e.target)) return;
  if (e.key === "n" || e.key === "N") {
    e.preventDefault();
    hideCapabilities();
    newChat();
  } else if (e.key === "/") {
    // Slash focuses the composer — matches Twitter/Discord muscle
    // memory for "start typing". Pre-existing Cmd+K still opens
    // the palette for switching conversations.
    e.preventDefault();
    const ta = $("chat-input");
    if (ta) ta.focus();
  }
});

$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

/** Theme toggle — flips ``data-theme`` on <html> between unset (light)
 *  and "dark", and mirrors the choice into localStorage so refreshes
 *  + the anti-FOUC head script keep the same state. Updates the
 *  ``aria-pressed`` flag on the button so screen readers report the
 *  current state correctly. */
function isDarkTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}
function applyTheme(theme) {
  if (theme === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  try { localStorage.setItem("agnes-theme", theme); }
  catch (_) { /* storage disabled — anti-FOUC just won't fire next time */ }
  const btn = $("chat-theme-toggle");
  if (btn) btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
}
(function wireThemeToggle() {
  const btn = $("chat-theme-toggle");
  if (!btn) return;
  // Sync aria-pressed with whatever the head pre-paint script applied.
  btn.setAttribute("aria-pressed", isDarkTheme() ? "true" : "false");
  btn.addEventListener("click", () => {
    applyTheme(isDarkTheme() ? "light" : "dark");
  });
})();

// ---------- Sidebar mini-mode --------------------------------------------
// Collapse the sidebar to a 56px rail showing only icons + per-row
// initials. State persists via localStorage["agnes-chat-sidebar-
// collapsed"]; the head pre-paint script primes <html data-chat-
// sidebar="mini"> to avoid a flash on reload. On boot we promote that
// transitional signal to a .is-mini class on the shell, and swap each
// sidebar item's label text for the conversation's first character so
// the rail reads as a column of initials.

const _SIDEBAR_KEY = "agnes-chat-sidebar-collapsed";

function _firstInitial(s) {
  const t = (s && (s.title || "")).trim();
  if (t) return t[0].toUpperCase();
  // Fall back to a glyph rather than empty — Untitled chats still
  // need a tap target in the rail.
  return "•";
}

/** Apply mini-mode to the DOM. Idempotent. ``collapsed=true`` swaps
 *  every sidebar item's label for an initial; ``false`` restores the
 *  full title text from ``_sessionsCache`` (or the data-id lookup). */
function applySidebarCollapse(collapsed) {
  const shell = document.querySelector(".cloud-chat-shell");
  if (shell) shell.classList.toggle("is-mini", collapsed);
  document.documentElement.removeAttribute("data-chat-sidebar");

  const toggle = $("chat-sidebar-toggle");
  if (toggle) {
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute(
      "aria-label",
      collapsed ? "Expand sidebar" : "Collapse sidebar",
    );
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }

  // Swap labels for initials (or back). Done in JS rather than CSS
  // because no pure-CSS rule can extract the first character of an
  // arbitrary string. Cached titles are preserved in data-full-title
  // so we can restore them losslessly without re-reading the API.
  const items = document.querySelectorAll("#chat-list li[data-id]");
  for (const li of items) {
    const label = li.querySelector(".cloud-chat-list-label");
    if (!label) continue;
    if (collapsed) {
      if (!label.dataset.fullTitle) label.dataset.fullTitle = label.textContent;
      const cached = _sessionsCache.find(s => s.id === li.dataset.id);
      label.textContent = _firstInitial(cached || { title: label.dataset.fullTitle });
    } else {
      if (label.dataset.fullTitle) {
        label.textContent = label.dataset.fullTitle;
        delete label.dataset.fullTitle;
      }
    }
  }
}

function isSidebarCollapsed() {
  try { return localStorage.getItem(_SIDEBAR_KEY) === "1"; }
  catch (_) { return false; }
}

function setSidebarCollapsed(collapsed) {
  try {
    if (collapsed) localStorage.setItem(_SIDEBAR_KEY, "1");
    else localStorage.removeItem(_SIDEBAR_KEY);
  } catch (_) { /* storage disabled — state survives until reload */ }
  applySidebarCollapse(collapsed);
}

(function wireSidebarToggle() {
  const btn = $("chat-sidebar-toggle");
  if (!btn) return;
  // Apply whatever the pre-paint script primed. The sidebar items
  // aren't in the DOM yet (loadSidebar runs after) — we re-apply
  // there so initials show on first render.
  applySidebarCollapse(isSidebarCollapsed());
  btn.addEventListener("click", () => {
    setSidebarCollapsed(!isSidebarCollapsed());
  });
})();

// ---------- Cmd+K command palette ----------------------------------------
// Fuzzy search over the in-memory sessions cache (_sessionsCache),
// keyboard-driven. Cmd/Ctrl+K toggles open. Type to filter by title,
// arrow keys move the selection, Enter opens, Esc closes. The input
// is empty on each open so the user starts fresh.

const _palette = {
  open: false,
  selected: 0,
  filtered: [],
};

function _renderPaletteResults(q) {
  const ul = $("chat-palette-results");
  if (!ul) return;
  ul.innerHTML = "";
  const needle = q.trim().toLowerCase();
  const matches = _sessionsCache.filter(s => {
    if (!needle) return true;
    const t = (s.title || "Untitled chat").toLowerCase();
    return t.includes(needle) || s.id.toLowerCase().includes(needle);
  });
  _palette.filtered = matches;
  if (matches.length === 0) {
    const empty = document.createElement("li");
    empty.className = "cloud-chat-palette-empty";
    empty.textContent = needle
      ? `No conversation matches "${q}"`
      : "No conversations yet. Hit \"+ New chat\" to start.";
    ul.appendChild(empty);
    return;
  }
  if (_palette.selected >= matches.length) _palette.selected = 0;
  for (let i = 0; i < matches.length; i++) {
    const s = matches[i];
    const li = document.createElement("li");
    if (i === _palette.selected) li.classList.add("is-selected");
    li.dataset.id = s.id;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === _palette.selected ? "true" : "false");

    const title = document.createElement("span");
    title.className = "cloud-chat-palette-title";
    title.textContent = s.title || "Untitled chat";
    li.appendChild(title);

    const meta = document.createElement("span");
    meta.className = "cloud-chat-palette-meta";
    meta.textContent = _palette_relativeTime(s.last_message_at || s.started_at);
    li.appendChild(meta);

    li.onmouseenter = () => {
      _palette.selected = i;
      _refreshPaletteSelection();
    };
    li.onclick = () => _palette_openCurrent();
    ul.appendChild(li);
  }
}

function _refreshPaletteSelection() {
  const ul = $("chat-palette-results");
  if (!ul) return;
  const items = ul.querySelectorAll("li:not(.cloud-chat-palette-empty)");
  items.forEach((li, i) => {
    const on = i === _palette.selected;
    li.classList.toggle("is-selected", on);
    li.setAttribute("aria-selected", on ? "true" : "false");
    if (on) li.scrollIntoView({ block: "nearest" });
  });
}

function _palette_relativeTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)} d ago`;
  return d.toLocaleDateString();
}

function _palette_openCurrent() {
  const s = _palette.filtered[_palette.selected];
  closePalette();
  if (s) openSession(s.id);
}

async function openPalette() {
  if (_palette.open) return;
  // Refresh the sidebar cache lazily — if the user opened Cmd+K very
  // soon after the page loaded, the cache may still be []. We don't
  // block the open, we just kick off a background refresh.
  if (_sessionsCache.length === 0) loadSidebar().catch(() => {});
  _palette.open = true;
  _palette.selected = 0;
  const wrap = $("chat-palette");
  if (wrap) wrap.hidden = false;
  const input = $("chat-palette-input");
  if (input) { input.value = ""; input.focus(); }
  _renderPaletteResults("");
}

function closePalette() {
  if (!_palette.open) return;
  _palette.open = false;
  const wrap = $("chat-palette");
  if (wrap) wrap.hidden = true;
  // Return focus to the composer so the user lands somewhere
  // expected after dismissing.
  const ta = $("chat-input");
  if (ta) ta.focus();
}

(function wirePalette() {
  document.addEventListener("keydown", (e) => {
    const isMod = e.metaKey || e.ctrlKey;
    if (isMod && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (_palette.open) closePalette(); else openPalette();
      return;
    }
    if (!_palette.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closePalette();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      _palette.selected = Math.min(_palette.selected + 1, _palette.filtered.length - 1);
      _refreshPaletteSelection();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      _palette.selected = Math.max(_palette.selected - 1, 0);
      _refreshPaletteSelection();
    } else if (e.key === "Enter") {
      e.preventDefault();
      _palette_openCurrent();
    }
  });
  const input = $("chat-palette-input");
  if (input) {
    input.addEventListener("input", () => {
      _palette.selected = 0;
      _renderPaletteResults(input.value);
    });
  }
  document.querySelectorAll("[data-palette-close]").forEach(el => {
    el.addEventListener("click", closePalette);
  });
})();

(async () => {
  renderCapabilities();
  wireSuggestionButtons();
  autosizeComposer();
  await loadSidebar();
})();
