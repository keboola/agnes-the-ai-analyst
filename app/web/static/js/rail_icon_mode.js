// rail_icon_mode.js — the icon-only global rail on /admin/* pages (rail
// ui_layout). Hover and keyboard `:focus-within` already expand the
// collapsed rail via CSS alone (see the `.rail-icon-mode` rules in
// rail.css) — this file exists only for what CSS can't do by itself:
//
//   - a tap/click affordance for touch, which has no hover event at all
//     (`#rail-icon-toggle`, rendered only in icon mode);
//   - Escape, which first has to move focus OUT of the rail before
//     `:focus-within` will let go and the rail can collapse back to icons.
//
// Self-guards on the icon-mode rail being present, so loading it only when
// `_admin_page` is true (see _app_rail.html) is a belt-and-braces no-op
// everywhere else.
(function () {
  "use strict";

  const rail = document.querySelector(".rail.rail-icon-mode");
  if (!rail) return;

  const toggle = document.getElementById("rail-icon-toggle");

  function setPinned(pinned) {
    rail.classList.toggle("rail-pinned-open", pinned);
    if (toggle) {
      toggle.setAttribute("aria-expanded", pinned ? "true" : "false");
      toggle.setAttribute("aria-label", pinned ? "Collapse navigation" : "Expand navigation");
    }
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      setPinned(!rail.classList.contains("rail-pinned-open"));
    });
  }

  // Tapping/clicking outside the rail while it's pinned open closes it again
  // — mirrors the admin sidebar's own outside-click flyout dismissal
  // (admin_nav.js).
  document.addEventListener("click", (e) => {
    if (rail.classList.contains("rail-pinned-open") && !rail.contains(e.target)) {
      setPinned(false);
    }
  });

  // Escape: un-pin, and if focus is still inside the (about to collapse)
  // rail, blur it — otherwise `:focus-within` alone would keep the rail
  // expanded regardless of the pin state, and the caller's focus would be
  // left sitting on a row that Escape just visually collapsed out from
  // under it.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const focusWasInside = rail.contains(document.activeElement);
    if (!rail.classList.contains("rail-pinned-open") && !focusWasInside) return;
    setPinned(false);
    if (focusWasInside) document.activeElement.blur();
  });
})();
