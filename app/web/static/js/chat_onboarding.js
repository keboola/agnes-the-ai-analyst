// chat_onboarding.js — chat-driven onboarding layer.
//
// The onboarding is driven BY the chat itself (not a separate wizard or a
// spotlight tour): Agnes greets once, and when a first question lands against
// an empty Stack she recommends the packages that would answer it, subscribes
// them on the user's say-so, then resumes the original question — all inside
// the conversation. A small "Your Journey" panel in the sidebar tracks the
// five progress steps and persists to `/api/chat/journey` (per-user, both
// backends).
//
// This module owns its own DOM (greeting bubbles, gap-resolver card, journey
// panel) so it needs only a few well-defined hooks from chat.js:
//   • renderAssistant(markdown)  — append an Agnes bubble to the thread
//   • appendNode(el)             — append a raw node to #chat-messages
//   • resubmit(text)             — re-enter submitUserMessage with `text`
//   • scrollToBottom()           — keep the newest node in view

const STEP_KEYS = [
  "first_asked",
  "stack_setup_done",
  "explored_stack",
  "catalog_discovered",
  "use_anywhere",
];

const DEFAULT_JOURNEY = {
  first_asked: false,
  stack_setup_done: false,
  explored_stack: false,
  catalog_discovered: false,
  use_anywhere: false,
  onboarded: false,
  successful_answers: 0,
};

const STEP_META = {
  first_asked: {
    label: "Ask your first question",
    why: "Start from your real goal so Agnes can shape onboarding around what you need.",
  },
  stack_setup_done: {
    label: "Set up your Stack",
    why: "Agnes needs the right company knowledge in your Stack to answer usefully.",
  },
  explored_stack: {
    label: "Explore My Stack",
    why: "My Stack shows what Agnes can already use and what you added.",
    href: "/stack",
  },
  catalog_discovered: {
    label: "Discover more knowledge",
    why: "The Catalog is where you add optional packages for new questions.",
    href: "/stack?tab=browse",
  },
  use_anywhere: {
    label: "Use Agnes from other AI tools",
    why: "Reuse the same trusted company context in Claude Code, Cursor, and VS Code.",
    href: "/setup",
  },
};

let journey = { ...DEFAULT_JOURNEY };
let hooks = {};
let ready = false;

async function apiJson(path, init) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function loadJourney() {
  try {
    const data = await apiJson("/api/chat/journey");
    journey = { ...DEFAULT_JOURNEY, ...data };
  } catch (_) {
    journey = { ...DEFAULT_JOURNEY };
  }
}

// Persist a partial update. Optimistically merge + re-render first so the UI
// feels instant; the PUT is fire-and-forget (onboarding progress is soft
// state — a dropped write just re-nudges next time).
async function patchJourney(fields) {
  journey = { ...journey, ...fields };
  renderJourneyPanel();
  try {
    await apiJson("/api/chat/journey", {
      method: "PUT",
      body: JSON.stringify(fields),
    });
  } catch (_) {
    /* soft state — ignore */
  }
}

// ── Stack helpers ─────────────────────────────────────────────────────────
// Only DATA_PACKAGE + MEMORY_DOMAIN are subscribable (see app/api/stack.py).
const STACK_TYPES = [
  { type: "data_package", label: "data" },
  { type: "memory_domain", label: "memory" },
];

async function browseStack() {
  const out = [];
  for (const { type } of STACK_TYPES) {
    try {
      const data = await apiJson(`/api/stack/browse?type=${encodeURIComponent(type)}`);
      for (const item of data.items || []) out.push({ ...item, resource_type: type });
    } catch (_) {
      /* type may be unavailable for this instance — skip */
    }
  }
  return out;
}

async function subscribe(resourceType, resourceId) {
  await apiJson("/api/stack/subscribe", {
    method: "POST",
    body: JSON.stringify({ resource_type: resourceType, resource_id: resourceId }),
  });
}

function inStackCount(items) {
  return items.filter((i) => i.in_stack).length;
}

// ── Journey panel ───────────────────────────────────────────────────────────
function renderJourneyPanel() {
  const el = document.getElementById("chat-journey");
  if (!el) return;
  if (!ready) {
    el.hidden = true;
    return;
  }
  el.hidden = false;

  const steps = STEP_KEYS.map((k) => ({ k, done: !!journey[k], ...STEP_META[k] }));
  const nextIdx = steps.findIndex((s) => !s.done);
  const complete = nextIdx === -1;

  const rows = steps
    .map((s, i) => {
      const unlocked = complete || i <= nextIdx;
      const cls = s.done ? "done" : unlocked ? "active" : "locked";
      const mark = s.done ? "✓" : unlocked ? "→" : "•";
      const attrs = s.href ? ` data-journey-go="${s.href}"` : "";
      return `<button type="button" class="cloud-chat-journey-step ${cls}"${attrs} title="${escapeAttr(s.why)}">
        <span class="cloud-chat-journey-mark">${mark}</span>
        <span class="cloud-chat-journey-label">${escapeHtml(s.label)}</span>
      </button>`;
    })
    .join("");

  el.innerHTML = `
    <div class="cloud-chat-journey-head">
      <h3>Your Journey</h3>
      ${complete ? '<span class="cloud-chat-journey-badge">Complete ✓</span>' : ""}
      <button type="button" class="cloud-chat-journey-restart" data-journey-restart
              title="Replay Agnes's welcome" aria-label="Restart onboarding">?</button>
    </div>
    <p class="cloud-chat-journey-sub">Learn what Agnes is and make it yours.</p>
    <div class="cloud-chat-journey-list">${rows}</div>`;

  el.querySelectorAll("[data-journey-go]").forEach((btn) => {
    btn.addEventListener("click", () => {
      // Locked steps are shown for context but must not be navigable —
      // clicking one would jump ahead and mark it done out of order.
      if (btn.classList.contains("locked")) return;
      const href = btn.getAttribute("data-journey-go");
      // Mark the step the destination represents so returning shows progress.
      if (href === "/stack") patchJourney({ explored_stack: true });
      else if (href.startsWith("/stack?tab=browse")) patchJourney({ catalog_discovered: true });
      else if (href === "/setup") patchJourney({ use_anywhere: true });
      window.location.href = href;
    });
  });

  const restartBtn = el.querySelector("[data-journey-restart]");
  if (restartBtn) restartBtn.addEventListener("click", restartOnboarding);
}

// ── Greeting ────────────────────────────────────────────────────────────────
function greetOnce(synced) {
  if (journey.onboarded) return;
  hooks.renderAssistant(
    "Hi, I'm **Agnes** 👋 I'll answer using the company knowledge in your Stack, and I'll always say where an answer came from.",
  );
  if (synced === false) {
    hooks.renderAssistant(
      "Heads up — I can't reach Claude Code or Cursor until you connect a machine, but I can still answer you right here.",
    );
  }
  patchJourney({ onboarded: true });
}

// Replay onboarding on demand — the "?" in the journey head clears the
// one-time greeting guard so greetOnce() shows Agnes's welcome again and
// re-renders the panel. Earned step progress is preserved.
function restartOnboarding() {
  if (!ready) return;
  journey = { ...journey, onboarded: false };
  greetOnce();
}

// ── Gap resolver ─────────────────────────────────────────────────────────────
// Lightweight intent reasons, phrased against common question shapes. Real
// package ids vary per instance, so we attach a reason by resource TYPE rather
// than by a hard-coded id (the prototype's fake ids don't exist here).
function reasonFor(item) {
  if (item.resource_type === "memory_domain")
    return "domain rules and definitions to ground my answers";
  return "the data I'd query to answer questions like this";
}

// Show the in-chat gap-resolver card. Returns true if it took over the turn
// (caller must NOT send the message to the model yet — the card's "Add &
// continue" button resubmits once the Stack is ready).
async function maybeShowGapResolver(text) {
  if (journey.stack_setup_done) return false;
  let items;
  try {
    items = await browseStack();
  } catch (_) {
    return false;
  }
  // Only intercept when the Stack is genuinely empty AND there is something to
  // add. A user who already has knowledge in their Stack goes straight to the
  // model.
  if (inStackCount(items) > 0) return false;
  const candidates = items.filter((i) => !i.in_stack);
  if (!candidates.length) return false;

  const rec = candidates.slice(0, 4);
  hooks.renderAssistant(
    "I can't answer that yet — your Stack is empty. But I know what I'd need. Here's what I'd add so I can help with this and questions like it:",
  );

  const card = document.createElement("div");
  card.className = "msg msg-assistant cloud-chat-gapcard-wrap";
  card.innerHTML = `
    <div class="msg-avatar" aria-hidden="true">A</div>
    <div class="msg-bubble">
      <div class="cloud-chat-gapcard">
        <div class="cloud-chat-gapcard-label">Recommended for your question</div>
        <div class="cloud-chat-gapcard-list">
          ${rec
            .map(
              (c) => `<label class="cloud-chat-gapcard-opt">
            <input type="checkbox" checked data-gap-id="${escapeAttr(c.id)}" data-gap-type="${escapeAttr(c.resource_type)}">
            <span><b>${escapeHtml(c.name || c.id)}</b> <span class="cloud-chat-gapcard-why">— ${escapeHtml(reasonFor(c))}</span>
            ${c.description ? `<br><span class="cloud-chat-gapcard-desc">${escapeHtml(c.description)}</span>` : ""}</span>
          </label>`,
            )
            .join("")}
        </div>
        <div class="cloud-chat-gapcard-foot">
          <button type="button" class="btn btn-primary btn-sm cloud-chat-gapcard-cta">Add &amp; answer my question</button>
        </div>
        <div class="cloud-chat-gapcard-hint">Prefer to type? Just tell me — e.g. <code>add ${escapeHtml(rec[0].name || rec[0].id)}</code>.</div>
      </div>
    </div>`;
  hooks.appendNode(card);
  hooks.scrollToBottom();

  const cta = card.querySelector(".cloud-chat-gapcard-cta");
  cta.addEventListener("click", async () => {
    const picks = [...card.querySelectorAll("input[data-gap-id]:checked")].map((cb) => ({
      id: cb.getAttribute("data-gap-id"),
      type: cb.getAttribute("data-gap-type"),
    }));
    if (!picks.length) return;
    cta.disabled = true;
    cta.textContent = "Adding…";
    let added = 0;
    for (const p of picks) {
      try {
        await subscribe(p.type, p.id);
        added += 1;
      } catch (_) {
        /* skip failures (e.g. lost grant) */
      }
    }
    await patchJourney({ stack_setup_done: true });
    hooks.renderAssistant(
      `Done — added ${added} package${added === 1 ? "" : "s"} to your Stack. Now let me answer what you actually asked:`,
    );
    hooks.resubmit(text);
  });

  return true;
}

// ── "add <thing>" parity ─────────────────────────────────────────────────────
// Typing "add X" in chat does what clicking Add in the Catalog does. Returns
// true if it handled the message.
async function maybeHandleAddCommand(text) {
  const m = text.trim().match(/^(?:add|enable|install)\s+(.+)/i);
  if (!m) return false;
  const query = m[1]
    .replace(/\b(the|a|an|package|data|memory|to|my|stack|please)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  let items;
  try {
    items = await browseStack();
  } catch (_) {
    return false;
  }
  const q = (query || m[1]).toLowerCase();
  const scored = items
    .filter((i) => !i.in_stack)
    .map((i) => ({ i, s: matchScore(q, i) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s);
  if (!scored.length) {
    hooks.renderAssistant(
      `I couldn't find anything called "**${escapeHtml(m[1])}**" to add. Open the [Catalog](/stack?tab=browse) to see what's available.`,
    );
    return true;
  }
  const target = scored[0].i;
  try {
    await subscribe(target.resource_type, target.id);
  } catch (_) {
    hooks.renderAssistant(
      `I couldn't add **${escapeHtml(target.name || target.id)}** — you may not have access. Ask your admin, or pick another from the [Catalog](/stack?tab=browse).`,
    );
    return true;
  }
  await patchJourney({ stack_setup_done: true });
  hooks.renderAssistant(
    `Added **${escapeHtml(target.name || target.id)}** to your Stack ✓ — it's live now, and visible under [My Stack](/stack).`,
  );
  return true;
}

function matchScore(needle, item) {
  const hay = `${item.name || ""} ${item.id || ""} ${item.description || ""}`.toLowerCase();
  const n = needle.toLowerCase().trim();
  if (!n) return 0;
  if ((item.name || "").toLowerCase() === n) return 100;
  if (hay.includes(n)) return 60;
  let s = 0;
  n.split(/\s+/)
    .filter((t) => t.length > 2)
    .forEach((t) => {
      if (hay.includes(t)) s += 12;
    });
  return s;
}

// ── escaping ─────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}
function escapeAttr(s) {
  return escapeHtml(s);
}

// ── public API ───────────────────────────────────────────────────────────────
export async function initChatOnboarding(h) {
  hooks = h;
  await loadJourney();
  ready = true;
  renderJourneyPanel();
}

// Called from submitUserMessage right after the user bubble is rendered and
// before the message is sent to the model. Returns true when onboarding has
// taken over the turn (the model send must be skipped).
export async function onUserMessage(text, { synced } = {}) {
  if (!ready) return false;
  greetOnce(synced);
  if (!journey.first_asked) await patchJourney({ first_asked: true });

  // Parity command takes precedence — "add X" is an action, not a question.
  if (await maybeHandleAddCommand(text)) return true;

  return maybeShowGapResolver(text);
}

// Let chat.js report a successful answer so we can advance the counter.
export function noteAnswered() {
  if (!ready) return;
  patchJourney({ successful_answers: (journey.successful_answers || 0) + 1 });
}
