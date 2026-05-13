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
        trigger.addEventListener("click", function (e) {
            e.stopPropagation();
            setOpen(trigger.getAttribute("aria-expanded") !== "true");
        });
        document.addEventListener("click", function (e) {
            if (!panel.contains(e.target) && e.target !== trigger) setOpen(false);
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

    window.appUI = { wireDropdown: wireDropdown };
    window.appToast = appToast;

    // Auto-wire the two dropdowns shipped from _app_header.html.
    function init() {
        wireDropdown("userMenuTrigger", "userMenuPanel");
        wireDropdown("adminNavTrigger", "adminNavPanel");
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
