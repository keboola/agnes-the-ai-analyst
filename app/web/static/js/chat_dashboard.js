// chat_dashboard.js — the rail pre-conversation Dashboard behavior.
//
// The rail layout's /chat empty state IS the Dashboard: one hero (greeting,
// Knowledge Layer banner, the real composer, Stack context line) and one
// "Suggested next actions" list below it — markup in chat.html's rail
// blocks, styles in css/chat_dashboard.css. This module owns the
// Dashboard-specific behavior and is driven by chat.js, which passes in
// the pieces of the one true chat lifecycle:
//
//   initChatDashboard({ submitPrompt, openSession })
//       — greeting fix-up + handler wiring. submitPrompt is chat.js's
//         submitUserMessage; openSession its in-place session opener.
//         Guided actions submit through them directly, so every suggestion
//         starts (or resumes) a conversation through the exact same flow
//         as a typed message. No navigation, no handoff, no second
//         composer.
//
//   updateDashboardSuggestions(sessions)
//       — builds + renders the Suggested-next-actions list once chat.js
//         has the caller's session list (no second request). Pass null
//         when that fetch failed: the personalized resume row is skipped
//         and the static suggestions still render (partial data beats a
//         broken section).
//
// Every function no-ops when the Dashboard markup is absent (topnav chat,
// or an active conversation restored from a deep link) — chat.js calls
// unconditionally.

const $ = (id) => document.getElementById(id);

// Handlers handed over by chat.js in initChatDashboard.
let _submitPrompt = null;
let _openSession = null;

// ---- Greeting -------------------------------------------------------------
// The server renders the salutation from ITS clock; re-derive from the
// browser clock so users in another timezone see the right time of day.
function fixGreeting() {
  const el = $("rdb-greeting-tod");
  if (!el) return;
  const h = new Date().getHours();
  el.textContent = h >= 5 && h < 12 ? "Good morning" : h >= 12 && h < 18 ? "Good afternoon" : "Good evening";
}

/** Compact relative timestamp: "just now" / "5m ago" / "2h ago" /
 *  "Yesterday" / "May 12". */
function relativeTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  if (hours < 48) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---- Guided task definitions ------------------------------------------------
//
// The dialog-backed conversation starters. Each entry defines the card-level
// identity (title/icon), the minimal input dialog (fields), and the prompt
// builder. Suggested-next-actions rows are derived from these via
// buildSuggestedActions() below.
//
// Task shape:
//   id           stable slug
//   title        row + dialog heading
//   description  relevance line shown in the suggestions list
//   icon         inline SVG string
//   available    render enabled; false → disabled with the unavailable hint
//   fields       [{ key, label, type: text|textarea|select, placeholder?,
//                   required?, hint?, options?: [{value, label}],
//                   showWhen?: {key, equals} }]
//   buildPrompt  (values) => string — the structured Kai prompt
//
// Prompt builders share two rules: ground Kai in COMPANY knowledge first
// (catalog / metric definitions / memory beat generic model knowledge),
// and tell it to say so honestly when something can't be found instead of
// inventing an answer.

const ICONS = {
  doc: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 3h8l4 4v14H6V3Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M14 3v4h4M9 12h6M9 16h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  person:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.7"/><path d="M5 20c1.2-3.2 3.8-4.8 7-4.8s5.8 1.6 7 4.8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  chart:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 19h16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="m5 14 4-4 3.5 3L18 7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  compare:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="5" width="7.5" height="14" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13.5" y="5" width="7.5" height="14" rx="1.5" stroke="currentColor" stroke-width="1.7"/></svg>',
  calendar:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M4 9.5h16M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H4l1.6-3.2A8 8 0 1 1 21 12Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  arrow:
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h13M12 6l6 6-6 6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
};

const SUMMARY_STYLES = {
  executive: "Executive summary — the high-level takeaways a leadership reader needs.",
  decisions: "Key decisions — every decision the document records, each with its rationale.",
  actions: "Action items — concrete follow-ups, each with its owner where stated.",
  detailed: "Detailed summary — section-by-section coverage of the whole document.",
};

const METRIC_STYLES = {
  simple: "Explain it in plain language a newcomer would understand — no jargon.",
  role: "Explain what it means for my role: how it affects my work and the decisions I make.",
  calculation: "Show exactly how it is calculated, step by step, including the source data it comes from.",
};

const COMPARE_FOCUS = {
  differences: "all meaningful differences",
  changes: "what changed between them (treat them as versions where that makes sense)",
  contradictions: "statements that contradict each other",
  metrics: "the metrics and figures each uses, and where they disagree",
  processes: "the processes each describes, and where they diverge",
};

const MEETING_FOCUS = {
  context: "the key context: the five things I most need to know walking in.",
  changes: "what changed recently around this topic — decisions, data, and documents.",
  decisions: "the decisions that need to be made in this meeting, each with the relevant background.",
  questions: "the open questions likely to come up, with what we currently know about each.",
  full: "a full briefing: background, current state, recent changes, decisions needed, open questions, and risks.",
};

const TASKS = [
  {
    id: "summarize",
    title: "Summarize a document",
    description: "Key points from any document in your company knowledge",
    icon: ICONS.doc,
    available: true,
    fields: [
      {
        key: "document",
        label: "Document",
        type: "text",
        placeholder: "e.g. Pricing deck (May 2026)",
        required: true,
        hint: "Name it the way your team does — Kai searches the company knowledge for it. You can also attach a file with the + button in the composer.",
      },
      {
        key: "style",
        label: "Summary style",
        type: "select",
        options: [
          { value: "executive", label: "Executive summary" },
          { value: "decisions", label: "Key decisions" },
          { value: "actions", label: "Action items" },
          { value: "detailed", label: "Detailed summary" },
        ],
      },
    ],
    buildPrompt: (v) =>
      [
        `Summarize the document "${v.document}".`,
        `Style: ${SUMMARY_STYLES[v.style] || SUMMARY_STYLES.executive}`,
        "Focus on conclusions, decisions, risks, and next steps. Use only that document as the source and reference it clearly.",
        "Look for it in the company knowledge sources first. If you cannot find it, say so and list the closest matches you did find instead of guessing.",
      ].join("\n"),
  },
  {
    id: "find-owner",
    title: "Find an owner or expert",
    description: "Who owns — or knows the most about — a topic, dataset, or area",
    icon: ICONS.person,
    available: true,
    fields: [
      {
        key: "subject",
        label: "Topic, project, dataset, client, or area",
        type: "text",
        placeholder: "e.g. customer segmentation model",
        required: true,
      },
    ],
    buildPrompt: (v) =>
      [
        `Who owns, or knows the most about: ${v.subject}?`,
        "Search the company knowledge for ownership signals and cite the sources you used.",
        "In your answer, clearly separate confirmed ownership (explicitly documented owners or responsible teams) from inferred expertise (people who authored, maintain, or frequently work with related material). Never present inferred expertise as confirmed ownership, and state your confidence in each.",
      ].join("\n"),
  },
  {
    id: "explain-metric",
    title: "Explain a metric or term",
    description: "Grounded in your company's canonical definitions",
    icon: ICONS.chart,
    available: true,
    fields: [
      {
        key: "metric",
        label: "Metric or term",
        type: "text",
        placeholder: "e.g. MRR, activation rate",
        required: true,
      },
      {
        key: "style",
        label: "Explanation style",
        type: "select",
        options: [
          { value: "simple", label: "Explain simply" },
          { value: "role", label: "Explain for my role" },
          { value: "calculation", label: "Show calculation" },
          { value: "compare", label: "Compare with another term" },
        ],
      },
      {
        key: "compare_with",
        label: "Compare with",
        type: "text",
        placeholder: "e.g. ARR",
        showWhen: { key: "style", equals: "compare" },
      },
    ],
    buildPrompt: (v) => {
      const style =
        v.style === "compare"
          ? `Compare it with "${v.compare_with || "the closest related term"}": definitions, calculation, and when to use which.`
          : METRIC_STYLES[v.style] || METRIC_STYLES.simple;
      return [
        `Explain the metric or term "${v.metric}".`,
        style,
        "Use company-specific knowledge first: look up our canonical definition (the metric definitions in the catalog), and include the definition, how it is calculated, who owns it, and related data where available. Point out conflicting definitions if you find them, and say explicitly when no company definition exists.",
      ].join("\n");
    },
  },
  {
    id: "compare",
    title: "Compare data or documents",
    description: "Differences, changes, and contradictions between two sources",
    icon: ICONS.compare,
    available: true,
    fields: [
      {
        key: "source_a",
        label: "Source A",
        type: "text",
        placeholder: "e.g. Q1 planning notes",
        required: true,
      },
      {
        key: "source_b",
        label: "Source B",
        type: "text",
        placeholder: "e.g. Q2 planning notes",
        required: true,
      },
      {
        key: "focus",
        label: "Focus on",
        type: "select",
        options: [
          { value: "differences", label: "Key differences" },
          { value: "changes", label: "Changes over time" },
          { value: "contradictions", label: "Contradictions" },
          { value: "metrics", label: "Metrics" },
          { value: "processes", label: "Processes" },
          { value: "custom", label: "Custom question…" },
        ],
      },
      {
        key: "focus_custom",
        label: "What should the comparison focus on?",
        type: "text",
        placeholder: "e.g. the churn assumptions",
        showWhen: { key: "focus", equals: "custom" },
      },
    ],
    buildPrompt: (v) => {
      const focus =
        v.focus === "custom" && v.focus_custom ? v.focus_custom : COMPARE_FOCUS[v.focus] || COMPARE_FOCUS.differences;
      return [
        "Compare these two sources from our company knowledge:",
        `A: ${v.source_a}`,
        `B: ${v.source_b}`,
        `Focus on: ${focus}.`,
        "Locate both sources first; if either cannot be found, stop and say so. Present the comparison as a structured summary and quote the relevant passages when you call out a difference or contradiction.",
      ].join("\n");
    },
  },
  {
    id: "prepare-meeting",
    title: "Prepare for a meeting",
    description: "A briefing built from your accessible company context",
    icon: ICONS.calendar,
    available: true,
    fields: [
      {
        key: "meeting",
        label: "Meeting name or purpose",
        type: "text",
        placeholder: "e.g. Q3 pricing review",
        required: true,
      },
      {
        key: "participants",
        label: "Participants (optional)",
        type: "text",
        placeholder: "e.g. finance team, Alex from sales",
      },
      {
        key: "context",
        label: "Project, client, topic, or documents to draw on (optional)",
        type: "textarea",
        placeholder: "Anything Kai should know or look at",
      },
      {
        key: "focus",
        label: "Briefing focus",
        type: "select",
        options: [
          { value: "context", label: "Key context" },
          { value: "changes", label: "Recent changes" },
          { value: "decisions", label: "Decisions needed" },
          { value: "questions", label: "Open questions" },
          { value: "full", label: "Full briefing" },
        ],
      },
    ],
    buildPrompt: (v) => {
      const lines = [`Help me prepare for the meeting "${v.meeting}".`];
      if (v.participants) lines.push(`Participants: ${v.participants}.`);
      if (v.context) lines.push(`Context: ${v.context}`);
      lines.push(`Give me ${MEETING_FOCUS[v.focus] || MEETING_FOCUS.context}`);
      lines.push(
        "Use the company knowledge for background on the topics" +
          (v.participants ? " and the participants' teams" : "") +
          ". I have not shared my calendar — work only from what I gave you here.",
      );
      return lines.join("\n");
    },
  },
];

// ---- Suggested next actions — the personalization boundary -----------------
//
// TODO(personalization): replace buildSuggestedActions() with a backend
// recommendation source (e.g. GET /api/me/suggested-actions) that ranks
// actions from the caller's Stack (knowledge sources, skills), recent
// conversations, and accessible company context. The renderer below only
// consumes the typed shape — swapping the source requires no UI change.
//
// What is REAL today: the resume row is built from the caller's actual
// session list (chat.js's cache of GET /api/chat/sessions). Everything
// else is the static guided-task defaults (honest capability statements,
// NOT presented as AI-generated recommendations).
//
// Suggested-action shape (superset of what the renderer reads):
//   id           stable slug (also the dismissal key)
//   kind         "task" (dialog-backed prompt) | "resume" (open a session)
//   title        row title
//   reason       short relevance line (muted, under the title)
//   icon         inline SVG string
//   cta          trailing action label ("Start" / "Resume")
//   priority     ascending sort rank
//   dismissible  row shows the hover dismiss control
//   available    false → disabled row with an unavailable hint
//   task         (kind=task) the TASKS entry to run
//   sessionId    (kind=resume) session to reopen

const DISMISSED_KEY = "agnes.dashboard.dismissedActions";

function _dismissedIds() {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (_) {
    return [];
  }
}

function _dismiss(id) {
  try {
    const ids = _dismissedIds();
    if (!ids.includes(id)) ids.push(id);
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(ids));
  } catch (_) {
    /* private mode — dismissal lasts until reload only */
  }
}

/** Build the caller's suggested-action list. `sessions` is the (already
 *  fetched) session list, or null when that fetch failed. */
function buildSuggestedActions(sessions) {
  const actions = [];

  // Real personalization: pick up the most recent conversation.
  if (Array.isArray(sessions) && sessions.length > 0) {
    const s = sessions[0];
    const when = relativeTime(s.last_message_at || s.started_at);
    actions.push({
      id: `resume-${s.id}`,
      kind: "resume",
      sessionId: s.id,
      title: `Pick up where you left off: ${s.title || "your last conversation"}`,
      reason: when ? `Your most recent conversation · ${when}` : "Your most recent conversation",
      icon: ICONS.chat,
      cta: "Resume",
      priority: 0,
      dismissible: true,
      available: true,
    });
  }

  // Static defaults until a backend recommendation source exists.
  TASKS.forEach((task, i) => {
    actions.push({
      id: task.id,
      kind: "task",
      task,
      title: task.title,
      reason: task.description,
      icon: task.icon,
      cta: "Start",
      priority: 10 + i,
      dismissible: true,
      available: task.available,
    });
  });

  const dismissed = new Set(_dismissedIds());
  return actions.filter((a) => !dismissed.has(a.id)).sort((a, b) => a.priority - b.priority);
}

// ---- Suggested next actions — renderer --------------------------------------

function _runAction(action) {
  if (!action.available) return;
  if (action.kind === "resume" && _openSession) {
    _openSession(action.sessionId);
  } else if (action.kind === "task" && _submitPrompt) {
    openTaskDialog(action.task, _submitPrompt);
  }
}

function _renderActionRow(action, rerender) {
  const li = document.createElement("li");
  li.className = "rdb-action-row";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rdb-action";
  btn.dataset.action = action.id;
  btn.disabled = !action.available;
  btn.setAttribute("aria-label", `${action.title} — ${action.available ? action.reason : "not available on this instance"}`);

  const icon = document.createElement("span");
  icon.className = "rdb-action-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = action.icon;

  const txt = document.createElement("span");
  txt.className = "rdb-action-txt";
  const title = document.createElement("span");
  title.className = "rdb-action-title";
  title.textContent = action.title;
  const reason = document.createElement("span");
  reason.className = "rdb-action-reason";
  reason.textContent = action.available ? action.reason : "Not available on this instance";
  txt.append(title, reason);

  btn.append(icon, txt);
  if (action.available) {
    const cta = document.createElement("span");
    cta.className = "rdb-action-cta";
    cta.setAttribute("aria-hidden", "true"); // the button label carries the name
    const ctaTxt = document.createElement("span");
    ctaTxt.textContent = action.cta;
    const arrow = document.createElement("span");
    arrow.innerHTML = ICONS.arrow;
    cta.append(ctaTxt, arrow);
    btn.appendChild(cta);
    btn.addEventListener("click", () => _runAction(action));
  }
  li.appendChild(btn);

  if (action.dismissible) {
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "rdb-action-dismiss";
    dismiss.setAttribute("aria-label", `Dismiss suggestion: ${action.title}`);
    dismiss.innerHTML = ICONS.x;
    dismiss.addEventListener("click", (e) => {
      e.stopPropagation();
      _dismiss(action.id);
      rerender();
    });
    li.appendChild(dismiss);
  }
  return li;
}

/** Render the Suggested-next-actions section. Call once chat.js has the
 *  session list; pass null when that fetch failed (partial data: the
 *  personalized resume row is skipped, static suggestions still render). */
export function updateDashboardSuggestions(sessions) {
  const section = $("rdb-actions");
  const list = $("rdb-actions-list");
  if (!section || !list) return; // topnav, or dashboard markup not on this page
  const loading = $("rdb-actions-loading");
  const empty = $("rdb-actions-empty");
  if (loading) loading.hidden = true;

  const actions = buildSuggestedActions(sessions);
  const rerender = () => updateDashboardSuggestions(sessions);

  list.innerHTML = "";
  if (empty) empty.hidden = actions.length > 0;
  for (const action of actions) list.appendChild(_renderActionRow(action, rerender));
}

// ---- Task dialog --------------------------------------------------------------
// Built on the shared .modal-backdrop / .modal-card scaffold from
// style-custom.css (the same structure modal.js's confirm/alert/prompt use —
// those helpers only support single-value dialogs, so the multi-field form is
// composed here with matching Esc / backdrop-click / focus-trap behavior).

function openTaskDialog(task, submitPrompt) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop is-open";
  backdrop.setAttribute("role", "dialog");
  backdrop.setAttribute("aria-modal", "true");
  backdrop.dataset.noEscClose = "1"; // we own Esc; opt out of the global handler

  const card = document.createElement("div");
  card.className = "modal-card";

  const h = document.createElement("h3");
  h.id = `rdb-dialog-title-${task.id}`;
  h.textContent = task.title;
  backdrop.setAttribute("aria-labelledby", h.id);
  card.appendChild(h);

  const form = document.createElement("form");
  const fieldsWrap = document.createElement("div");
  fieldsWrap.className = "rdb-dialog-fields";
  const controls = new Map(); // key -> input element
  const fieldWraps = new Map(); // key -> field container (for showWhen)

  for (const field of task.fields) {
    const wrap = document.createElement("div");
    wrap.className = "rdb-field";

    const label = document.createElement("label");
    label.className = "rdb-field-label";
    label.htmlFor = `rdb-f-${task.id}-${field.key}`;
    label.textContent = field.label;
    wrap.appendChild(label);

    let control;
    if (field.type === "select") {
      control = document.createElement("select");
      for (const opt of field.options || []) {
        const o = document.createElement("option");
        o.value = opt.value;
        o.textContent = opt.label;
        control.appendChild(o);
      }
    } else if (field.type === "textarea") {
      control = document.createElement("textarea");
      if (field.placeholder) control.placeholder = field.placeholder;
    } else {
      control = document.createElement("input");
      control.type = "text";
      if (field.placeholder) control.placeholder = field.placeholder;
    }
    control.id = `rdb-f-${task.id}-${field.key}`;
    if (field.required) control.setAttribute("aria-required", "true");
    // Enter submits from single-line inputs — bound explicitly (like
    // modal.js's promptModal) rather than relying on implicit form
    // submission, which the app's global key handling can swallow.
    if (field.type !== "textarea") {
      control.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          form.requestSubmit();
        }
      });
    }
    wrap.appendChild(control);

    if (field.hint) {
      const hint = document.createElement("span");
      hint.className = "rdb-field-hint";
      hint.textContent = field.hint;
      wrap.appendChild(hint);
    }

    controls.set(field.key, control);
    fieldWraps.set(field.key, wrap);
    fieldsWrap.appendChild(wrap);
  }

  // Conditional visibility (e.g. compare's custom-focus field).
  const applyShowWhen = () => {
    for (const field of task.fields) {
      if (!field.showWhen) continue;
      const dep = controls.get(field.showWhen.key);
      const visible = !!dep && dep.value === field.showWhen.equals;
      fieldWraps.get(field.key).hidden = !visible;
    }
  };
  for (const field of task.fields) {
    if (field.type === "select") controls.get(field.key).addEventListener("change", applyShowWhen);
  }
  applyShowWhen();

  const errorLine = document.createElement("p");
  errorLine.className = "rdb-dialog-error";
  errorLine.setAttribute("role", "alert");
  errorLine.hidden = true;

  const actions = document.createElement("div");
  actions.className = "modal-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn btn-secondary";
  cancel.textContent = "Cancel";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary";
  submit.textContent = "Ask Kai";
  actions.append(cancel, submit);

  form.append(fieldsWrap, errorLine, actions);
  card.appendChild(form);
  backdrop.appendChild(card);

  const prevFocus = document.activeElement;
  const close = () => {
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    if (prevFocus && typeof prevFocus.focus === "function") {
      try {
        prevFocus.focus();
      } catch (_) {
        /* element gone */
      }
    }
  };

  const onKey = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      // Minimal focus trap — same approach as modal.js.
      const f = card.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
      const focusables = Array.from(f).filter((el) => !el.closest("[hidden]"));
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };

  cancel.addEventListener("click", close);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey, true);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const values = {};
    for (const field of task.fields) {
      const control = controls.get(field.key);
      const wrap = fieldWraps.get(field.key);
      values[field.key] = wrap.hidden ? "" : control.value.trim();
      if (field.required && !values[field.key]) {
        errorLine.textContent = `Please fill in “${field.label}”.`;
        errorLine.hidden = false;
        control.focus();
        return;
      }
    }
    // Close first, then send through the one chat flow — submitPrompt
    // (chat.js's submitUserMessage) hides the dashboard, renders the user
    // bubble, and surfaces any conversation-creation failure in the chat
    // status banner exactly like a typed message.
    close();
    submitPrompt(task.buildPrompt(values));
  });

  document.body.appendChild(backdrop);
  const firstControl = controls.get(task.fields[0]?.key);
  if (firstControl) firstControl.focus();
}

// ---- init ---------------------------------------------------------------------

/** Wire the Dashboard empty state. No-ops when its markup is absent
 *  (topnav chat). ``submitPrompt`` is chat.js's submitUserMessage;
 *  ``openSession`` its in-place session opener. */
export function initChatDashboard({ submitPrompt, openSession }) {
  if (!$("rdb-actions")) return;
  _submitPrompt = submitPrompt;
  _openSession = openSession;
  fixGreeting();
}
