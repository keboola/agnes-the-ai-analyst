/* Onboarding / guided-tour engine.
 *
 * A small, dependency-free spotlight tour that walks a signed-in user
 * through the primary navigation. The nav lives in `_app_header.html` and
 * renders on every authed page, so the whole tour runs in place on whatever
 * page the user is on — no cross-page state machine, no backend persistence.
 *
 * Steps are NOT defined here. They come from app/web/onboarding.py, filtered
 * by audience (admin vs non-admin) server-side, and injected into the page as
 * a JSON <script id="agnesOnboardingSteps">. This engine just renders them —
 * which is what lets the contract test guarantee they never go stale.
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
    var root = document.getElementById("agnesTour");
    if (!root) return;

    var els = {
        backdrop: root.querySelector(".agnes-tour__backdrop"),
        spot: root.querySelector(".agnes-tour__spot"),
        intro: root.querySelector(".agnes-tour__intro"),
        pop: root.querySelector(".agnes-tour__pop"),
        eyebrow: root.querySelector(".agnes-tour__eyebrow"),
        title: root.querySelector(".agnes-tour__title"),
        body: root.querySelector(".agnes-tour__body"),
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

    function render() {
        var step = active[idx];
        var el = step.anchor ? resolve(step) : null;

        els.eyebrow.textContent = idx === active.length - 1 ? "All set" : ("Step " + (idx + 1) + " of " + active.length);
        els.title.textContent = step.title || "";
        els.body.textContent = step.body || "";
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

    // Run the spotlight steps.
    function startSteps() {
        closeDropdowns();
        buildActive();
        if (!active.length) { hideOverlay(); return; }
        idx = 0;
        els.intro.hidden = true;
        els.pop.hidden = false;
        showOverlay();
        render();
        els.next.focus();
    }

    function end() {
        hideOverlay();
        markSeen();
    }

    function next() {
        if (idx >= active.length - 1) { end(); return; }
        idx++;
        render();
    }

    function back() {
        if (idx === 0) return;
        idx--;
        render();
    }

    // Wire controls.
    if (els.next) els.next.addEventListener("click", next);
    if (els.back) els.back.addEventListener("click", back);
    if (els.skip) els.skip.addEventListener("click", end);
    if (els.end) els.end.addEventListener("click", end);
    if (els.introStart) els.introStart.addEventListener("click", function () {
        markSeen();
        startSteps();
    });
    if (els.introSkip) els.introSkip.addEventListener("click", function () {
        markSeen();
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
                startSteps();
            });
        }
    );

    // Auto-show the consent modal once, on the first authed visit.
    if (!seen() && allSteps.length) {
        setTimeout(showIntro, 800);   // let the header settle before measuring
    }
})();
