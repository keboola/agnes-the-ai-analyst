// app/static/js/chat.js
const $ = (id) => document.getElementById(id);

let ws = null;
let currentChatId = null;
let inFlightToolCalls = new Map();

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
    li.textContent = s.title || s.id;
    li.dataset.id = s.id;
    li.onclick = () => openSession(s.id);
    ul.appendChild(li);
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
  $("chat-messages").innerHTML = "";
  $("chat-status").textContent = "";

  // Hydrate history
  const history = await api(`/api/chat/sessions/${chatId}/messages`);
  for (const m of history) renderMessage(m);

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
  ws.onclose = () => { $("chat-status").textContent = "Disconnected."; };
}

function handleFrame(frame) {
  switch (frame.type) {
    case "ready":
    case "runner_ready":
      $("chat-status").textContent = "Connected.";
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
      $("chat-status").textContent = `Cancelled tool: ${frame.tool || ""}`;
      break;
    case "error":
      $("chat-status").textContent = `Error: ${frame.kind} (${frame.message || ""})`;
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

$("new-chat").onclick = newChat;
$("chat-form").onsubmit = (e) => {
  e.preventDefault();
  if (!ws || ws.readyState !== 1) return;
  const text = $("chat-input").value.trim();
  if (!text) return;
  renderMessage({ role: "user", content: text });
  ws.send(JSON.stringify({ type: "user_msg", text }));
  $("chat-input").value = "";
};
$("cancel-btn").onclick = () => ws?.send(JSON.stringify({ type: "cancel" }));

(async () => {
  await loadSidebar();
})();
