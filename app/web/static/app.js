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

    // Priority-plus navigation — keeps the primary nav on a single row and
    // moves the lowest-priority links (from the end: Memory first, back toward
    // Dashboard) into a "More" overflow menu when they'd otherwise overflow,
    // BEFORE anything shrinks or clips. Progressive enhancement: without this
    // the nav simply wraps (CSS fallback). Markup: #primaryNav > a.app-nav-link
    // items + a trailing #navMore (button #navMoreTrigger, panel #navMorePanel).
    function initPriorityNav() {
        var nav = document.getElementById("primaryNav");
        var more = document.getElementById("navMore");
        var panel = document.getElementById("navMorePanel");
        var trigger = document.getElementById("navMoreTrigger");
        if (!nav || !more || !panel || !trigger) return;

        // Managed links in priority order (DOM order; last = lowest priority).
        var items = Array.prototype.slice.call(
            nav.querySelectorAll(":scope > a.app-nav-link")
        );
        if (!items.length) return;

        nav.classList.add("is-priority");   // CSS: single measured row, no wrap

        function reflectActive() {
            // Keep the selected state visible when the active page's link is
            // tucked inside the overflow menu.
            trigger.classList.toggle(
                "is-active", panel.querySelector(".is-active") != null
            );
        }

        var laying = false;   // re-entrancy guard (layout mutates the DOM, which
                              // can re-trigger the ResizeObserver below).
        function layout() {
            if (laying) return;
            laying = true;

            // 1. Reset — every managed link back inline, just before #navMore.
            items.forEach(function (a) { nav.insertBefore(a, more); });
            more.hidden = true;

            // 2. Fits as-is? Done.
            if (nav.scrollWidth <= nav.clientWidth) {
                reflectActive();
                laying = false;
                return;
            }

            // 3. Reveal More (it consumes row width) and move items from the
            //    end into the panel until the inline row fits.
            more.hidden = false;
            var i = items.length - 1;
            while (i >= 0 && nav.scrollWidth > nav.clientWidth) {
                panel.insertBefore(items[i], panel.firstChild);
                i--;
            }
            if (!panel.children.length) more.hidden = true;
            reflectActive();
            laying = false;
        }

        // Re-run whenever the header's width changes. ResizeObserver fires
        // reliably (unlike a resize+rAF path in backgrounded tabs) and also
        // catches container-driven width changes that aren't window resizes.
        var host = nav.closest(".app-header") || nav;
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(function () { layout(); }).observe(host);
        } else {
            window.addEventListener("resize", layout);
        }
        layout();
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
        // Admin is a first-class header entry (mega-menu), no longer buried in
        // the user menu. The primary nav carries a "More" overflow menu.
        wireDropdown("adminMenuTrigger", "adminMenuPanel");
        wireDropdown("navMoreTrigger", "navMorePanel");
        initPriorityNav();
        wireThemeToggle();
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
