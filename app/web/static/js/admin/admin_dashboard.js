/* Fills the "Needs fixing" zone on /admin from
   GET /api/admin/dashboard/signals.
 *
 * Split out of the page render because these signals read sync_history, jobs
 * and usage_events — the tables that grow without bound. See
 * app/services/admin_dashboard.py for the TTL cache that keeps a tab left
 * open on this page from becoming a load source.
 *
 * All rows are built with createElement + textContent, never innerHTML:
 * blurbs interpolate values that originate in the registry and in job/tool
 * names, which are not authored by us.
 */
(function () {
  'use strict';

  var host = document.querySelector('[data-adash-fixing]');
  if (!host) return;

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function note(cls, text) {
    var d = document.createElement('div');
    d.className = cls;
    d.textContent = text;
    return d;
  }

  function row(sig) {
    // A failed check renders as a non-link: there is no count to act on, and
    // sending the admin to a page that would show them nothing is worse than
    // saying plainly that the check broke.
    var el = document.createElement(sig.failed || !sig.href ? 'div' : 'a');
    el.className = 'adash-row is-' + (sig.severity || 'warn') +
      (sig.failed ? ' adash-row--failed' : '');
    if (el.tagName === 'A') el.href = sig.href;

    var count = document.createElement('span');
    count.className = 'adash-row__count';
    if (sig.failed) {
      count.textContent = '—';
      count.setAttribute('aria-hidden', 'true');
    } else {
      count.textContent = String(sig.count);
    }

    var body = document.createElement('span');
    body.className = 'adash-row__body';

    var title = document.createElement('span');
    title.className = 'adash-row__title';
    title.textContent = sig.title || '';

    var blurb = document.createElement('span');
    blurb.className = 'adash-row__blurb';
    blurb.textContent = sig.blurb || '';

    body.appendChild(title);
    body.appendChild(blurb);
    el.appendChild(count);
    el.appendChild(body);

    if (el.tagName === 'A') {
      var go = document.createElement('span');
      go.className = 'adash-row__go';
      go.setAttribute('aria-hidden', 'true');
      go.textContent = 'Fix →';
      el.appendChild(go);
    }
    return el;
  }

  fetch('/api/admin/dashboard/signals', { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      var signals = (data && data.signals) || [];
      clear(host);
      if (!signals.length) {
        // An empty payload IS the healthy state — the API omits clear
        // signals rather than returning them at zero.
        host.appendChild(note('adash-clear', 'Everything is running normally.'));
      } else {
        var rows = document.createElement('div');
        rows.className = 'adash-rows';
        signals.forEach(function (s) { rows.appendChild(row(s)); });
        host.appendChild(rows);
      }
    })
    .catch(function () {
      clear(host);
      // Never fall back to a reassuring message: a failed fetch tells us
      // nothing about instance health.
      host.appendChild(note('adash-skeleton', 'Could not load instance health. Reload to retry.'));
    })
    .finally(function () {
      host.setAttribute('aria-busy', 'false');
    });
})();
