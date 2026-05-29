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

/** Show an ephemeral toast at the bottom-right. ``kind`` of "ok" /
 *  "warn" / "error" tints the chip. Auto-dismisses after 2.4s; can
 *  be dismissed early with a click. Multiple toasts stack. */
function showToast(text, kind = "ok", { durationMs = 2400 } = {}) {
  const stack = $("chat-toasts");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `cloud-chat-toast is-${kind}`;
  toast.setAttribute("role", "status");
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
    table.classList.add("cloud-chat-table");

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

function renderToolCallStart(frame) {
  clearThinkingPlaceholder();
  const det = document.createElement("details");
  det.open = false;
  det.dataset.tool = frame.tool;
  det.innerHTML = `<summary>⏳ tool: ${frame.tool}</summary>
    <pre><code>${JSON.stringify(frame.args, null, 2)}</code></pre>`;
  $("chat-messages").appendChild(det);
  enhanceCodeBlocks(det);
  inFlightToolCalls.set(frame.tool, det);
  maybeScrollToBottom();
  $("cancel-btn").hidden = false;
}

function renderToolCallEnd(frame) {
  const det = inFlightToolCalls.get(frame.tool);
  if (det) {
    det.querySelector("summary").textContent = `✓ tool: ${frame.tool}`;
    const pre = document.createElement("pre");
    pre.innerHTML = `<code>${JSON.stringify(frame.result, null, 2).slice(0, 4000)}</code>`;
    det.appendChild(pre);
    enhanceCodeBlocks(det);
    inFlightToolCalls.delete(frame.tool);
    maybeScrollToBottom();
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
  lastUserText = text;
  ws.send(JSON.stringify({ type: "user_msg", text }));
  const ta = $("chat-input");
  if (ta) {
    ta.value = "";
    autosizeComposer();
  }
  // Show the thinking placeholder + Stop button immediately so the
  // user sees *something* and can cancel between Send and the first
  // server frame. The "done" / "cancelled" / "error" frames hide
  // the Stop button again.
  showThinkingPlaceholder();
  $("cancel-btn").hidden = false;
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
