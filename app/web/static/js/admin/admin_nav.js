// admin_nav.js — per-section disclosure for the /admin sidebar
// (`_admin_nav.html`), persisted per browser in localStorage.
//
// Server-side rendering already gets FIRST PAINT right — see the comment
// block at the top of `_admin_nav.html`: the active section's body has no
// `hidden` attribute and its header's `aria-expanded` is already "true";
// every other section is already collapsed.
//
// This file's job is the one thing that HTML response could not have known: a
// caller's own manually-opened/closed sections, which may disagree with the
// server's "just the active one" default.
//
// The whole-sidebar collapse this file used to own (a 56px icon strip with
// hover flyouts, the `agnes.adminNav.collapsed` key, and an inline
// first-paint script inside the aside) is gone — the column does not collapse
// any more. The primary rail beside it owns that behaviour for the window.
(function () {
  "use strict";

  const nav = document.querySelector("[data-admin-nav]");
  if (!nav) return;

  const ACTIVE_KEY = nav.dataset.activeSection || "";
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
})();
