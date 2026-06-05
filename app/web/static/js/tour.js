/* Walking guide / product tour engine.
 *
 * A small, dependency-free spotlight tour that walks a signed-in user
 * through the primary navigation. The nav lives in `_app_header.html`
 * and renders on every authed page, so the whole tour runs in place on
 * whatever page the user happens to be on — no cross-page state machine,
 * no backend persistence. Progress + "already seen" live in
 * localStorage, mirroring the existing `agnes-admin-nav-*` pattern.
 *
 * Entry points:
 *   • Auto: first authed visit (no `seen` flag) → starts after a beat.
 *   • Manual: any element with [data-tour-start] (user menu item).
 *
 * Steps target [data-tour="<key>"] anchors in the header. Steps whose
 * target is absent (e.g. Chat link hidden without a grant) are skipped
 * automatically, so the same step list works for every user.
 */
(function () {
    "use strict";

    var SEEN_KEY = "agnes-tour-v1-seen";
    var root = document.getElementById("agnesTour");
    if (!root) return;

    var instance = root.getAttribute("data-instance") || "Agnes";

    // Step list. `target` is a [data-tour] key or null for a centered card.
    var STEPS = [
        {
            target: null,
            eyebrow: "Quick tour",
            title: "Welcome to " + instance,
            body: "Take 30 seconds to see how things are laid out. You can skip anytime and reopen this from your profile menu."
        },
        {
            target: "nav-home",
            eyebrow: "Step",
            title: "Home",
            body: "Your starting point — a dashboard of what's available to you and shortcuts to get going."
        },
        {
            target: "nav-chat",
            eyebrow: "Step",
            title: "Chat",
            body: "Ask questions about your data in natural language, right in the browser."
        },
        {
            target: "nav-marketplace",
            eyebrow: "Step",
            title: "Marketplace",
            body: "Discover skills and plugins that extend what your AI agent can do — install them into your workspace."
        },
        {
            target: "nav-catalog",
            eyebrow: "Step",
            title: "Data Packages",
            body: "Browse the datasets you have access to. Each package shows its tables, schema, and how to query it locally."
        },
        {
            target: "nav-memory",
            eyebrow: "Step",
            title: "Memory",
            body: "Shared organizational knowledge — canonical metric definitions and business rules your agent should follow."
        },
        {
            target: "user-menu",
            eyebrow: "Step",
            title: "Your menu",
            body: "Your profile, AI Cowork setup, recent activity, and sign-out all live here."
        },
        {
            target: null,
            eyebrow: "All set",
            title: "You're ready to go",
            body: "That's the lay of the land. Reopen this tour anytime from your profile menu → “Take a tour”."
        }
    ];

    var els = {
        backdrop: root.querySelector(".agnes-tour__backdrop"),
        spot: root.querySelector(".agnes-tour__spot"),
        pop: root.querySelector(".agnes-tour__pop"),
        eyebrow: root.querySelector(".agnes-tour__eyebrow"),
        title: root.querySelector(".agnes-tour__title"),
        body: root.querySelector(".agnes-tour__body"),
        dots: root.querySelector(".agnes-tour__dots"),
        skip: root.querySelector('[data-act="skip"]'),
        back: root.querySelector('[data-act="back"]'),
        next: root.querySelector('[data-act="next"]')
    };

    var active = [];   // steps whose target resolves (or is null)
    var idx = 0;

    function resolve(step) {
        if (!step.target) return null;
        var el = document.querySelector('[data-tour="' + step.target + '"]');
        // Treat off-screen / display:none anchors as absent.
        if (el && el.offsetParent === null && el.getClientRects().length === 0) {
            return false;
        }
        return el || false;
    }

    function buildActive() {
        active = STEPS.filter(function (s) {
            return s.target === null || resolve(s) !== false;
        });
    }

    function renderDots() {
        els.dots.innerHTML = "";
        for (var i = 0; i < active.length; i++) {
            var d = document.createElement("span");
            d.className = "agnes-tour__dot" + (i === idx ? " is-on" : "");
            els.dots.appendChild(d);
        }
    }

    function placePop(rect) {
        var pop = els.pop;
        pop.classList.remove("is-centered");
        // Measure after content is set.
        var pw = pop.offsetWidth;
        var ph = pop.offsetHeight;
        var gap = 14;
        var vw = window.innerWidth;
        var vh = window.innerHeight;

        var top, left;
        // Prefer below the target; flip above if it would overflow.
        if (rect.bottom + gap + ph <= vh) {
            top = rect.bottom + gap;
        } else if (rect.top - gap - ph >= 0) {
            top = rect.top - gap - ph;
        } else {
            top = Math.max(gap, (vh - ph) / 2);
        }
        // Align left edge to target, clamped to viewport.
        left = rect.left;
        left = Math.min(left, vw - pw - gap);
        left = Math.max(gap, left);

        pop.style.top = top + "px";
        pop.style.left = left + "px";
    }

    function render() {
        var step = active[idx];
        var el = step.target ? resolve(step) : null;

        els.eyebrow.textContent = step.eyebrow || "";
        els.title.textContent = step.title;
        els.body.textContent = step.body;

        els.back.disabled = idx === 0;
        els.next.textContent = idx === active.length - 1 ? "Done" : "Next";

        renderDots();

        if (el && el.getBoundingClientRect) {
            el.scrollIntoView({ block: "nearest", inline: "nearest" });
            var r = el.getBoundingClientRect();
            var pad = 6;
            els.spot.hidden = false;
            els.backdrop.classList.remove("is-dim");
            els.spot.style.top = (r.top - pad) + "px";
            els.spot.style.left = (r.left - pad) + "px";
            els.spot.style.width = (r.width + pad * 2) + "px";
            els.spot.style.height = (r.height + pad * 2) + "px";
            placePop(r);
        } else {
            // Target-less step → dim via backdrop, center the card.
            els.spot.hidden = true;
            els.backdrop.classList.add("is-dim");
            els.pop.classList.add("is-centered");
            els.pop.style.top = "";
            els.pop.style.left = "";
        }
    }

    function reposition() {
        if (root.hidden) return;
        render();
    }

    function open() {
        // Close any open header dropdowns so they don't overlap the spot.
        Array.prototype.forEach.call(
            document.querySelectorAll(".app-user-menu-panel, .app-nav-menu-panel"),
            function (p) { p.hidden = true; }
        );
        Array.prototype.forEach.call(
            document.querySelectorAll('[aria-haspopup="menu"]'),
            function (t) { t.setAttribute("aria-expanded", "false"); }
        );

        buildActive();
        if (!active.length) return;
        idx = 0;
        root.hidden = false;
        render();
        els.next.focus();
        window.addEventListener("resize", reposition);
        window.addEventListener("scroll", reposition, true);
    }

    function close() {
        root.hidden = true;
        window.removeEventListener("resize", reposition);
        window.removeEventListener("scroll", reposition, true);
        try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {}
    }

    function next() {
        if (idx >= active.length - 1) { close(); return; }
        idx++;
        render();
    }

    function back() {
        if (idx === 0) return;
        idx--;
        render();
    }

    els.next.addEventListener("click", next);
    els.back.addEventListener("click", back);
    els.skip.addEventListener("click", close);

    document.addEventListener("keydown", function (e) {
        if (root.hidden) return;
        if (e.key === "Escape") { e.preventDefault(); close(); }
        else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); next(); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
    });

    // Manual launchers.
    Array.prototype.forEach.call(
        document.querySelectorAll("[data-tour-start]"),
        function (btn) {
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                open();
            });
        }
    );

    // Auto-start on first authed visit.
    var seen;
    try { seen = localStorage.getItem(SEEN_KEY); } catch (e) { seen = "1"; }
    if (!seen) {
        // Let the header settle (fonts, dropdown wiring) before measuring.
        setTimeout(open, 800);
    }
})();
