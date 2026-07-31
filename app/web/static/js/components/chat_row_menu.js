/* =====================================================================
 * chat_row_menu.js — the "⋮" overflow menu on a conversation row.
 *
 * The history list has TWO renderers (chat.js on /chat, rail_history.js
 * everywhere else — see rail_history.js's header for why). Row actions used to
 * be inline icon buttons, which meant every new action cost horizontal space in
 * a ~250px rail and had to be duplicated twice. This is the shared owner
 * instead: both renderers ask for a trigger button and hand over three
 * callbacks. Actions are Pin/Unpin, Rename, Delete.
 *
 * Exposes ONE global so a classic script and an ES module can both use it:
 *
 *   window.chatRowMenu.trigger({ session, onPin, onRename, onDelete })
 *       -> HTMLButtonElement to append to the row
 *   window.chatRowMenu.close()   -> close whatever is open (renderers call
 *                                   this before wiping the list)
 *
 * Contract: call it from render/event code, never at top-level parse time —
 * same rule _app_scripts.html documents for confirmModal. Both loaders use
 * `defer`, so by the time a row is built (after an async sessions fetch) this
 * is defined regardless of which script tag came first.
 *
 * Structure follows the house row-menu pattern (library.html's `lib-movemenu`):
 * a single body-appended div[role=menu] reused across rows, positioned under
 * the trigger and clamped into the viewport, arrow-key navigable, Escape and
 * outside-click close and return focus to the trigger. Body-appended rather
 * than nested in the row because the rail's history body is an
 * `overflow: hidden` scroll container — a nested menu would be clipped.
 * ===================================================================== */
(function () {
  "use strict";

  // Idempotent: loaded from both _app_rail.html and chat.html, and on a rail
  // /chat page BOTH tags are present. Second execution must not re-register the
  // document-level listeners below (every keystroke would be handled twice).
  if (window.chatRowMenu) return;

  const KEBAB_SVG =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/>' +
    "</svg>";

  let menu = null; // the single reused panel
  let openTrigger = null; // the ⋮ button it currently belongs to
  let openActions = null; // [{key, label, run, danger}] for the open menu

  function ensureMenu() {
    if (menu) return menu;
    menu = document.createElement("div");
    menu.className = "chat-rowmenu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    document.body.appendChild(menu);
    return menu;
  }

  function close(restoreFocus) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    menu.innerHTML = "";
    if (openTrigger) {
      openTrigger.setAttribute("aria-expanded", "false");
      // Only pull focus back when the user closed the menu deliberately
      // (Escape / re-click). After an action runs the list re-renders and this
      // trigger is a detached node — focusing it would drop focus to <body>.
      if (restoreFocus && openTrigger.isConnected) openTrigger.focus();
    }
    openTrigger = null;
    openActions = null;
  }

  function items() {
    return menu ? Array.from(menu.querySelectorAll(".chat-rowmenu__item")) : [];
  }

  function open(trigger, actions) {
    // Re-clicking the open trigger toggles it shut (menu-button convention).
    if (openTrigger === trigger) {
      close(true);
      return;
    }
    close(false);
    const el = ensureMenu();
    openActions = actions;

    for (const action of actions) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chat-rowmenu__item";
      if (action.danger) item.classList.add("is-danger");
      item.setAttribute("role", "menuitem");
      item.dataset.key = action.key;

      const label = document.createElement("span");
      label.className = "chat-rowmenu__label";
      label.textContent = action.label;
      item.appendChild(label);

      // The single-letter accelerator, shown the way the reference UI does:
      // right-aligned and muted. aria-hidden — the shortcut is a convenience,
      // and screen readers get the action from the label.
      const hint = document.createElement("span");
      hint.className = "chat-rowmenu__key";
      hint.textContent = action.key.toUpperCase();
      hint.setAttribute("aria-hidden", "true");
      item.appendChild(hint);

      item.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        // Close BEFORE running: the action re-renders the list (and may open a
        // modal), and a menu still anchored to a row that no longer exists
        // would hang over the new one.
        close(false);
        Promise.resolve(action.run()).catch(() => {
          /* callers surface their own errors (toast on /chat, silent on the rail) */
        });
      });
      el.appendChild(item);
    }

    // Position under the trigger, clamped into the viewport — and flipped above
    // it when a row near the bottom of a tall rail would push the panel off
    // screen. Fixed positioning (not page coords): the rail is itself a scroll
    // container, so a page-scroll offset would misplace it.
    el.hidden = false;
    const r = trigger.getBoundingClientRect();
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const below = r.bottom + 6;
    const flip = below + h > window.innerHeight - 8 && r.top - h - 6 > 8;
    el.style.top = (flip ? r.top - h - 6 : below) + "px";
    el.style.left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + "px";

    openTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    const first = items()[0];
    if (first) first.focus();
  }

  /** Build the ⋮ button for one row. `session` is the row's session object
   *  (`{id, title, pinned}`); the three callbacks do the actual work and own
   *  their own error reporting + re-render. */
  function makeTrigger(opts) {
    const s = opts.session || {};
    const name = s.title || "this conversation";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-rowmenu-btn";
    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", `More actions for ${name}`);
    btn.title = "More actions";
    btn.innerHTML = KEBAB_SVG;

    const actions = [
      {
        key: "p",
        label: s.pinned ? "Unpin" : "Pin",
        run: () => opts.onPin(!s.pinned),
      },
      { key: "r", label: "Rename", run: () => opts.onRename() },
      { key: "d", label: "Delete", danger: true, run: () => opts.onDelete() },
    ];

    btn.addEventListener("click", (e) => {
      // The row itself is a button (opens/navigates to the conversation) —
      // without this the menu would open and the row would fire too.
      e.preventDefault();
      e.stopPropagation();
      open(btn, actions);
    });
    // Same reason, for the row's own Enter/Space keydown handler.
    btn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") e.stopPropagation();
    });
    return btn;
  }

  // ---- Document-level handlers (registered once) ------------------------
  document.addEventListener("click", (e) => {
    if (menu && !menu.hidden && !menu.contains(e.target) && e.target !== openTrigger) {
      close(false);
    }
  });

  // Any scroll of an ancestor (the rail's history body, or the page) moves the
  // row out from under a fixed-position panel. Cheaper and less surprising to
  // close than to re-anchor mid-scroll. Capture phase: scroll doesn't bubble.
  window.addEventListener("scroll", () => close(false), true);
  window.addEventListener("resize", () => close(false));

  document.addEventListener("keydown", (e) => {
    if (!menu || menu.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close(true);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const list = items();
      if (!list.length) return;
      e.preventDefault();
      const at = list.indexOf(document.activeElement);
      const step = e.key === "ArrowDown" ? 1 : -1;
      // -1 (focus elsewhere) + a down-step lands on 0, which is what we want.
      list[(at + step + list.length) % list.length].focus();
      return;
    }
    if (e.key === "Home" || e.key === "End") {
      const list = items();
      if (!list.length) return;
      e.preventDefault();
      (e.key === "Home" ? list[0] : list[list.length - 1]).focus();
      return;
    }
    // Single-letter accelerators (P / R / D), matching the hints in the panel.
    // Modifier-free only, so Cmd-R still reloads the page.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const key = e.key.toLowerCase();
    const hit = (openActions || []).find((a) => a.key === key);
    if (!hit) return;
    e.preventDefault();
    close(false);
    Promise.resolve(hit.run()).catch(() => {
      /* see the click handler */
    });
  });

  window.chatRowMenu = { trigger: makeTrigger, close: () => close(false) };
})();
