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
 *   sessionStorage 'agnes.tour.pending'   — cross-page resume {id, index}
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
    // Step 1: /stack — the New upload button
    {
      page: '/stack',
      selector: '#new-upload-btn',
      title: 'Add your own files',
      desc: 'Upload a document only you need — a pricing deck, a spec, meeting notes — and I\'ll search it and cite it in my answers. No admin needed.',
      points: [
        'Works with PDF, Markdown, TXT, and more.',
        'Private to you unless you choose to share it.',
      ],
    },
    // Step 2: /stack — Catalog nav item (in rail)
    {
      page: '/stack',
      selector: '#nav-catalog',
      title: 'Need something I don\'t have?',
      desc: 'When a question needs data or a tool that isn\'t in your Stack yet, add it from the Catalog. My Stack is what you already have; the Catalog is what you can add.',
      points: [
        'When a question needs something new, I\'ll usually point you here right in the chat.',
      ],
    },
    // Step 3: /catalog — the catalog listing zone
    {
      page: '/catalog',
      selector: '#browse-catalog-zone',
      title: 'This is the Catalog',
      desc: 'Browse everything your organization has published, then click Add to put it in your Stack so I can use it.',
      points: [
        'Prefer to type? Just ask me in chat — "add Salesforce" does the same thing.',
      ],
    },
    // Step 4: /catalog — the submit CTA
    {
      page: '/catalog',
      selector: '#browse-submit-btn',
      title: 'Share what you build',
      desc: 'Built a skill or plugin your team could reuse? Publish it here so others can add it from the Catalog.',
      points: [
        'Publishing to the Flea Market is separate from adding something to your own Stack.',
      ],
    },
    // Step 5: final centered step — no anchor
    {
      page: '/catalog',
      centered: true,
      finalChoice: true,
      title: 'That\'s the tour',
      desc: 'You can set up your Stack and add what I need by prompting me in chat. Want the same company context in Claude Code, Cursor, and VS Code? Use Connect my AI tools below.',
      points: [],
    },
  ],
};

// ── Persistence helpers ─────────────────────────────────────────────────────

const PENDING_KEY = 'agnes.tour.pending';

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

function stashPending(id, index) {
  try {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ id, index }));
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
// Marks all journey steps done via the existing /api/chat/journey endpoint.
async function markAllJourneyDone() {
  try {
    await fetch('/api/chat/journey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        first_asked: true,
        stack_setup_done: true,
        explored_stack: true,
        catalog_discovered: true,
        use_anywhere: true,
      }),
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

function _startTour(id, steps, index) {
  _endTour(false); // clean up any prior run

  const overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  document.body.appendChild(overlay);

  _active = { id, steps, index: null, overlay, popover: null, spotlight: null };
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

  _active.index = index;

  // Resolve anchor element (skip forward on miss).
  let anchor = null;
  if (!step.centered && step.selector) {
    anchor = document.querySelector(step.selector);
    if (!anchor) {
      // Resilient: auto-skip this step if the anchor is missing.
      _showStep(index + 1);
      return;
    }
    // Scroll target into view (center) before positioning.
    anchor.scrollIntoView({ behavior: 'smooth', block: 'center' });
    anchor.classList.add('tour-spotlight');
    _active.spotlight = anchor;
  }

  // Build popover.
  const popover = _buildPopover(step, index, steps.length);
  document.body.appendChild(popover);
  _active.popover = popover;

  // Position after a short yield (scroll settles).
  requestAnimationFrame(() => {
    if (_active && _active.index === index) {
      _positionPopover(popover, anchor, step.centered);
    }
  });
}

// ── Popover builder ─────────────────────────────────────────────────────────

function _buildPopover(step, index, total) {
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
  dots.setAttribute('aria-label', `Step ${index + 1} of ${total}`);
  for (let i = 0; i < total; i++) {
    const dot = document.createElement('span');
    dot.className = 'tour-dot' + (i === index ? ' on' : '');
    dots.appendChild(dot);
  }

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
    backBtn.addEventListener('click', () => _showStep(index - 1));

    const finishBtn = document.createElement('button');
    finishBtn.type = 'button';
    finishBtn.className = 'tour-btn tour-btn-finish';
    finishBtn.textContent = 'Finish onboarding';
    finishBtn.addEventListener('click', async () => {
      markSeen(_active ? _active.id : 'stack');
      await markAllJourneyDone();
      _endTour(true);
      window.location.href = '/chat';
    });

    const connectBtn = document.createElement('button');
    connectBtn.type = 'button';
    connectBtn.className = 'tour-btn tour-btn-connect';
    connectBtn.textContent = 'Connect my AI tools';
    connectBtn.addEventListener('click', () => {
      markSeen(_active ? _active.id : 'stack');
      _endTour(true);
      window.location.href = '/setup';
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
    backBtn.addEventListener('click', () => _showStep(index - 1));

    const isLastNonFinal = index === total - 2; // last before finalChoice
    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'tour-btn tour-btn-next';
    nextBtn.textContent = isLastNonFinal ? 'Done' : 'Next';
    nextBtn.addEventListener('click', () => _advanceStep());

    actions.appendChild(endBtn);
    actions.appendChild(backBtn);
    actions.appendChild(nextBtn);
  }

  footer.appendChild(dots);
  footer.appendChild(actions);

  popover.appendChild(header);
  popover.appendChild(body);
  popover.appendChild(footer);

  return popover;
}

// ── Step advance (handles cross-page navigation) ────────────────────────────

function _advanceStep() {
  if (!_active) return;
  const { id, steps, index } = _active;
  const nextIndex = index + 1;
  if (nextIndex >= steps.length) { _endTour(true); return; }

  const nextStep = steps[nextIndex];
  const currentPath = window.location.pathname;

  if (!pathMatches(nextStep.page, currentPath)) {
    // Cross-page: stash pending + navigate.
    markSeen(id); // persist seen before nav so back-nav doesn't re-launch
    // Actually we only mark "pending" not "seen" — don't mark seen until truly done.
    // But we need to persist progress across pages:
    stashPending(id, nextIndex);
    window.location.href = nextStep.page;
    return;
  }

  _showStep(nextIndex);
}

// ── End tour ────────────────────────────────────────────────────────────────

function _endTour(markSeenNow) {
  if (!_active) return;
  if (markSeenNow) markSeen(_active.id);
  clearPending();

  if (_active.spotlight) {
    _active.spotlight.classList.remove('tour-spotlight');
  }
  if (_active.popover) _active.popover.remove();
  if (_active.overlay) _active.overlay.remove();
  _active = null;
  _removeListeners();
}

// ── Positioning ─────────────────────────────────────────────────────────────

const POPOVER_GAP = 14;
const VIEWPORT_PAD = 12;

function _positionPopover(popover, anchor, centered) {
  if (centered || !anchor) {
    // Center in viewport.
    popover.style.position = 'fixed';
    popover.style.top = '50%';
    popover.style.left = '50%';
    popover.style.transform = 'translate(-50%, -50%)';
    return;
  }

  popover.style.transform = '';
  const rect = anchor.getBoundingClientRect();
  const popW = popover.offsetWidth || 380;
  const popH = popover.offsetHeight || 260;
  const vh = window.innerHeight;
  const vw = window.innerWidth;

  // Try below the anchor first.
  let top = rect.bottom + POPOVER_GAP;
  // If it overflows the bottom, flip above.
  if (top + popH > vh - VIEWPORT_PAD) {
    top = rect.top - POPOVER_GAP - popH;
  }
  // Never go above viewport.
  if (top < VIEWPORT_PAD) top = VIEWPORT_PAD;

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
// Attach listeners once after tour starts:
const _origStartTour = _startTour;
function _startTourWrapped(id, steps, index) {
  _attachListeners();
  _origStartTour(id, steps, index);
}

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

  const { id, index } = pending;
  const steps = TOURS[id];
  if (!steps) { clearPending(); return false; }

  const step = steps[index];
  if (!step) { clearPending(); return false; }

  if (!pathMatches(step.page, window.location.pathname)) return false;

  // Resume — don't clearPending yet; _endTour will.
  _attachListeners();
  _startTour(id, steps, index);
  return true;
}
