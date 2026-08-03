// ── Fast tooltip for `[data-tip]` ───────────────────────────────────────────
// Shared (v113). Lifted verbatim in behaviour from library.html's inline
// `.lib-tip` implementation, which had to become shared once the trust markers
// started appearing on the catalog and detail pages too: one copy, because two
// would eventually disagree about the delay or the geometry.
//
// Why not the native `title`: its show delay is OS-controlled at 600ms+, which
// is far too slow when the tooltip is the ONLY explanation of an icon-only
// marker — a 14px glyph nobody can read on sight. Elements carry `data-tip` and
// `aria-label` and deliberately NOT `title`, so the slow native bubble never
// arrives on top of this one.
//
// Why fixed-position on <body> rather than a CSS ::after: a ::after is clipped
// by the ancestors these markers live in — `.lib-table td` is `overflow: hidden`,
// `.lib-tablewrap` is `overflow-x: auto` (so `overflow-y` computes to auto too),
// and a grid `.fbar-card` is `overflow: hidden`, which would slice a bubble off
// at the card edge.
//
// Delegated on `document`, so markup cloned in after load — the grid cards are
// built from the table rows at render time — is picked up with no re-wiring.
(function () {
  if (window.__dsTooltipReady) return;   // base_ds + base both include this
  window.__dsTooltipReady = true;

  // 120ms is anti-flicker for WIDE targets: a Library Sharing badge is most of
  // a column, so a pointer crossing the table sweeps several and each would
  // blink. `data-tip-instant` opts out for targets too small to cross by
  // accident (the trust markers), where the delay only makes the one affordance
  // that explains the glyph feel sluggish.
  const SWEEP_DELAY_MS = 120;

  const tip = document.createElement('div');
  tip.className = 'ds-tip';
  tip.setAttribute('role', 'tooltip');
  let timer = null;
  let target = null;

  function mounted() {
    if (!tip.isConnected && document.body) document.body.appendChild(tip);
    return tip.isConnected;
  }

  function position(el) {
    const r = el.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    let left = r.left + r.width / 2 - t.width / 2;
    left = Math.max(6, Math.min(left, window.innerWidth - t.width - 6));
    let top = r.top - t.height - 8;
    if (top < 4) top = r.bottom + 8;   // no room above — flip below
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function show(el) {
    const text = el.getAttribute('data-tip');
    if (!text || !mounted()) return;
    tip.textContent = text;
    target = el;
    position(el);
    // Measure-then-reveal: the first position() runs before the text has been
    // laid out at its final width, so re-position on the next frame.
    requestAnimationFrame(() => {
      if (target !== el) return;
      position(el);
      tip.classList.add('is-visible');
    });
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    target = null;
    tip.classList.remove('is-visible');
  }

  function arm(el, delay) {
    clearTimeout(timer);
    timer = setTimeout(() => show(el), delay);
  }

  document.addEventListener('mouseover', ev => {
    const el = ev.target.closest && ev.target.closest('[data-tip]');
    if (!el || el === target) return;
    arm(el, el.hasAttribute('data-tip-instant') ? 0 : SWEEP_DELAY_MS);
  });
  document.addEventListener('mouseout', ev => {
    const el = ev.target.closest && ev.target.closest('[data-tip]');
    if (!el || (ev.relatedTarget && el.contains(ev.relatedTarget))) return;
    hide();
  });
  // Keyboard route: same sentence, reached by focus. Always delayed — tabbing
  // through a toolbar should not strobe.
  document.addEventListener('focusin', ev => {
    const el = ev.target.closest && ev.target.closest('[data-tip]');
    if (!el) return;
    arm(el, SWEEP_DELAY_MS);
  });
  document.addEventListener('focusout', ev => {
    if (ev.target.closest && ev.target.closest('[data-tip]')) hide();
  });
  document.addEventListener('click', hide);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') hide(); });
  // Capture: a scroll inside `.lib-tablewrap` does not bubble to window.
  window.addEventListener('scroll', hide, true);
})();
