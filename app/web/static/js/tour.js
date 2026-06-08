/* Onboarding / guided-tour engine.
 *
 * A small, dependency-free spotlight tour that walks a signed-in user
 * through the primary navigation. The nav lives in `_app_header.html` and
 * renders on every authed page, so every step resolves on whatever page the
 * user lands on.
 *
 * Steps are NOT defined here. They come from app/web/onboarding.py, filtered
 * by audience (admin vs non-admin) server-side, and injected into the page as
 * a JSON <script id="agnesOnboardingSteps">. This engine just renders them —
 * which is what lets the contract test guarantee they never go stale.
 *
 * Cross-page walkthrough: each step carries a `route`. Clicking Next/Back
 * navigates to the step's page when it differs from the current one, stashing
 * the tour position in sessionStorage so the engine resumes mid-walk after the
 * reload. There is no backend persistence — sessionStorage is per-tab and
 * cleared the moment the tour ends.
 *
 * Flow:
 *   • First authed visit (no `seen` flag) → the intro consent modal pops once.
 *       "Show me around" → run the spotlight steps.  "Not now" → dismiss.
 *     Either choice sets the `seen` flag, so it never auto-pops again.
 *   • Re-open: any [data-tour-start] element (header help icon, profile page)
 *     starts the spotlight steps directly, skipping the consent modal.
 *
 * Each step can be skipped or ended (Skip / ✕ / Esc). Steps whose target
 * anchor is absent for the viewer (e.g. Chat without a grant) are dropped.
 */
(function () {
    "use strict";

    var SEEN_KEY = "agnes-onboarding-v1-seen";
    var RUN_KEY = "agnes-onboarding-v1-run";   // sessionStorage: resume position across page nav
    var root = document.getElementById("agnesTour");
    if (!root) return;

    var reduceMotion = false;
    try {
        reduceMotion = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { reduceMotion = false; }

    var els = {
        backdrop: root.querySelector(".agnes-tour__backdrop"),
        spot: root.querySelector(".agnes-tour__spot"),
        intro: root.querySelector(".agnes-tour__intro"),
        pop: root.querySelector(".agnes-tour__pop"),
        bar: root.querySelector(".agnes-tour__bar-fill"),
        icon: root.querySelector(".agnes-tour__icon"),
        eyebrow: root.querySelector(".agnes-tour__eyebrow"),
        title: root.querySelector(".agnes-tour__title"),
        body: root.querySelector(".agnes-tour__body"),
        tips: root.querySelector(".agnes-tour__tips"),
        dots: root.querySelector(".agnes-tour__dots"),
        skip: root.querySelector('[data-act="skip"]'),
        back: root.querySelector('[data-act="back"]'),
        next: root.querySelector('[data-act="next"]'),
        end: root.querySelector('[data-act="end"]'),
        introStart: root.querySelector('[data-act="intro-start"]'),
        introSkip: root.querySelector('[data-act="intro-skip"]')
    };

    // Steps injected by the server (already audience-filtered). Parse once.
    var allSteps = [];
    try {
        var raw = document.getElementById("agnesOnboardingSteps");
        if (raw) allSteps = JSON.parse(raw.textContent) || [];
    } catch (e) { allSteps = []; }

    var active = [];   // steps whose target resolves (or is target-less)
    var idx = 0;

    function seen() {
        try { return !!localStorage.getItem(SEEN_KEY); } catch (e) { return true; }
    }
    function markSeen() {
        try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {}
    }

    // sessionStorage run-state: { idx } while a cross-page navigation is in flight.
    function saveRun(at) {
        try { sessionStorage.setItem(RUN_KEY, JSON.stringify({ idx: at })); } catch (e) {}
    }
    function readRun() {
        try {
            var v = sessionStorage.getItem(RUN_KEY);
            if (!v) return null;
            var o = JSON.parse(v);
            return (o && typeof o.idx === "number") ? o.idx : null;
        } catch (e) { return null; }
    }
    function clearRun() {
        try { sessionStorage.removeItem(RUN_KEY); } catch (e) {}
    }

    // Compare a step.route against where we actually are. Trailing slashes and
    // an empty/`/` home both normalize so we don't navigate to the page we're on.
    function normPath(p) {
        if (!p) return "";
        p = p.split("?")[0].split("#")[0];
        if (p.length > 1 && p.charAt(p.length - 1) === "/") p = p.slice(0, -1);
        return p;
    }
    function onStepPage(step) {
        if (!step || !step.route) return true;   // routeless step renders in place
        return normPath(step.route) === normPath(window.location.pathname);
    }

    function resolve(step) {
        if (!step.anchor) return null;            // target-less (centered) step
        var el = document.querySelector('[data-tour="' + step.anchor + '"]');
        if (!el) return false;
        // Treat off-screen / display:none anchors as absent.
        if (el.offsetParent === null && el.getClientRects().length === 0) return false;
        return el;
    }

    function buildActive() {
        active = allSteps.filter(function (s) {
            return !s.anchor || resolve(s) !== false;
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
        var pw = pop.offsetWidth, ph = pop.offsetHeight, gap = 14;
        var vw = window.innerWidth, vh = window.innerHeight;
        var top, left;
        if (rect.bottom + gap + ph <= vh) {
            top = rect.bottom + gap;
        } else if (rect.top - gap - ph >= 0) {
            top = rect.top - gap - ph;
        } else {
            top = Math.max(gap, (vh - ph) / 2);
        }
        left = Math.min(rect.left, vw - pw - gap);
        left = Math.max(gap, left);
        pop.style.top = top + "px";
        pop.style.left = left + "px";
    }

    // Re-trigger the card's entrance animation each render (unless reduced motion).
    function animateCard() {
        if (reduceMotion) return;
        els.pop.classList.remove("is-enter");
        // force reflow so the class re-add restarts the keyframe
        void els.pop.offsetWidth;
        els.pop.classList.add("is-enter");
    }

    function render() {
        var step = active[idx];
        var el = step.anchor ? resolve(step) : null;
        var last = idx === active.length - 1;

        els.icon.textContent = step.icon || "";
        els.icon.hidden = !step.icon;
        els.eyebrow.textContent = last ? "All set" : ("Step " + (idx + 1) + " of " + active.length);
        els.title.textContent = step.title || "";
        els.body.textContent = step.body || "";
        if (els.tips) {
            els.tips.innerHTML = "";
            var tips = step.tips || [];
            for (var t = 0; t < tips.length; t++) {
                var li = document.createElement("li");
                li.textContent = tips[t];
                els.tips.appendChild(li);
            }
            els.tips.hidden = tips.length === 0;
        }
        els.back.disabled = idx === 0;
        els.next.textContent = last ? "Done" : "Next";
        if (els.bar) els.bar.style.width = Math.round(((idx + 1) / active.length) * 100) + "%";
        renderDots();
        animateCard();

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
        if (root.hidden || els.pop.hidden) return;
        render();
    }

    function closeDropdowns() {
        Array.prototype.forEach.call(
            document.querySelectorAll(".app-user-menu-panel, .app-nav-menu-panel"),
            function (p) { p.hidden = true; }
        );
        Array.prototype.forEach.call(
            document.querySelectorAll('[aria-haspopup="menu"]'),
            function (t) { t.setAttribute("aria-expanded", "false"); }
        );
    }

    function showOverlay() {
        root.hidden = false;
        window.addEventListener("resize", reposition);
        window.addEventListener("scroll", reposition, true);
    }

    function hideOverlay() {
        root.hidden = true;
        els.intro.hidden = true;
        els.pop.hidden = true;
        els.spot.hidden = true;
        window.removeEventListener("resize", reposition);
        window.removeEventListener("scroll", reposition, true);
    }

    // Intro consent modal (first-visit welcome).
    function showIntro() {
        if (!els.intro) { startSteps(); return; }
        closeDropdowns();
        els.pop.hidden = true;
        els.spot.hidden = true;
        els.backdrop.classList.add("is-dim");
        els.intro.hidden = false;
        showOverlay();
        if (els.introStart) els.introStart.focus();
    }

    // Run the spotlight steps. `at` lets resume() jump to a saved position.
    function startSteps(at) {
        // Once a tour begins by any path, never auto-pop the intro again — even
        // if a cross-page nav loses the run flag on the destination page.
        markSeen();
        closeDropdowns();
        buildActive();
        if (!active.length) { hideOverlay(); return; }
        idx = Math.max(0, Math.min(typeof at === "number" ? at : 0, active.length - 1));
        els.intro.hidden = true;
        els.pop.hidden = false;
        showOverlay();
        render();
        els.next.focus();
    }

    function end() {
        clearRun();
        hideOverlay();
        markSeen();
    }

    // Move to `target` index, navigating across pages when the destination
    // step lives on another route (resuming there via sessionStorage).
    function goTo(target) {
        if (target < 0 || target > active.length - 1) { end(); return; }
        var step = active[target];
        if (!onStepPage(step)) {
            saveRun(target);
            window.location.assign(step.route);
            return;
        }
        idx = target;
        render();
    }

    function next() {
        if (idx >= active.length - 1) { end(); return; }
        goTo(idx + 1);
    }

    function back() {
        if (idx === 0) return;
        goTo(idx - 1);
    }

    // Wire controls.
    if (els.next) els.next.addEventListener("click", next);
    if (els.back) els.back.addEventListener("click", back);
    if (els.skip) els.skip.addEventListener("click", end);
    if (els.end) els.end.addEventListener("click", end);
    if (els.introStart) els.introStart.addEventListener("click", function () {
        startSteps();
    });
    if (els.introSkip) els.introSkip.addEventListener("click", function () {
        markSeen();
        clearRun();
        hideOverlay();
    });

    document.addEventListener("keydown", function (e) {
        if (root.hidden) return;
        if (e.key === "Escape") { e.preventDefault(); end(); return; }
        if (!els.pop.hidden) {  // step navigation only while a step is showing
            if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); next(); }
            else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
        }
    });

    // Manual launchers (header help icon, profile page button). Re-opening
    // skips the consent modal and goes straight into the spotlight.
    Array.prototype.forEach.call(
        document.querySelectorAll("[data-tour-start]"),
        function (btn) {
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                clearRun();
                startSteps(0);
            });
        }
    );

    // Resume a cross-page walk if one is in flight; otherwise auto-show the
    // consent modal once on the first authed visit.
    var resumeAt = readRun();
    if (resumeAt !== null && allSteps.length) {
        clearRun();   // consume immediately so a manual refresh doesn't relaunch
        // Let the header + page settle before measuring the target rect.
        setTimeout(function () { startSteps(resumeAt); }, reduceMotion ? 0 : 250);
    } else if (!seen() && allSteps.length) {
        setTimeout(showIntro, 800);   // let the header settle before measuring
    }
})();
