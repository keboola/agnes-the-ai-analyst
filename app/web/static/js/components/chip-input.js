/*
 * chip-input.js — generic multi-select with typeahead + optional "+ Create new" hook.
 *
 * Used by /admin/tables (Data Packages field) and /admin/corporate-memory
 * (item Domains field). Vanilla — no framework deps.
 *
 * Markup (any tag with role="chip-input" or class="chip-input" works):
 *
 *   <div class="chip-input"
 *        data-source-url="/api/admin/data-packages"
 *        data-allow-create="true"
 *        data-name="package_ids"
 *        data-selected='[{"id":"p1","name":"Sales"}]'></div>
 *
 * Emits the standard `change` event with `event.detail = { selected: [...] }`.
 * A hidden <input name="data-name"> mirrors the selected ids as a JSON array
 * so the surrounding <form> picks the value up on submit.
 *
 * Keyboard:
 *   ↓ / ↑   — navigate dropdown
 *   Enter   — pick highlighted candidate (or "+ Create new" tail row)
 *   Esc     — close dropdown
 *   Backspace on empty input — remove last chip
 *
 * a11y: combobox role + aria-activedescendant on the highlighted row.
 */
(function() {
  'use strict';

  function init(host) {
    if (host.dataset.chipReady === '1') return;
    host.dataset.chipReady = '1';

    const sourceUrl = host.dataset.sourceUrl;
    const allowCreate = host.dataset.allowCreate === 'true';
    const name = host.dataset.name || 'chip_ids';
    let selected = [];
    try { selected = JSON.parse(host.dataset.selected || '[]'); } catch (_) { selected = []; }

    // ── Build DOM ─────────────────────────────────────────────────────────
    host.innerHTML = '';
    host.style.cssText = host.style.cssText +
      ';display:flex;flex-wrap:wrap;gap:6px;align-items:center;' +
      'border:1px solid #e5e7eb;border-radius:8px;padding:6px;position:relative;background:#fff;';

    const chipsHost = document.createElement('div');
    chipsHost.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;flex:1;min-width:120px;';
    host.appendChild(chipsHost);

    const input = document.createElement('input');
    input.type = 'text';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.style.cssText = 'flex:1;min-width:100px;border:none;outline:none;font:inherit;padding:4px;';
    input.placeholder = host.dataset.placeholder || 'Type to search or create…';
    chipsHost.appendChild(input);

    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = name;
    hidden.value = JSON.stringify(selected.map(s => s.id));
    host.appendChild(hidden);

    const dropdown = document.createElement('div');
    dropdown.setAttribute('role', 'listbox');
    dropdown.style.cssText = 'position:absolute;left:0;right:0;top:100%;background:#fff;' +
      'border:1px solid #e5e7eb;border-radius:8px;margin-top:4px;max-height:240px;' +
      'overflow-y:auto;display:none;z-index:1000;box-shadow:0 4px 12px rgba(0,0,0,0.08);';
    host.appendChild(dropdown);

    let candidates = [];
    let activeIdx = -1;

    function renderChips() {
      chipsHost.innerHTML = '';
      for (const s of selected) {
        const chip = document.createElement('span');
        chip.style.cssText = 'display:inline-flex;align-items:center;gap:4px;' +
          'background:#dbeafe;color:#1e3a8a;border-radius:999px;padding:2px 10px;font-size:12px;';
        chip.textContent = s.name || s.id;
        const x = document.createElement('button');
        x.type = 'button';
        x.textContent = '×';
        x.setAttribute('aria-label', 'Remove ' + (s.name || s.id));
        x.style.cssText = 'border:none;background:transparent;cursor:pointer;color:#1e3a8a;font-size:14px;line-height:1;padding:0 0 0 2px;';
        x.addEventListener('click', () => {
          selected = selected.filter(it => it.id !== s.id);
          syncHiddenAndEmit();
          renderChips();
          chipsHost.appendChild(input);
          input.focus();
        });
        chip.appendChild(x);
        chipsHost.appendChild(chip);
      }
      chipsHost.appendChild(input);
    }

    function syncHiddenAndEmit() {
      hidden.value = JSON.stringify(selected.map(s => s.id));
      host.dispatchEvent(new CustomEvent('change', {
        detail: { selected: selected.slice() },
      }));
    }

    function closeDropdown() {
      dropdown.style.display = 'none';
      input.setAttribute('aria-expanded', 'false');
      activeIdx = -1;
    }

    function renderDropdown(filter) {
      dropdown.innerHTML = '';
      activeIdx = -1;
      const q = (filter || '').toLowerCase();
      const selIds = new Set(selected.map(s => s.id));
      const matched = (candidates || [])
        .filter(c => !selIds.has(c.id))
        .filter(c => (c.name || '').toLowerCase().includes(q));
      matched.forEach((c, i) => {
        const row = document.createElement('div');
        row.setAttribute('role', 'option');
        row.id = 'chip-opt-' + i;
        row.style.cssText = 'padding:6px 12px;cursor:pointer;font-size:13px;';
        row.textContent = c.name || c.id;
        row.dataset.idx = String(i);
        row.addEventListener('mousedown', (e) => {
          e.preventDefault();
          pickIdx(i, matched);
        });
        dropdown.appendChild(row);
      });
      if (allowCreate && filter && !matched.some(m => (m.name || '').toLowerCase() === q)) {
        const createRow = document.createElement('div');
        createRow.setAttribute('role', 'option');
        createRow.style.cssText = 'padding:6px 12px;cursor:pointer;font-size:13px;' +
          'border-top:1px solid #e5e7eb;color:#0073D1;';
        createRow.textContent = '+ Create new "' + filter + '"…';
        createRow.dataset.create = '1';
        createRow.dataset.name = filter;
        createRow.addEventListener('mousedown', (e) => {
          e.preventDefault();
          host.dispatchEvent(new CustomEvent('chip-create', {
            detail: { typed: filter, host: host },
          }));
        });
        dropdown.appendChild(createRow);
      }
      const hasRows = dropdown.children.length > 0;
      dropdown.style.display = hasRows ? 'block' : 'none';
      input.setAttribute('aria-expanded', hasRows ? 'true' : 'false');
    }

    function pickIdx(i, matched) {
      const c = matched[i];
      if (!c) return;
      selected.push({ id: c.id, name: c.name });
      input.value = '';
      syncHiddenAndEmit();
      renderChips();
      renderDropdown('');
    }

    function setActive(idx) {
      const opts = dropdown.querySelectorAll('[role="option"]');
      opts.forEach((o, i) => {
        o.style.background = i === idx ? '#dbeafe' : 'transparent';
      });
      activeIdx = idx;
      if (idx >= 0 && opts[idx]) input.setAttribute('aria-activedescendant', opts[idx].id || '');
    }

    let fetchTimer;
    function loadCandidates(q) {
      if (!sourceUrl) return Promise.resolve([]);
      const params = new URLSearchParams();
      if (q) params.set('search', q);
      const url = sourceUrl + (params.toString() ? '?' + params.toString() : '');
      return fetch(url, { credentials: 'same-origin' })
        .then(r => r.ok ? r.json() : [])
        .then(data => Array.isArray(data) ? data : (data.items || []))
        .catch(() => []);
    }

    input.addEventListener('focus', async () => {
      candidates = await loadCandidates('');
      renderDropdown(input.value || '');
    });
    input.addEventListener('input', () => {
      clearTimeout(fetchTimer);
      fetchTimer = setTimeout(async () => {
        candidates = await loadCandidates(input.value || '');
        renderDropdown(input.value || '');
      }, 120);
    });
    input.addEventListener('keydown', (e) => {
      const opts = dropdown.querySelectorAll('[role="option"]');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (!opts.length) return;
        setActive(Math.min(activeIdx + 1, opts.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive(Math.max(activeIdx - 1, 0));
      } else if (e.key === 'Enter') {
        if (activeIdx >= 0 && opts[activeIdx]) {
          e.preventDefault();
          opts[activeIdx].dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        }
      } else if (e.key === 'Escape') {
        closeDropdown();
      } else if (e.key === 'Backspace' && !input.value && selected.length) {
        selected.pop();
        syncHiddenAndEmit();
        renderChips();
      }
    });
    input.addEventListener('blur', () => {
      // Delay close so the mousedown handlers fire.
      setTimeout(closeDropdown, 120);
    });

    // Public API for parent code to append a freshly-created chip
    // (used by the inline "Create new" modal after POST returns the new id).
    host.addChip = function(entry) {
      if (!entry || !entry.id) return;
      if (selected.some(s => s.id === entry.id)) return;
      selected.push({ id: entry.id, name: entry.name || entry.id });
      input.value = '';
      syncHiddenAndEmit();
      renderChips();
      closeDropdown();
    };

    renderChips();
  }

  function bootstrapAll() {
    document.querySelectorAll('.chip-input, [data-chip-input]').forEach(init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapAll);
  } else {
    bootstrapAll();
  }
  // Expose for dynamically-inserted hosts.
  window.ChipInput = { init: init, bootstrapAll: bootstrapAll };
})();
