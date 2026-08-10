/* =====================================================================
 * ds_dropdown.js — the `.ds-dropdown` custom select-replacement (#1055).
 *
 * Generalizes the chat composer's "+" upload menu (chat.js `plusBtn` /
 * `plusMenu`: aria-expanded toggling, role="menu" + arrow-key nav, Enter/
 * Space to activate, Esc + outside-click to close) into a reusable
 * component. See `_components.html`'s `dropdown` macro for the markup and
 * `ds_dropdown.css` for the look.
 *
 * A dropdown paired to a native <select> (`data-ds-dropdown-target` on the
 * wrapper) sets `.value` on that select and dispatches a `change` event on
 * it when an item is chosen, so existing listeners on the select keep
 * working unchanged regardless of which UI (native or custom) is visible.
 *
 * Self-bootstraps on every `.ds-dropdown` present at load — no explicit
 * init call needed, same contract as chip-input.js.
 * ===================================================================== */
(function () {
  "use strict";

  function init(host) {
    const btn = host.querySelector(".ds-dropdown-btn");
    const menu = host.querySelector(".ds-dropdown-menu");
    if (!btn || !menu) return;
    const label = btn.querySelector(".ds-dropdown-btn-label");
    const items = Array.from(menu.querySelectorAll('[role="menuitemradio"]'));
    const targetId = host.getAttribute("data-ds-dropdown-target");
    const target = targetId ? document.getElementById(targetId) : null;

    function isOpen() {
      return !menu.hidden;
    }

    function close(restoreFocus) {
      if (!isOpen()) return;
      menu.hidden = true;
      btn.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
      if (restoreFocus) btn.focus();
    }

    function open() {
      if (isOpen()) return;
      menu.hidden = false;
      btn.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      const checked = items.find((i) => i.getAttribute("aria-checked") === "true");
      (checked || items[0] || btn).focus();
    }

    function selectItem(item) {
      const value = item.getAttribute("data-value");
      items.forEach((i) => {
        const selected = i === item;
        i.classList.toggle("is-selected", selected);
        i.setAttribute("aria-checked", selected ? "true" : "false");
      });
      if (label) label.textContent = item.textContent.trim();
      if (target) {
        target.value = value;
        target.dispatchEvent(new Event("change", { bubbles: true }));
      }
      host.dispatchEvent(new CustomEvent("ds-dropdown-change", { detail: { value: value }, bubbles: true }));
      close(true);
    }

    btn.addEventListener("click", () => {
      if (isOpen()) {
        close(false);
      } else {
        open();
      }
    });

    items.forEach((item) => {
      item.addEventListener("click", () => selectItem(item));
    });

    menu.addEventListener("keydown", (e) => {
      const idx = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (idx < items.length - 1) items[idx + 1].focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (idx > 0) items[idx - 1].focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (items[idx]) selectItem(items[idx]);
      } else if (e.key === "Escape") {
        // Stop here so an enclosing dialog's own Escape handler (bubble-
        // phase, listening on document) doesn't ALSO see this keypress and
        // close itself on the same press that was meant to just close this
        // menu — innermost-first dismissal.
        e.stopPropagation();
        close(true);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen()) close(true);
    });
    document.addEventListener("click", (e) => {
      if (!isOpen()) return;
      if (host.contains(e.target)) return;
      close(false);
    });
  }

  function bootstrapAll() {
    document.querySelectorAll(".ds-dropdown").forEach(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapAll);
  } else {
    bootstrapAll();
  }
})();
