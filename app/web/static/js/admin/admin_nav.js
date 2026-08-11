// admin_nav.js — the grouped /admin sidebar (`_admin_nav.html`): per-section
// disclosure + the whole-sidebar collapse, both persisted per browser in
// localStorage.
//
// Server-side rendering already gets FIRST PAINT right — see the comment
// block at the top of `_admin_nav.html`:
//   - the active section's body has no `hidden` attribute and its header's
//     `aria-expanded` is already "true"; every other section is already
//     collapsed.
//   - a stored "sidebar collapsed" preference is applied by a tiny inline
//     script directly inside the aside, before this file (loaded `defer`)
//     even runs.
//
// This file's job is everything that needs a stored preference this HTML
// response couldn't have known: a caller's own manually-opened/closed
// sections (which may disagree with the server's "just the active one"
// default), and the interactive parts of the collapsed-mode icon rail
// (flyouts, Escape-to-close).
(function () {
  "use strict";

  const nav = document.querySelector("[data-admin-nav]");
  if (!nav) return;

  const ACTIVE_KEY = nav.dataset.activeSection || "";

  // ---- Per-section disclosure --------------------------------------------
  const SEC_STORAGE_KEY = "agnes.adminNav.sections";

  function readSectionState() {
    try {
      const raw = localStorage.getItem(SEC_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeSectionState(state) {
    try {
      localStorage.setItem(SEC_STORAGE_KEY, JSON.stringify(state));
    } catch (_) {
      /* private mode / quota — toggling still works for this page load */
    }
  }

  function setGroupOpen(group, open) {
    const toggle = group.querySelector("[data-admin-nav-toggle]");
    const body = group.querySelector(".admin-nav__group-body");
    if (!toggle || !body) return;
    body.hidden = !open;
    group.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  const groups = nav.querySelectorAll("[data-admin-nav-group]");
  const storedSections = readSectionState();

  groups.forEach((group) => {
    const key = group.dataset.adminNavGroup;

    // A stored preference wins over the server default — EXCEPT the active
    // section, which must never end up hidden regardless of what a previous
    // visit stored (the caller is standing in it right now).
    if (
      key !== ACTIVE_KEY &&
      Object.prototype.hasOwnProperty.call(storedSections, key)
    ) {
      setGroupOpen(group, !!storedSections[key]);
    }

    const toggle = group.querySelector("[data-admin-nav-toggle]");
    if (!toggle) return;
    toggle.addEventListener("click", () => {
      const body = group.querySelector(".admin-nav__group-body");
      const willOpen = !!(body && body.hidden);
      setGroupOpen(group, willOpen);
      const next = readSectionState();
      next[key] = willOpen;
      writeSectionState(next);
    });
  });

  // ---- Whole-sidebar collapse ---------------------------------------------
  const COLLAPSE_KEY = "agnes.adminNav.collapsed";
  const collapseToggle = nav.querySelector("[data-admin-nav-collapse-toggle]");

  function closeAllFlyouts() {
    nav.querySelectorAll("[data-admin-nav-flyout]").forEach((f) => {
      f.hidden = true;
    });
    nav.querySelectorAll("[data-admin-nav-rail-btn]").forEach((b) => {
      b.setAttribute("aria-expanded", "false");
    });
  }

  function applyCollapsed(collapsed) {
    nav.classList.toggle("is-collapsed", collapsed);
    if (collapseToggle) {
      collapseToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      collapseToggle.setAttribute(
        "aria-label",
        collapsed ? "Expand sidebar" : "Collapse sidebar",
      );
    }
    if (collapsed) closeAllFlyouts();
  }

  if (collapseToggle) {
    collapseToggle.addEventListener("click", () => {
      const collapsed = !nav.classList.contains("is-collapsed");
      applyCollapsed(collapsed);
      try {
        localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
      } catch (_) {
        /* private mode / quota */
      }
    });
  }

  // ---- Collapsed-mode flyouts ---------------------------------------------
  let openFlyoutKey = null;

  function closeFlyout() {
    if (!openFlyoutKey) return;
    const btn = nav.querySelector(`[data-admin-nav-rail-btn="${openFlyoutKey}"]`);
    const flyout = nav.querySelector(`[data-admin-nav-flyout="${openFlyoutKey}"]`);
    if (btn) btn.setAttribute("aria-expanded", "false");
    if (flyout) flyout.hidden = true;
    openFlyoutKey = null;
  }

  nav.querySelectorAll("[data-admin-nav-rail-btn]").forEach((btn) => {
    const key = btn.dataset.adminNavRailBtn;
    const flyout = nav.querySelector(`[data-admin-nav-flyout="${key}"]`);
    if (!flyout) return;
    btn.addEventListener("click", () => {
      const willOpen = openFlyoutKey !== key;
      closeFlyout();
      if (willOpen) {
        flyout.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        openFlyoutKey = key;
      }
    });
  });

  document.addEventListener("click", (e) => {
    if (openFlyoutKey && !nav.contains(e.target)) closeFlyout();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !openFlyoutKey) return;
    const btn = nav.querySelector(`[data-admin-nav-rail-btn="${openFlyoutKey}"]`);
    closeFlyout();
    if (btn) btn.focus();
  });

  // Apply a stored "collapsed" preference in case the inline bootstrap script
  // in `_admin_nav.html` couldn't run (e.g. a template test harness that
  // strips inline scripts) — a no-op when it already has.
  try {
    applyCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
  } catch (_) {
    /* private mode / quota — sidebar stays expanded */
  }
})();
