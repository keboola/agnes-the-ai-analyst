// rail_history.js — populates the rail's "Chats" history on every page
// EXCEPT /chat.
//
// The rail (html[data-ui-layout="rail"]) renders the chat-history list
// (_app_rail.html → .rail-history) on every page. On /chat, chat.js owns that
// same <ul id="chat-list"> — it renders live, highlights the active row, and
// handles open/delete in place — so this script MUST stay out of its way. On
// every other page chat.js isn't loaded, so we fetch the caller's sessions and
// render the same rows, wiring each to a /chat?session=<id> navigation.
//
// Loaded with `defer` (not a module) from the rail partial, gated on can_chat.
(function () {
  "use strict";

  // ---- Collapse toggle (runs on EVERY rail page, /chat included) -------
  // The section is a button + region (not a native <details>, which won't
  // scroll as a flex parent — see rail.css). We manage expand/collapse via an
  // `.is-collapsed` class + aria-expanded, persisted so it survives navigation.
  // This is wired BEFORE the /chat bail below so the toggle works there too
  // (on /chat, chat.js populates the list; the collapse is still ours).
  const OPEN_KEY = "agnes.rail.historyOpen";
  const section = document.getElementById("rail-history");
  const toggle = document.getElementById("rail-history-toggle");
  if (section && toggle) {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(OPEN_KEY) === "0";
    } catch (_) {
      /* private mode / storage disabled — keep the expanded default */
    }
    const apply = (isCollapsed) => {
      section.classList.toggle("is-collapsed", isCollapsed);
      toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
    };
    apply(collapsed);
    toggle.addEventListener("click", () => {
      collapsed = !section.classList.contains("is-collapsed");
      apply(collapsed);
      try {
        localStorage.setItem(OPEN_KEY, collapsed ? "0" : "1");
      } catch (_) {
        /* storage off — state survives until reload */
      }
    });
  }

  // ---- "Get started" journey popover (runs on EVERY rail page) --------
  // A pinned foot button opens the "Your Journey" onboarding card as a popover
  // (chat_onboarding.js fills #chat-journey inside it). Toggle open/close,
  // close on Escape or an outside click. Wired before the /chat bail so it
  // works there too.
  const gsToggle = document.getElementById("rail-getstarted-toggle");
  const gsPanel = document.getElementById("rail-getstarted-panel");
  const gsWrap = document.getElementById("railGetStarted");
  if (gsToggle && gsPanel && gsWrap) {
    const setOpen = (open) => {
      gsPanel.hidden = !open;
      gsToggle.setAttribute("aria-expanded", open ? "true" : "false");
      gsWrap.classList.toggle("is-open", open);
    };
    gsToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(gsPanel.hidden);
    });
    document.addEventListener("click", (e) => {
      if (!gsPanel.hidden && !gsWrap.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !gsPanel.hidden) {
        setOpen(false);
        gsToggle.focus();
      }
    });
  }

  // /chat is chat.js's turf for the LIST — bail so we never double-render or
  // fight it (the collapse toggle + Get started popover above are wired for
  // both).
  if (document.body.classList.contains("chat-page-body")) return;

  const list = document.getElementById("chat-list");
  if (!list) return; // no chat grant / history section not rendered

  const emptyEl = document.getElementById("cloud-chat-empty-state");

  // ---- Fetch helper ---------------------------------------------------
  async function api(path, init) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      ...(init || {}),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    if (r.status === 204 || r.headers.get("content-length") === "0") return null;
    return r.json();
  }

  // ---- Date grouping --------------------------------------------------
  // Mirrors chat.js's _groupSessionsByDate so the rail reads identically on
  // /chat and everywhere else: Today / Yesterday / Earlier this week /
  // Earlier this month / Older, most-recent bucket first, empty buckets
  // dropped. The server already sorts sessions most-recent-first.
  function groupByDate(sessions) {
    const now = new Date();
    const startOfToday = new Date(now);
    startOfToday.setHours(0, 0, 0, 0);
    const startOfYesterday = new Date(startOfToday);
    startOfYesterday.setDate(startOfYesterday.getDate() - 1);
    const startOfWeek = new Date(startOfToday);
    const dow = (startOfWeek.getDay() + 6) % 7; // ISO week, Monday = 0
    startOfWeek.setDate(startOfWeek.getDate() - dow);
    const startOfMonth = new Date(startOfToday);
    startOfMonth.setDate(startOfMonth.getDate() - 30);

    const groups = [
      { label: "Today", items: [], threshold: startOfToday },
      { label: "Yesterday", items: [], threshold: startOfYesterday },
      { label: "Earlier this week", items: [], threshold: startOfWeek },
      { label: "Earlier this month", items: [], threshold: startOfMonth },
      { label: "Older", items: [], threshold: new Date(0) },
    ];
    for (const s of sessions) {
      const ts = s.last_message_at || s.started_at;
      const d = ts ? new Date(ts) : new Date(0);
      for (const g of groups) {
        if (d >= g.threshold) {
          g.items.push(s);
          break;
        }
      }
    }
    return groups.filter((g) => g.items.length > 0);
  }

  // ---- Row rendering --------------------------------------------------
  // Same markup/classes as chat.js's _makeSidebarItem so rail.css styles both
  // identically — but the row NAVIGATES (this page has no in-place session
  // machinery) instead of calling openSession.
  function makeRow(s) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    li.title = s.title || `Untitled · ${s.id}`;
    li.setAttribute("role", "button");
    li.tabIndex = 0;
    li.setAttribute("aria-label", `Open ${s.title || "untitled conversation"}`);
    const go = () => {
      window.location.href = `/chat?session=${encodeURIComponent(s.id)}`;
    };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        go();
      }
    });

    const label = document.createElement("span");
    label.className = "cloud-chat-list-label";
    label.textContent = s.title || "Untitled chat";
    li.appendChild(label);

    if (s.surface === "slack_dm" || s.surface === "slack_thread") {
      const badge = document.createElement("span");
      badge.className = "cloud-chat-surface-badge";
      badge.textContent = "Slack";
      badge.setAttribute("aria-hidden", "true");
      li.appendChild(badge);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.className = "cloud-chat-list-del";
    del.setAttribute("aria-label", `Delete ${s.title || "this conversation"}`);
    del.innerHTML = "&times;";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      await deleteSession(s.id);
    });
    li.appendChild(del);
    return li;
  }

  function render(sessions) {
    list.innerHTML = "";
    for (const group of groupByDate(sessions)) {
      const header = document.createElement("li");
      header.className = "cloud-chat-list-group-header";
      header.setAttribute("role", "presentation");
      header.textContent = group.label;
      list.appendChild(header);
      for (const s of group.items) list.appendChild(makeRow(s));
    }
    if (emptyEl) emptyEl.hidden = sessions.length > 0;
  }

  async function deleteSession(id) {
    try {
      await api(`/api/chat/sessions/${id}`, { method: "DELETE" });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load();
  }

  async function load() {
    try {
      const sessions = await api("/api/chat/sessions");
      render(Array.isArray(sessions) ? sessions : []);
    } catch (_) {
      // Leave the list empty and reveal the empty-state; a failed fetch here
      // shouldn't break the page the user actually navigated to.
      if (emptyEl) emptyEl.hidden = false;
    }
  }

  load();
})();
