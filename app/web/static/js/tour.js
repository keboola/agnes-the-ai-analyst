/**
 * tour.js — Config-driven, non-blocking coach-mark engine.
 *
 * Cross-page design: The real app is multi-page (not a SPA). When a step's
 * `page` differs from the current pathname, the engine stashes
 * {id, index} in sessionStorage under 'agnes.tour.pending' and navigates.
 * On load, every page checks for a pending tour whose step[index].page
 * matches window.location.pathname and resumes automatically.
 *
 * State:
 *   localStorage  'agnes.tour.<id>.seen'  — don't auto-launch a seen tour
 *   sessionStorage 'agnes.tour.pending'   — cross-page resume
 *                                           {id, index, ts, skipped?}
 *
 * Usage:
 *   import { launchTour, TOURS } from './tour.js';
 *   launchTour('stack');          // start from step 0
 *   launchTour('stack', 2);       // resume at index 2
 */

// ── Tour definitions ────────────────────────────────────────────────────────

export const TOURS = {
  stack: [
    // Step 0: /stack — the tabs + cards zone
    {
      page: '/stack',
      selector: '#stack-explore-zone',
      title: 'This is your Stack',
      desc: 'Your Stack is everything I can draw on to answer you — all your knowledge sources and capabilities, in one place.',
      points: [
        'Switch between Data, Plugins, Memory, and Uploads with the tabs.',
        'Locked cards were set up by your admin — I use them automatically, and you can\'t remove them.',
      ],
    },
    // Step 1: /stack — the Artefacts nav (private uploads live under Artefacts).
    {
      page: '/stack',
      selector: '#nav-artefacts',
      title: 'Add your own files',
      desc: 'Your private files live under Artefacts — upload a document only you need (a pricing deck, a spec, meeting notes) and I\'ll search it and cite it in my answers. No admin needed.',
      points: [
        'Works with PDF, Markdown, TXT, and more.',
        'Private to you unless you choose to share it.',
      ],
    },
    // Step 2: /stack — Marketplace nav item (in rail). Framed as the community
    // sharing surface: see what colleagues built, add it, and share back.
    {
      page: '/stack',
      selector: '#nav-catalog',
      title: 'Here\'s the Marketplace',
      desc: 'See what your colleagues have already built and shared — data packages, skills, and plugins you can add to your Stack in one click. Spot a gap? Build something useful and share it back for the whole team.',
      points: [
        'When a question needs something you don\'t have yet, I\'ll usually point you here right in the chat.',
      ],
    },
    // Step 3: /catalog — the submit CTA
    {
      page: '/catalog',
      selector: '#browse-submit-btn',
      title: 'Share what you build',
      desc: 'Built a skill or plugin your team could reuse? Publish it here so others can add it from the Marketplace.',
      points: [
        'Publishing to the Flea Market is separate from adding something to your own Stack.',
      ],
    },
    // Step 4: final centered step — no anchor
    {
      page: '/catalog',
      centered: true,
      finalChoice: true,
      title: 'That\'s the tour',
      desc: 'You can set up your Stack and add what I need by prompting me in chat. Want the same company context in Claude Code, Cursor, and VS Code? Use Connect my AI tools below.',
      points: [],
    },
  ],

  // Single-step coach-mark for authors arriving from the Marketplace's
  // "Submit a skill or plugin" CTA (/skills?spotlight=new-skill). The Skills
  // grid opens on the caller's existing skills, so the one card that starts
  // authoring needs pointing at. One step → the popover renders in its solo
  // form (no dots, no "I'll explore on my own", primary reads "Got it").
  'skill-builder': [
    {
      page: '/skills',
      // /skills IS the builder now (the separate "your skills" index was
      // retired when created skills started landing in the Library), so the
      // coach-mark anchors on the first field instead of a "+ New skill" card.
      selector: '[data-sk-field="name"]',
      title: 'Start here',
      desc: 'This is where a skill begins — a name, a line on when to use it, and the instructions themselves.',
      points: [
        'Choose who can use it: just you, or everyone in the organization.',
        'Save to Library and it appears in your Library, ready to share.',
      ],
    },
  ],
};

// ── Persistence helpers ─────────────────────────────────────────────────────

const PENDING_KEY = 'agnes.tour.pending';
// Only auto-resume a pending record written within this window. A cross-page
// tour hop stashes then navigates and the destination loads within seconds; a
// stale record (the user abandoned the tour via a normal link) must not re-pop
// the tour on later /stack or /catalog visits.
const RESUME_FRESH_MS = 12000;

function seenKey(id) {
  return `agnes.tour.${id}.seen`;
}

function isSeen(id) {
  try {
    return !!localStorage.getItem(seenKey(id));
  } catch (_) {
    return false;
  }
}

function markSeen(id) {
  try {
    localStorage.setItem(seenKey(id), '1');
  } catch (_) { /* storage unavailable — non-fatal */ }
}

function stashPending(id, index, skipped) {
  try {
    // `skipped` rides along so steps dropped for a missing anchor stay dropped
    // across the tour's cross-page hops — otherwise the progress dots would
    // re-count them after every navigation.
    const rec = { id, index, ts: Date.now() };
    if (skipped && skipped.size) rec.skipped = Array.from(skipped);
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(rec));
  } catch (_) { /* storage unavailable — non-fatal */ }
}

function clearPending() {
  try {
    sessionStorage.removeItem(PENDING_KEY);
  } catch (_) {}
}

function getPending() {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

// ── Journey patch helper (fire-and-forget) ──────────────────────────────────
// Mark ONLY the journey steps the tour genuinely walks the user through.
//
// The tour visits My Stack (explored_stack) and the Marketplace
// (catalog_discovered), so it may assert those. It must NOT touch the
// activity-driven flags — chat_onboarding.js deliberately leaves those to real
// activity: `first_asked` completes when a question is asked, and
// `stack_setup_done` when a package is actually subscribed. `stack_setup_done`
// additionally gates the in-chat gap resolver, so asserting it here would
// permanently suppress the "your Stack is empty, here's what I'd add" card for
// anyone who takes the tour before asking their first question.
//
// `use_anywhere` is likewise left alone: the final step OFFERS "Connect my AI
// tools" but finishing the tour is not the same as having connected one.
async function markTourStepsDone() {
  try {
    await fetch('/api/chat/journey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        explored_stack: true,
        catalog_discovered: true,
      }),
    });
  } catch (_) { /* fire-and-forget — onboarding is soft state */ }
}

// Mark the "Use Agnes from other AI tools" step — fired when the final step's
// "Connect my AI tools" button navigates to the AI Connector page.
async function markUseAnywhereDone() {
  try {
    // keepalive: the one call site fires this immediately before a full-page
    // navigation (`window.location.href = '/me/ai-connector'`); without it
    // the browser cancels the in-flight PUT and the journey step stays
    // unmarked (Devin Review on #1092). keepalive lets the write survive the
    // page teardown while keeping the call fire-and-forget.
    await fetch('/api/chat/journey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_anywhere: true }),
      keepalive: true,
    });
  } catch (_) { /* fire-and-forget — onboarding is soft state */ }
}

// ── Engine ──────────────────────────────────────────────────────────────────

let _active = null; // { id, steps, index, overlay, popover, spotlight }

/** Public: launch a named tour at the given step index (default 0). */
export function launchTour(id, index = 0) {
  const steps = TOURS[id];
  if (!steps || !steps.length) return;

  const step = steps[index];
  if (!step) return;

  // If this step lives on a different page, stash + navigate.
  const currentPath = window.location.pathname;
  if (!pathMatches(step.page, currentPath)) {
    stashPending(id, index);
    window.location.href = step.page;
    return;
  }

  // Same page — render.
  _startTour(id, steps, index);
}

function pathMatches(page, path) {
  // Normalize both: strip trailing slash, compare exact pathname.
  const norm = (p) => p.replace(/\/$/, '') || '/';
  return norm(page) === norm(path);
}

function _startTour(id, steps, index, skipped) {
  _endTour(false); // clean up any prior run
  // Wire Escape + resize/scroll reflow for EVERY launch path (direct
  // launchTour on the current page as well as resumePendingTour). Guarded by
  // _listenersAttached so it's idempotent.
  _attachListeners();

  const overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  document.body.appendChild(overlay);

  // `skipped` collects indices whose anchor turned out to be absent on their
  // page (permission-gated CTAs, layout-dependent nav items). The progress dots
  // are built from the steps that actually render, so the count stays honest
  // instead of promising a "Step 4 of 5" that never appears. It can only grow
  // as the tour walks forward — a step on a page not yet visited is assumed
  // renderable until proven otherwise. `liftedAncestor` tracks a stacking-
  // context ancestor (e.g. the rail) temporarily promoted above the scrim so
  // a spotlighted descendant is reachable — see _findStackingContextAncestor.
  _active = {
    id, steps, index: null, overlay, popover: null, spotlight: null,
    skipped: new Set(skipped || []),
    liftedAncestor: null,
  };
  _showStep(index);
}

function _showStep(index) {
  if (!_active) return;
  const { id, steps, overlay } = _active;
  const step = steps[index];
  if (!step) { _endTour(true); return; }

  // Clear previous popover + spotlight.
  if (_active.popover) _active.popover.remove();
  if (_active.spotlight) {
    _active.spotlight.classList.remove('tour-spotlight');
    _active.spotlight = null;
  }
  if (_active.liftedAncestor) {
    _active.liftedAncestor.classList.remove('tour-lifts-ancestor');
    _active.liftedAncestor = null;
  }

  _active.index = index;

  // Resolve anchor element (skip forward on miss).
  let anchor = null;
  if (!step.centered && step.selector) {
    anchor = document.querySelector(step.selector);
    if (!anchor) {
      // Resilient: the anchor is absent here. Record it so the progress dots
      // stop counting a step the user will never see, then route through
      // _gotoStep so a next step on another page cross-navigates instead of
      // recursing on the current page (which would blow through cross-page
      // steps to the final screen).
      _active.skipped.add(index);
      _gotoStep(index + 1);
      return;
    }
    // Scroll target into view — horizontally only "nearest" so a target near
    // the viewport edge (e.g. a nav item in the rail's mobile top-bar layout)
    // never induces horizontal scroll of the page itself (see the
    // rail-overflow note on _positionPopover's horizontal clamp).
    anchor.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    anchor.classList.add('tour-spotlight');
    _active.spotlight = anchor;

    // If the anchor sits inside a positioned ancestor that has its own
    // z-index (a stacking context — e.g. the rail: `position: fixed;
    // z-index: 40`), `.tour-spotlight`'s z-index resolves INSIDE that
    // context and can never outrank the scrim, which paints at the root.
    // Lift the ancestor above the scrim for the step's duration so the
    // spotlighted target is actually reachable through it.
    const stackingAncestor = _findStackingContextAncestor(anchor);
    if (stackingAncestor) {
      stackingAncestor.classList.add('tour-lifts-ancestor');
      _active.liftedAncestor = stackingAncestor;
    }
  }

  // Keep the resume record in sync with the step actually on screen (not just
  // cross-page hops), so a reload mid-tour resumes here — and re-stamp its
  // freshness. Combined with the RESUME_FRESH_MS window, an abandoned tour
  // stops re-popping once the record goes stale.
  stashPending(_active.id, index, _active.skipped);

  // Build popover.
  const popover = _buildPopover(step, index, steps.length);
  document.body.appendChild(popover);
  _active.popover = popover;

  // Position once the scroll above has actually settled — `scrollIntoView`
  // with `behavior: 'smooth'` takes ~300-500ms, while a single
  // requestAnimationFrame fires in ~16ms. Positioning on the very next frame
  // reads the anchor's PRE-scroll rect, so the popover lands wherever the
  // anchor used to be and is never corrected once the scroll finishes. Wait
  // for the anchor's rect to stop moving instead of guessing a fixed delay.
  _waitForScrollSettle(anchor, () => {
    if (_active && _active.index === index) {
      _positionPopover(popover, anchor, step.centered);
    }
  });
}

// Resolve once `anchor`'s bounding rect is unchanged across two consecutive
// animation frames (the scroll driving it has settled), or after
// SCROLL_SETTLE_TIMEOUT_MS elapses — whichever comes first, so a scroll that
// never quite stabilizes (e.g. a still-loading page) can't hang the tour
// forever. No-op (fires on the next frame) when there's no anchor, i.e. a
// centered step.
const SCROLL_SETTLE_TIMEOUT_MS = 600;

function _waitForScrollSettle(anchor, cb) {
  if (!anchor) {
    requestAnimationFrame(cb);
    return;
  }
  const start = performance.now();
  let last = anchor.getBoundingClientRect();
  const check = () => {
    const rect = anchor.getBoundingClientRect();
    const stable = rect.top === last.top && rect.left === last.left;
    if (stable || performance.now() - start > SCROLL_SETTLE_TIMEOUT_MS) {
      cb();
      return;
    }
    last = rect;
    requestAnimationFrame(check);
  };
  requestAnimationFrame(check);
}

// ── Popover builder ─────────────────────────────────────────────────────────

function _buildPopover(step, index, total) {
  // A one-step tour is a coach-mark, not a walkthrough: progress dots and an
  // "I'll explore on my own" escape hatch both imply a sequence that isn't
  // there, so the solo form drops them and reads "Got it" instead of "Next".
  const solo = total === 1;

  const popover = document.createElement('div');
  popover.className = 'tour-popover' + (step.finalChoice ? ' tour-popover--final' : '');
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-modal', 'false');
  popover.setAttribute('aria-live', 'polite');
  popover.setAttribute('aria-label', step.title);

  // Header
  const header = document.createElement('div');
  header.className = 'tour-popover-header';
  header.innerHTML = `
    <span class="tour-popover-header-orb" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="12" r="3.5" fill="currentColor" opacity=".6"/></svg>
    </span>
    <span class="tour-popover-header-label">Agnes is showing you around</span>`;

  // Body
  const body = document.createElement('div');
  body.className = 'tour-popover-body';

  const title = document.createElement('h2');
  title.className = 'tour-popover-title';
  title.textContent = step.title;

  const desc = document.createElement('p');
  desc.className = 'tour-popover-desc';
  desc.textContent = step.desc;

  body.appendChild(title);
  body.appendChild(desc);

  if (step.points && step.points.length) {
    const ul = document.createElement('ul');
    ul.className = 'tour-popover-points';
    step.points.forEach((pt) => {
      const li = document.createElement('li');
      li.className = 'tour-popover-point';
      li.textContent = pt;
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }

  // Footer
  const footer = document.createElement('div');
  footer.className = 'tour-popover-footer';

  // Dots
  const dots = document.createElement('div');
  dots.className = 'tour-dots';
  // Count only the steps that will actually render. A step whose anchor was
  // missing is dropped from the progress indicator entirely, so the label never
  // promises a step the user cannot reach.
  const skipped = (_active && _active.skipped) || new Set();
  const shown = [];
  for (let i = 0; i < total; i++) if (!skipped.has(i)) shown.push(i);
  const position = shown.indexOf(index);
  dots.setAttribute('aria-label', `Step ${position + 1} of ${shown.length}`);
  shown.forEach((i) => {
    const dot = document.createElement('span');
    dot.className = 'tour-dot' + (i === index ? ' on' : '');
    dots.appendChild(dot);
  });

  // Actions
  const actions = document.createElement('div');
  actions.className = 'tour-actions';

  if (step.finalChoice) {
    // Final step: Back · Finish onboarding · Connect my AI tools
    const backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'tour-btn tour-btn-back';
    backBtn.textContent = 'Back';
    if (index === 0) backBtn.disabled = true;
    backBtn.addEventListener('click', () => _gotoStep(index - 1));

    const finishBtn = document.createElement('button');
    finishBtn.type = 'button';
    finishBtn.className = 'tour-btn tour-btn-finish';
    finishBtn.textContent = 'Finish onboarding';
    finishBtn.addEventListener('click', async () => {
      markSeen(_active ? _active.id : 'stack');
      await markTourStepsDone();
      _endTour(true);
      window.location.href = '/chat';
    });

    const connectBtn = document.createElement('button');
    connectBtn.type = 'button';
    connectBtn.className = 'tour-btn tour-btn-connect';
    connectBtn.textContent = 'Connect my AI tools';
    connectBtn.addEventListener('click', () => {
      markSeen(_active ? _active.id : 'stack');
      // Navigating to the AI Connector is exactly what the journey panel's
      // "Use Agnes from other AI tools" step does, and that step marks itself
      // done on click — mirror it so the same action doesn't behave two ways.
      // Fire-and-forget: the navigation below must not wait on it.
      markUseAnywhereDone();
      _endTour(true);
      // The AI Connector page (/me/ai-connector) is the per-tool MCP guide
      // (Claude Code · Cursor · VS Code · …) that matches this button's intent
      // and the main-page "Connect your tools" CTA. /setup is the narrower
      // CLI-install page, reached from getting-started, not from here.
      window.location.href = '/me/ai-connector';
    });

    actions.appendChild(backBtn);
    actions.appendChild(finishBtn);
    actions.appendChild(connectBtn);
  } else {
    // Normal step: "I'll explore on my own" · Back · Next/Done
    const endBtn = document.createElement('button');
    endBtn.type = 'button';
    endBtn.className = 'tour-btn tour-btn-end';
    endBtn.textContent = 'I\'ll explore on my own';
    endBtn.addEventListener('click', () => _endTour(true));

    const backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'tour-btn tour-btn-back';
    backBtn.textContent = 'Back';
    if (index === 0) backBtn.disabled = true;
    backBtn.addEventListener('click', () => _gotoStep(index - 1));

    const isLastNonFinal = index === total - 2; // last before finalChoice
    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'tour-btn tour-btn-next';
    nextBtn.textContent = solo ? 'Got it' : (isLastNonFinal ? 'Done' : 'Next');
    nextBtn.addEventListener('click', () => _advanceStep());

    if (!solo) {
      actions.appendChild(endBtn);
      actions.appendChild(backBtn);
    }
    actions.appendChild(nextBtn);
  }

  if (solo) {
    // Without the dots the lone action row would sit left under
    // `justify-content: space-between` — pin it right.
    footer.classList.add('tour-popover-footer--solo');
  } else {
    footer.appendChild(dots);
  }
  footer.appendChild(actions);

  popover.appendChild(header);
  popover.appendChild(body);
  popover.appendChild(footer);

  return popover;
}

// ── Step advance (handles cross-page navigation) ────────────────────────────

// Go to a specific step index, crossing pages when that step lives on another
// page. Shared by _advanceStep (Next) and the missing-anchor auto-skip so both
// handle cross-page steps identically — otherwise a missing anchor would blindly
// recurse on the current page and blow through every cross-page step to the end.
function _gotoStep(nextIndex) {
  if (!_active) return;
  const { id, steps } = _active;
  if (nextIndex >= steps.length) { _endTour(true); return; }

  const nextStep = steps[nextIndex];
  if (!pathMatches(nextStep.page, window.location.pathname)) {
    // Cross-page: persist progress (as pending, NOT "seen" — the tour isn't
    // done) + navigate. resumePendingTour resumes it on the destination page.
    stashPending(id, nextIndex, _active.skipped);
    window.location.href = nextStep.page;
    return;
  }

  _showStep(nextIndex);
}

function _advanceStep() {
  if (!_active) return;
  _gotoStep(_active.index + 1);
}

// ── End tour ────────────────────────────────────────────────────────────────

function _endTour(markSeenNow) {
  if (!_active) return;
  if (markSeenNow) markSeen(_active.id);
  clearPending();

  if (_active.spotlight) {
    _active.spotlight.classList.remove('tour-spotlight');
  }
  if (_active.liftedAncestor) {
    _active.liftedAncestor.classList.remove('tour-lifts-ancestor');
  }
  if (_active.popover) _active.popover.remove();
  if (_active.overlay) _active.overlay.remove();
  _active = null;
  _removeListeners();
}

// Walk up from `el` to find the nearest ancestor that establishes its own
// stacking context via a positioned + z-indexed box (the pattern behind the
// bug this exists to work around: the rail is `position: fixed; z-index:
// 40`). A z-index set on a descendant of such an ancestor is scoped to that
// ancestor's context and can never out-rank an element painted at the root,
// like the tour scrim — regardless of how large the descendant's own
// z-index is. Stops at <body> since promoting the whole document is never
// useful here.
function _findStackingContextAncestor(el) {
  let node = el.parentElement;
  while (node && node !== document.body) {
    const cs = getComputedStyle(node);
    if (cs.position !== 'static' && cs.zIndex !== 'auto') return node;
    node = node.parentElement;
  }
  return null;
}

// ── Positioning ─────────────────────────────────────────────────────────────

const POPOVER_GAP = 14;
const VIEWPORT_PAD = 12;

function _positionPopover(popover, anchor, centered) {
  if (centered || !anchor) {
    // Center in the viewport WITHOUT a transform. The popover carries a
    // fill:both entrance animation whose end keyframe sets `transform`
    // (translateY(0) scale(1)); a CSS animation outranks an inline style, so an
    // inline translate(-50%,-50%) would be clobbered and the card would settle
    // off-centre (down/right). Compute absolute top/left from the measured size
    // instead — the animation's identity end-transform is then harmless.
    const popW = popover.offsetWidth || 380;
    const popH = popover.offsetHeight || 260;
    popover.style.position = 'fixed';
    popover.style.transform = '';
    popover.style.left = `${Math.max(VIEWPORT_PAD, (window.innerWidth - popW) / 2)}px`;
    popover.style.top = `${Math.max(VIEWPORT_PAD, (window.innerHeight - popH) / 2)}px`;
    return;
  }

  popover.style.transform = '';
  const rect = anchor.getBoundingClientRect();
  const popW = popover.offsetWidth || 380;
  const popH = popover.offsetHeight || 260;
  const vh = window.innerHeight;
  const vw = window.innerWidth;

  // Room available on each side of the anchor (clamped at 0 — an anchor
  // partly scrolled off-screen must not report negative space).
  const spaceBelow = Math.max(0, vh - rect.bottom - POPOVER_GAP - VIEWPORT_PAD);
  const spaceAbove = Math.max(0, rect.top - POPOVER_GAP - VIEWPORT_PAD);

  if (popH > spaceBelow && popH > spaceAbove) {
    // The card fits on neither side of the anchor — e.g. a short viewport, or
    // an anchor near an edge after the rail collapses to a top bar on mobile.
    // Pinning to `top: VIEWPORT_PAD` in this case reads as broken (the card
    // floats disconnected from what it's supposed to be pointing at); centering
    // at least reads as an intentional layout, and `.tour-popover`'s own
    // max-height + scroll (tour.css) keeps the content reachable either way.
    _positionPopover(popover, anchor, true);
    return;
  }

  // Try below the anchor first, else flip above.
  let top = popH <= spaceBelow ? rect.bottom + POPOVER_GAP : rect.top - POPOVER_GAP - popH;
  top = Math.max(VIEWPORT_PAD, Math.min(top, vh - popH - VIEWPORT_PAD));

  // Horizontal: align to anchor left, clamped into viewport.
  let left = rect.left;
  left = Math.max(VIEWPORT_PAD, Math.min(left, vw - popW - VIEWPORT_PAD));

  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
}

// ── Reflow listeners (resize + scroll) ──────────────────────────────────────

function _onReflow() {
  if (!_active || !_active.popover) return;
  const step = _active.steps[_active.index];
  if (!step) return;
  _positionPopover(_active.popover, _active.spotlight, step.centered);
}

function _onKeyDown(e) {
  if (e.key === 'Escape' && _active) _endTour(true);
}

let _listenersAttached = false;

function _attachListeners() {
  if (_listenersAttached) return;
  window.addEventListener('resize', _onReflow, { passive: true });
  window.addEventListener('scroll', _onReflow, { passive: true });
  document.addEventListener('keydown', _onKeyDown);
  _listenersAttached = true;
}

function _removeListeners() {
  window.removeEventListener('resize', _onReflow);
  window.removeEventListener('scroll', _onReflow);
  document.removeEventListener('keydown', _onKeyDown);
  _listenersAttached = false;
}

// ── Auto-resume on page load ────────────────────────────────────────────────

/**
 * Call once per page on DOMContentLoaded (or directly from a module script).
 * Checks for a pending tour whose next step belongs to the current page
 * and resumes automatically.
 *
 * Returns true if a tour was resumed, false otherwise.
 */
export function resumePendingTour() {
  const pending = getPending();
  if (!pending) return false;

  // Only resume a fresh record — a stale one means the user abandoned the tour
  // via ordinary navigation; don't re-pop it uninvited.
  if (!pending.ts || Date.now() - pending.ts > RESUME_FRESH_MS) {
    clearPending();
    return false;
  }

  const { id, index, skipped } = pending;
  const steps = TOURS[id];
  if (!steps) { clearPending(); return false; }

  const step = steps[index];
  if (!step) { clearPending(); return false; }

  if (!pathMatches(step.page, window.location.pathname)) return false;

  // Resume — don't clearPending yet; _endTour will. _startTour attaches listeners.
  _startTour(id, steps, index, skipped);
  return true;
}
