/* Global UI helpers loaded by base.html (not by base_login.html — login pages
   have no nav and no toasts, so the helpers aren't reachable there).
   Two responsibilities for now:
   - wireDropdown: open/close + click-outside + Escape for the user menu and
     the Admin nav dropdown. Used by _app_header.html.
   - More helpers (window.appToast, etc.) added later as the design system
     primitives need JS sidecars. */
(function () {
    "use strict";

    function wireDropdown(triggerId, panelId) {
        var trigger = document.getElementById(triggerId);
        var panel = document.getElementById(panelId);
        if (!trigger || !panel) return;
        function setOpen(open) {
            trigger.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                panel.removeAttribute("hidden");
            } else {
                panel.setAttribute("hidden", "");
            }
        }
        trigger.addEventListener("click", function () {
            // No stopPropagation — let the click bubble to the document
            // handler so any OTHER open dropdown's handler can close
            // itself ("only one menu open at a time" behaviour).
            setOpen(trigger.getAttribute("aria-expanded") !== "true");
        });
        document.addEventListener("click", function (e) {
            // Use trigger.contains(target) instead of strict equality —
            // clicking the chevron <svg> inside the button reports the
            // svg / path as e.target, which would otherwise trip the
            // close branch immediately after opening.
            if (!panel.contains(e.target) && !trigger.contains(e.target)) {
                setOpen(false);
            }
        });
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                setOpen(false);
                trigger.focus();
            }
        });
    }

    // Toast helper — paired with .toast / .toast-container CSS in style-custom.css.
    // Usage: window.appToast({kind: "success", msg: "Saved", timeout: 4000})
    function ensureToastContainer() {
        var c = document.getElementById("appToastContainer");
        if (c) return c;
        c = document.createElement("div");
        c.id = "appToastContainer";
        c.className = "toast-container";
        document.body.appendChild(c);
        return c;
    }
    function appToast(opts) {
        opts = opts || {};
        var kind = opts.kind || "info";
        var msg = String(opts.msg || "");
        var timeout = opts.timeout == null ? 4000 : opts.timeout;
        var el = document.createElement("div");
        el.className = "toast is-" + kind;
        el.textContent = msg;
        el.addEventListener("click", function () { el.remove(); });
        ensureToastContainer().appendChild(el);
        if (timeout > 0) setTimeout(function () { el.remove(); }, timeout);
        return el;
    }

    // Theme toggle (Light / Dark / System) — the segmented control in the user
    // menu (_app_header.html). The pre-paint resolver in _theme_resolve.html owns
    // theme application + the single OS listener and exposes window.__agnesTheme;
    // this just drives it from clicks/keys and reflects the active choice. Clicks
    // stop propagation so the user menu (wired above) stays open while switching.
    function wireThemeToggle() {
        var group = document.getElementById("themeToggle");
        var theme = window.__agnesTheme;
        if (!group || !theme) return;
        var btns = Array.prototype.slice.call(
            group.querySelectorAll("[data-theme-choice]")
        );
        if (!btns.length) return;

        function reflect(choice) {
            btns.forEach(function (b) {
                var on = b.getAttribute("data-theme-choice") === choice;
                b.setAttribute("aria-checked", on ? "true" : "false");
                b.setAttribute("tabindex", on ? "0" : "-1");
                b.classList.toggle("is-active", on);
            });
        }
        function choose(choice, moveFocus) {
            theme.apply(choice, true);
            reflect(choice);
            if (moveFocus) {
                var sel = group.querySelector('[data-theme-choice="' + choice + '"]');
                if (sel) sel.focus();
            }
        }
        group.addEventListener("click", function (e) {
            var btn = e.target.closest && e.target.closest("[data-theme-choice]");
            if (!btn) return;
            e.stopPropagation();   // keep the user menu open while switching
            choose(btn.getAttribute("data-theme-choice"), false);
        });
        // Roving radiogroup: arrows move + activate the selection.
        group.addEventListener("keydown", function (e) {
            var i = btns.indexOf(document.activeElement);
            if (i === -1) return;
            var to = -1;
            if (e.key === "ArrowRight" || e.key === "ArrowDown") to = (i + 1) % btns.length;
            else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = (i - 1 + btns.length) % btns.length;
            if (to === -1) return;
            e.preventDefault();
            e.stopPropagation();
            choose(btns[to].getAttribute("data-theme-choice"), true);
        });
        reflect(theme.current());
    }

    window.appUI = { wireDropdown: wireDropdown };
    window.appToast = appToast;

    // Auto-wire the dropdowns + theme toggle shipped from _app_header.html.
    function init() {
        wireDropdown("userMenuTrigger", "userMenuPanel");
        wireDropdown("adminNavTrigger", "adminNavPanel");
        wireThemeToggle();
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
