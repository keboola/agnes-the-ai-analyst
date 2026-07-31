// rail_history.js — the rail's recent-conversations list: populated here on
// every page EXCEPT /chat.
//
// The rail (html[data-ui-layout="rail"]) renders the chat list
// (_app_rail.html → .rail-history) on every page, directly under New chat and
// with no heading of its own. On /chat, chat.js owns that same
// <ul id="chat-list"> — it renders live, highlights the active row, and handles
// open/delete in place — so this script MUST stay out of its way there. On every
// other page chat.js isn't loaded, so we fetch the caller's sessions and render
// the same rows, wiring each to a /chat?session=<id> navigation.
//
// Nothing here truncates the list. It fills the free space between the rail's
// two fixed zones and scrolls inside it, in one state — see the note further
// down for why the old five-row cap and its "View all chats" toggle were
// redundant, and what went away with them.
//
// Pinning is server-side state (`chat_sessions.pinned_at`, PUT
// /api/chat/sessions/{id}/pin). Pinned rows are hoisted into a leading "Pinned"
// group on BOTH paths — which is why chat.js stamps `data-pinned="1"` on its own
// rows too.
//
// Row actions (Pin/Unpin · Rename · Delete) live behind one "⋮" menu owned by
// js/components/chat_row_menu.js, shared with chat.js so both renderers offer
// the identical set. Rename goes through PUT /api/chat/sessions/{id}/title.
//
// Loaded with `defer` (not a module) from the rail partial, gated on can_chat.
(function () {
  "use strict";

  // ---- No truncation, and no "View all chats" toggle --------------------
  // Both are gone, and the reason they were redundant is worth recording so
  // they don't come back. `.rail-history` already occupies exactly the free
  // space between the two fixed zones and `.rail-history-body` scrolls inside
  // it, so the list was ALWAYS bounded by the column's own geometry. Capping it
  // at 5 rows on top of that meant a laptop with room for nine showed five and
  // then offered a button to reveal rows that already fitted — a control whose
  // whole job was to undo a limit we imposed ourselves.
  //
  // Deleting it took the truncation pass, its MutationObserver, the
  // localStorage expanded-state, the date-header hiding, the "never truncate
  // the active row" special case and the pins-are-exempt budget with it: none
  // of those rules had anything to describe once the list simply fills its
  // space and scrolls. Scrolling is also the more discoverable affordance —
  // every conversation list in every comparable product works this way.
  //
  // Date group headers now always render (they were hidden only while
  // truncated), and the pinned group needs no special handling.

  // ---- Mobile/tablet nav collapse (row layout, ≤1024px) --------------
  // Below 1024px the rail becomes a wrapping top bar with nowhere to collapse
  // to — this toggle shows/hides both nav zones + recents + Admin
  // (.rail-collapsible); the foot (the profile row) stays reachable
  // regardless of its state. Inert above the breakpoint (the
  // button is display:none there, rail.css) — no harm wiring it always.
  const navToggle = document.getElementById("rail-collapse-toggle");
  const railEl = document.querySelector("html[data-ui-layout='rail'] .rail");
  if (navToggle && railEl) {
    navToggle.addEventListener("click", () => {
      const open = !railEl.classList.contains("is-nav-open");
      railEl.classList.toggle("is-nav-open", open);
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // ---- Onboarding card popover ("Set up Agnes") -----------------------
  // Reveals on hover via CSS, but ALSO opens on click and stays pinned (the
  // `.is-open` class forces it visible) so click users aren't left with a
  // dead button. Outside-click / Escape closes it. chat_onboarding.js fills
  // #chat-journey inside it — and hides the whole row once every step is done.
  // Wired before the /chat bail so it works there too.
  const gsToggle = document.getElementById("rail-getstarted-toggle");
  const gsWrap = document.getElementById("railGetStarted");
  if (gsToggle && gsWrap) {
    const setOpen = (open) => {
      // Ask the checklist to re-read /api/chat/journey before it comes into
      // view. Steps complete from real activity now (adding to your stack from
      // the Library marks its milestone server-side), so the copy this panel
      // loaded at page load can already be out of date by the time it is
      // opened — and a checklist showing a step you just finished as pending is
      // the single fastest way to make onboarding feel broken.
      if (open) document.dispatchEvent(new CustomEvent("agnes:journey-refresh"));
      gsWrap.classList.toggle("is-open", open);
      // `.is-closed` (rail.css) is the only thing that can override the
      // panel's CSS :hover / :focus-within reveal rules — closing here
      // otherwise has no visible effect while the cursor is still over the
      // launcher (exactly when a click-to-close fires) or while the toggle
      // itself holds focus (it's a descendant of .rail-getstarted, so
      // :focus-within stays true after Escape moves focus there below).
      gsWrap.classList.toggle("is-closed", !open);
      gsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    gsToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(!gsWrap.classList.contains("is-open"));
    });
    document.addEventListener("click", (e) => {
      if (gsWrap.classList.contains("is-open") && !gsWrap.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && gsWrap.classList.contains("is-open")) {
        setOpen(false);
        gsToggle.focus();
      }
    });
    // Once the cursor genuinely leaves the launcher, lift the suppression so
    // a later hover can preview the panel again — `.is-closed` is meant to
    // block the SAME hover session from immediately reopening what was just
    // closed, not to disable hover-to-preview permanently.
    // ...but only once nothing inside the launcher still holds focus. The
    // toggle keeps DOM focus after a click-to-close, and Escape explicitly
    // refocuses it, so lifting `.is-closed` while `:focus-within` is still
    // true would let the CSS reveal reopen the panel the instant the cursor
    // leaves. Guarding on activeElement keeps hover-to-preview working while
    // fixing the toggle-click / Escape close paths.
    gsWrap.addEventListener("mouseleave", () => {
      if (!gsWrap.contains(document.activeElement)) gsWrap.classList.remove("is-closed");
    });
  }

  // ---- Scroll fade on the recents list --------------------------------
  // Marks WHICH edge of .rail-history-body still has rows behind it; rail.css
  // turns each flag into a gradient mask so the list dissolves into the rail
  // instead of ending in a hard cut through a title. Only the state lives here
  // — the pixels are the stylesheet's.
  //
  // Wired BEFORE the /chat bail, like the nav collapse and the onboarding
  // popover above: chat.js owns the ROWS on /chat, but the scroll container is
  // the same element on every rail page and no other script touches it.
  const histBody = document.getElementById("rail-history-body");
  if (histBody) {
    // A pixel of slack at both ends. Fractional scroll offsets (browser zoom,
    // HiDPI, momentum scrolling) mean scrollTop rarely lands exactly on 0 or
    // exactly on the maximum, so an exact comparison leaves the bottom fade
    // permanently on at the true end of the list — which is precisely the
    // "there is a clipped row down there" impression the fade exists to avoid.
    const EPS = 1;
    const syncFade = () => {
      const max = histBody.scrollHeight - histBody.clientHeight;
      histBody.classList.toggle("is-fade-top", histBody.scrollTop > EPS);
      histBody.classList.toggle("is-fade-bottom", max > EPS && histBody.scrollTop < max - EPS);
    };
    histBody.addEventListener("scroll", syncFade, { passive: true });
    // Observe the boxes rather than syncing once at load: the rows arrive from
    // an async fetch (on BOTH renderers), rename/delete/pin change the list's
    // height under us, and the free space itself moves whenever the viewport
    // or the zones around it resize. Watching the list's own box covers the
    // content changes without a MutationObserver.
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(syncFade);
      ro.observe(histBody);
      const histList = document.getElementById("chat-list");
      if (histList) ro.observe(histList);
    }
    syncFade();
  }

  // /chat is chat.js's turf for the LIST — bail so we never double-render or
  // fight it (the nav collapse + onboarding popover above are wired for both).
  if (document.body.classList.contains("chat-page-body")) return;

  // Looked up here, NOT hoisted to the top of the IIFE. It used to be a
  // module-level `listEl` declared beside the truncation state; when that block
  // was deleted this line still read `listEl`, so the whole script threw a
  // ReferenceError and the rail rendered NO conversations on any page except
  // /chat. `node --check` cannot catch that — it is valid syntax.
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

  // ---- Grouping -------------------------------------------------------
  // Mirrors chat.js's _groupSessionsByDate under RAIL so the list reads
  // identically on /chat and everywhere else: Pinned first (if any), then an
  // unlabelled recent bucket (rolling 7 days), then Older. Empty buckets are
  // dropped. The server already sorts sessions pinned-first and then
  // most-recent-first.
  //
  // Only two buckets, where there were five. The rail gives this list about
  // seven rows between its two fixed zones, and each header costs a full row
  // (rail.css: 14px margin + line + 4px ≈ the 34px row height) — so Today /
  // Yesterday / Earlier this week / Earlier this month / Older put THREE
  // headers among five titles and spent more than a third of the visible list
  // labelling groups of one. The labels were also redundant with the ordering,
  // which is strictly most-recent-first. What survives is the one boundary the
  // ordering cannot express: past "Older", search rather than scroll.
  //
  // chat.js keeps all five under TOPNAV, where the conversations column is
  // full-height and the labels are what make twenty titles scannable. This
  // file only ever runs under rail, so it needs no branch.
  //
  // Pinned conversations are HOISTED out of their date bucket rather than
  // duplicated — a chat is either under Pinned or under its date, never both.
  // Pinned keeps its label: it is the only group that breaks the chronology,
  // so it is the only one that cannot be inferred from position.
  function groupByDate(sessions) {
    const now = new Date();
    const startOfToday = new Date(now);
    startOfToday.setHours(0, 0, 0, 0);
    // Rolling 7 days rather than the ISO week that used to bound "Earlier this
    // week": a week boundary means Friday's work drops into the archive when
    // you sit down on Monday, which is exactly when you want it.
    const startOfRecent = new Date(startOfToday);
    startOfRecent.setDate(startOfRecent.getDate() - 6); // today included

    const groups = [
      { label: null, items: [], threshold: startOfRecent },
      { label: "Older", items: [], threshold: new Date(0), boundaryLabel: true },
    ];
    const pinned = { label: "Pinned", items: [], pinnedGroup: true };
    for (const s of sessions) {
      if (s.pinned) {
        pinned.items.push(s);
        continue;
      }
      const ts = s.last_message_at || s.started_at;
      const d = ts ? new Date(ts) : new Date(0);
      for (const g of groups) {
        if (d >= g.threshold) {
          g.items.push(s);
          break;
        }
      }
    }
    return [pinned, ...groups].filter((g) => g.items.length > 0);
  }

  // ---- Row rendering --------------------------------------------------
  // Same markup/classes as chat.js's _makeSidebarItem so rail.css styles both
  // identically — but the row NAVIGATES (this page has no in-place session
  // machinery) instead of calling openSession.
  // Pushpin glyph, shared with chat.js's copy. Outline by default; rail.css
  // fills it via `fill: currentColor` on the pressed state so a pinned row
  // reads as pinned at a glance and not only by position.
  const PIN_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 17v5"/>' +
    '<path d="M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.2V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.8' +
    'a2 2 0 0 0-1.1-1.7l-1.8-.9A2 2 0 0 1 15 10.8V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>' +
    "</svg>";

  function makeRow(s) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    // Read by rail.css for the always-visible pin button on a pinned row, and
    // it is the contract chat.js mirrors (see the header comment).
    if (s.pinned) {
      li.dataset.pinned = "1";
      li.classList.add("is-pinned");
    }
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

    // Pinned-state indicator. NOT a control — the pin ACTION lives in the row
    // menu now, so without this a pinned row would look identical to any other
    // once the "Pinned" header scrolled out of view. Filled (rail.css) and
    // aria-hidden: the row's group header already names the state for AT.
    if (s.pinned) {
      const flag = document.createElement("span");
      flag.className = "cloud-chat-pin-flag";
      flag.setAttribute("aria-hidden", "true");
      flag.innerHTML = PIN_SVG;
      li.appendChild(flag);
    }

    // One "⋮" for all three row actions (Pin/Unpin · Rename · Delete). Inline
    // icon buttons don't scale in a ~250px rail, and the shared component keeps
    // the menu identical to chat.js's copy. Absent if the component failed to
    // load — the row still opens the conversation, which is its main job.
    if (window.chatRowMenu) {
      li.appendChild(
        window.chatRowMenu.trigger({
          session: s,
          onPin: (pinned) => setPinned(s.id, pinned),
          onRename: () => renameSession(s),
          onDelete: () => deleteSession(s, { confirm: true }),
        }),
      );
    }
    return li;
  }

  function render(sessions) {
    // Every action re-renders, and the menu is body-appended (not a child of the
    // row) — so without this an open panel would survive the wipe and hover over
    // a row it no longer belongs to.
    if (window.chatRowMenu) window.chatRowMenu.close();
    list.innerHTML = "";
    for (const group of groupByDate(sessions)) {
      // Same two suppressions as chat.js's _renderSidebar: a null label is the
      // deliberately unlabelled recent bucket, and the "Older" BOUNDARY label
      // is dropped when nothing renders above it — a boundary with nothing on
      // the near side just tells someone returning after a month that all
      // their work is old. "Pinned" always renders.
      if (group.label && !(group.boundaryLabel && list.childElementCount === 0)) {
        const header = document.createElement("li");
        header.className = "cloud-chat-list-group-header";
        // Marks the hoisted "Pinned" group for rail.css.
        if (group.pinnedGroup) header.classList.add("is-pinned-group");
        header.setAttribute("role", "presentation");
        header.textContent = group.label;
        list.appendChild(header);
      }
      for (const s of group.items) list.appendChild(makeRow(s));
    }
    if (emptyEl) emptyEl.hidden = sessions.length > 0;
  }

  async function setPinned(id, pinned) {
    try {
      await api(`/api/chat/sessions/${id}/pin`, {
        method: "PUT",
        body: JSON.stringify({ pinned }),
      });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load(); // re-fetch so the row moves into (or out of) the Pinned group
  }

  async function renameSession(s) {
    if (typeof window.promptModal !== "function") return;
    const next = await window.promptModal({
      title: "Rename conversation",
      message: "This is the name shown in the history panel.",
      defaultValue: s.title || "",
      placeholder: "Conversation name",
      confirmText: "Rename",
    });
    // null = cancelled/Escape. An unchanged or blank-only title is a no-op
    // rather than a request the server would just 400.
    if (next === null) return;
    const title = next.trim();
    if (!title || title === (s.title || "")) return;
    try {
      await api(`/api/chat/sessions/${s.id}/title`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
    } catch (_) {
      return; // silent on the rail — no toast surface off /chat
    }
    await load();
  }

  async function deleteSession(s, opts) {
    // `s` may be a bare id (internal callers) or a session object (the menu,
    // which needs the title for the confirmation copy).
    const id = typeof s === "string" ? s : s.id;
    const name = typeof s === "string" ? "this conversation" : s.title || "this conversation";
    if (opts && opts.confirm && typeof window.confirmModal === "function") {
      const ok = await window.confirmModal({
        title: "Delete conversation?",
        message: `"${name}" will be permanently deleted.`,
        confirmText: "Delete",
        danger: true,
      });
      if (!ok) return;
    }
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
