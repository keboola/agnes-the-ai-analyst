/* =====================================================================
 * package_drawer.js — bringing a Data Package into existence, from
 * wherever the reader noticed one was missing.
 *
 * This was a centred modal that lived in admin_tables.html, and it had
 * two problems the drawer fixes by construction:
 *
 *   1. It was the tallest form in the admin surface (name, slug,
 *      description, lifecycle, category, icon, colour, cover image, and
 *      a group-access matrix) inside a card sized for one decision — so
 *      on a laptop its footer sat on top of its own last field.
 *   2. Only /admin/tables carried the scaffolding, so the Packages lens
 *      — where "+ New package" actually belongs — had to LINK to it
 *      (`/admin/tables?new_package=1`). Clicking "new package" on the
 *      Packages tab switched you to the Tables tab to fill the form in.
 *
 * So it is a drawer, and it is a component: the flow opens in place on
 * whatever lens you are standing on, and the page it was opened from
 * stays visible behind it.
 *
 *   window.AgnesPackageDrawer.open({
 *     typed:     'Sales bundle',   // prefill the name
 *     chipHost:  hostEl,           // chip-input to append the new chip to
 *     onCreated: function (pkg, grantFailures) { … },
 *   })
 *
 * No endpoint is new here — the same three the modal used:
 *   POST /api/admin/data-packages        (create)
 *   POST /api/admin/uploads/cover-image  (cover, on pick)
 *   POST /api/admin/grants               (one per chosen group)
 *
 * Chrome: css/drawer.css (the shared drawer) + css/filter_toolbar.css
 * (`.fbar-select`, `.fbar-seg` — the product's select and segmented
 * control) + css/stack_card.css (`.cf-palette-row`, hydrated globally by
 * _app_scripts.html). A consumer links those three and nothing else.
 * ===================================================================== */
(function () {
  'use strict';

  var PKG_API = '/api/admin/data-packages';
  var GRANTS_API = '/api/admin/grants';
  var GROUPS_API = '/api/admin/groups';
  var COVER_API = '/api/admin/uploads/cover-image';

  var els = null;          // built lazily on first open
  var st = null;           // per-open state

  function api(url, opts) {
    opts = opts || {};
    opts.credentials = 'include';
    if (opts.body) opts.headers = { 'Content-Type': 'application/json' };
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var d = b && b.detail;
          throw new Error(typeof d === 'string' ? d : 'HTTP ' + r.status);
        });
      }
      return r.status === 204 ? null : r.json();
    });
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* URL-safe identifier, same normalisation the server's seed step uses. */
  function slugify(name) {
    return (name || '').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  /* ── Scaffold ─────────────────────────────────────────────────────── */

  function build() {
    if (els) return els;
    var root = document.createElement('div');
    root.className = 'ds-drawer';
    root.hidden = true;
    // This drawer owns its own Escape / backdrop handling; opt out of the
    // global handler in _app_scripts.html, which hides overlays with an
    // inline display:none and would leave our state half-open.
    root.dataset.noEscClose = '1';
    root.innerHTML =
      '<div class="ds-drawer__backdrop" data-pdw-close></div>' +
      '<div class="ds-drawer__panel" role="dialog" aria-modal="true" aria-labelledby="pdw-title">' +
      '  <div class="ds-drawer__head">' +
      '    <div class="ds-drawer__head-main">' +
      '      <h2 class="ds-drawer__title" id="pdw-title">New data package</h2>' +
      '      <p class="ds-drawer__sub">A package is the unit an analyst receives — tables reach them only through one.</p>' +
      '    </div>' +
      '    <button type="button" class="ds-drawer__x" data-pdw-close aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="ds-drawer__body">' +
      '    <section class="ds-drawer__pane is-on">' +
      '      <p class="ds-drawer__lede">Name it after what it carries. Everything below this' +
      '        stays editable on the package’s own page afterwards.</p>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-name">Name</label>' +
      '        <input type="text" id="pdw-name" autocomplete="off" placeholder="Sales bundle">' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-slug">Slug</label>' +
      '        <input type="text" id="pdw-slug" autocomplete="off" placeholder="sales-bundle">' +
      '        <p class="ds-drawer__hint">URL-safe identifier; follows the name until you edit it.</p>' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-desc">Description <span class="ds-drawer__opt">(optional)</span></label>' +
      '        <textarea id="pdw-desc" autocomplete="off" placeholder="What is in here, and who it is for."></textarea>' +
      '      </div>' +
      '      <div class="ds-drawer__row">' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-status">Status</label>' +
      '          <span class="fbar-select ds-drawer__select">' +
      '            <select id="pdw-status">' +
      '              <option value="prod" selected>Prod — ready for analyst use</option>' +
      '              <option value="poc">POC — try-before-you-buy</option>' +
      '              <option value="coming-soon">Coming soon — visible, not usable yet</option>' +
      '              <option value="draft">Draft — admin-only, hidden from analysts</option>' +
      '            </select>' +
      '          </span>' +
      '        </div>' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-category">Category <span class="ds-drawer__opt">(optional)</span></label>' +
      '          <input type="text" id="pdw-category" autocomplete="off" placeholder="e.g. Sessions &amp; Traffic">' +
      '          <p class="ds-drawer__hint">The eyebrow line above the card title in the Library.</p>' +
      '        </div>' +
      '      </div>' +
      '      <div class="ds-drawer__row">' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-icon">Icon <span class="ds-drawer__opt">(optional)</span></label>' +
      '          <input type="text" id="pdw-icon" autocomplete="off" maxlength="4" placeholder="📦">' +
      '        </div>' +
      '        <div class="ds-drawer__field">' +
      '          <label for="pdw-color">Colour</label>' +
      '          <div class="pdw-color">' +
      '            <div class="cf-palette-row" data-target="pdw-color"></div>' +
      '            <input type="color" id="pdw-color" value="#0EA5B5" class="ds-drawer__color">' +
      '          </div>' +
      '        </div>' +
      '      </div>' +
      '      <div class="ds-drawer__field">' +
      '        <label for="pdw-cover-file">Cover image <span class="ds-drawer__opt">(optional)</span></label>' +
      '        <div class="pdw-cover">' +
      '          <div class="pdw-cover__preview" id="pdw-cover-preview">No image</div>' +
      '          <div class="pdw-cover__pick">' +
      '            <input type="file" id="pdw-cover-file" class="ds-drawer__file"' +
      '                   accept="image/png,image/jpeg,image/gif,image/webp">' +
      '            <input type="hidden" id="pdw-cover-url" value="">' +
      '            <p class="ds-drawer__hint">PNG / JPEG / GIF / WebP, max 5 MiB.' +
      '              <button type="button" class="ds-drawer__linkbtn" id="pdw-cover-clear" hidden>Remove</button></p>' +
      '          </div>' +
      '        </div>' +
      '      </div>' +
      '      <details class="ds-drawer__disclose" id="pdw-access">' +
      '        <summary>Who gets it <span class="ds-drawer__opt">(optional)</span></summary>' +
      '        <p class="ds-drawer__hint" style="margin:8px 0 12px;">' +
      '          <strong>Optional</strong> shows the package in that group’s Library for members to add;' +
      '          <strong>Automatic</strong> puts it in their workspace on the next sync.' +
      '          Leave this closed and the package is private until you share it.</p>' +
      '        <div id="pdw-groups"></div>' +
      '      </details>' +
      '      <div class="ds-drawer__err" id="pdw-err" hidden></div>' +
      '    </section>' +
      '  </div>' +
      '  <div class="ds-drawer__foot">' +
      '    <span class="ds-drawer__foot-gap"></span>' +
      '    <button type="button" class="btn btn-secondary" data-pdw-close>Cancel</button>' +
      '    <button type="button" class="btn btn-primary" id="pdw-submit">Create package</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);

    els = {
      root: root,
      panel: root.querySelector('.ds-drawer__panel'),
      body: root.querySelector('.ds-drawer__body'),
      name: root.querySelector('#pdw-name'),
      slug: root.querySelector('#pdw-slug'),
      desc: root.querySelector('#pdw-desc'),
      status: root.querySelector('#pdw-status'),
      category: root.querySelector('#pdw-category'),
      icon: root.querySelector('#pdw-icon'),
      color: root.querySelector('#pdw-color'),
      coverFile: root.querySelector('#pdw-cover-file'),
      coverUrl: root.querySelector('#pdw-cover-url'),
      coverPreview: root.querySelector('#pdw-cover-preview'),
      coverClear: root.querySelector('#pdw-cover-clear'),
      access: root.querySelector('#pdw-access'),
      groups: root.querySelector('#pdw-groups'),
      err: root.querySelector('#pdw-err'),
      submit: root.querySelector('#pdw-submit'),
    };

    root.addEventListener('click', function (e) {
      if (e.target.closest('[data-pdw-close]')) close();
    });
    // Document-level, not panel-level: focus can legitimately sit outside
    // the panel (a click on the backdrop, the native colour picker), and
    // Escape has to close the drawer from there too.
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && st) { e.stopPropagation(); close(); }
    });
    // The slug follows the name until the admin types in it — after that it
    // is theirs. (The modal derived it only at open, so typing a name into
    // an already-open form left the slug empty and the create silently
    // bounced on a required field.)
    els.name.addEventListener('input', function () {
      if (!st || st.slugTouched) return;
      els.slug.value = slugify(els.name.value);
    });
    els.slug.addEventListener('input', function () {
      if (st) st.slugTouched = true;
    });
    els.name.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); els.submit.click(); }
    });
    els.coverFile.addEventListener('change', onCoverPicked);
    els.coverClear.addEventListener('click', clearCover);
    els.access.addEventListener('toggle', function () {
      if (els.access.open) hydrateGroups();
    });
    // Tier segments inside the group list: a group is granted when it is
    // ticked, and ticking it defaults to Optional — the safe tier, since
    // Automatic writes into every member's workspace on their next sync.
    els.groups.addEventListener('click', function (e) {
      var btn = e.target.closest('.fbar-seg__btn');
      if (!btn) return;
      var row = btn.closest('[data-group-id]');
      var box = row.querySelector('input[type="checkbox"]');
      box.checked = true;
      row.querySelectorAll('.fbar-seg__btn').forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
    });
    els.groups.addEventListener('change', function (e) {
      var box = e.target.closest('input[type="checkbox"]');
      if (!box) return;
      var row = box.closest('[data-group-id]');
      var segs = row.querySelectorAll('.fbar-seg__btn');
      if (box.checked && !row.querySelector('.fbar-seg__btn.is-active')) {
        segs[0].classList.add('is-active');
        segs[0].setAttribute('aria-pressed', 'true');
      } else if (!box.checked) {
        segs.forEach(function (b) {
          b.classList.remove('is-active');
          b.setAttribute('aria-pressed', 'false');
        });
      }
    });
    els.submit.addEventListener('click', submit);
    return els;
  }

  /* ── Open / close ─────────────────────────────────────────────────── */

  function open(opts) {
    opts = opts || {};
    build();
    st = {
      chipHost: opts.chipHost || null,
      onCreated: opts.onCreated || function () {},
      slugTouched: false,
      groupsLoaded: false,
      restoreFocus: document.activeElement,
    };
    var typed = opts.typed || '';
    els.name.value = typed;
    els.slug.value = slugify(typed);
    els.desc.value = '';
    els.status.value = 'prod';
    els.category.value = '';
    els.icon.value = '';
    els.color.value = '#0EA5B5';
    els.color.dispatchEvent(new Event('change', { bubbles: true }));
    // Reset the cover on every open so a picked-then-cancelled image can
    // never ride along into the next package.
    els.coverFile.value = '';
    els.coverUrl.value = '';
    renderCover('');
    els.access.open = false;
    els.groups.innerHTML = '';
    els.err.hidden = true;
    els.submit.disabled = false;
    els.submit.textContent = 'Create package';

    els.root.hidden = false;
    els.root.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    els.body.scrollTop = 0;
    setTimeout(function () { els.name.focus({ preventScroll: true }); }, 60);
  }

  function close() {
    if (!els) return;
    els.root.classList.remove('is-open');
    els.root.hidden = true;
    document.body.style.overflow = '';
    var s = st;
    st = null;
    if (s && s.restoreFocus && s.restoreFocus.focus) {
      s.restoreFocus.focus({ preventScroll: true });
    }
  }

  function fail(msg) {
    els.err.textContent = msg;
    els.err.hidden = false;
    els.submit.disabled = false;
  }

  /* ── Cover image ──────────────────────────────────────────────────────
     Uploaded on pick rather than on save: the admin gets the preview and
     any failure immediately, and the returned URL rides along on the
     create body. */

  function renderCover(url) {
    els.coverClear.hidden = !url;
    if (!url) { els.coverPreview.textContent = 'No image'; return; }
    els.coverPreview.innerHTML = '<img src="' + esc(url) + '" alt="">';
  }

  function clearCover() {
    els.coverFile.value = '';
    els.coverUrl.value = '';
    renderCover('');
  }

  function onCoverPicked() {
    var file = els.coverFile.files && els.coverFile.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      clearCover();
      fail('That image is larger than 5 MiB.');
      return;
    }
    els.err.hidden = true;
    els.coverPreview.textContent = 'Uploading…';
    var fd = new FormData();
    fd.append('file', file);
    fetch(COVER_API, { method: 'POST', credentials: 'include', body: fd })
      .then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            throw new Error((b && b.detail) || 'HTTP ' + r.status);
          });
        }
        return r.json();
      })
      .then(function (body) {
        els.coverUrl.value = body.url || '';
        renderCover(els.coverUrl.value);
      })
      .catch(function (e) {
        clearCover();
        fail('The cover image did not upload: ' + e.message);
      });
  }

  /* ── Who gets it ──────────────────────────────────────────────────────
     Groups are lazy — most packages are created and shared later from the
     package's own page or a group's Access tab, and this is a request the
     collapsed state should not have made. */

  function hydrateGroups() {
    if (st.groupsLoaded) return;
    st.groupsLoaded = true;
    els.groups.innerHTML = '<p class="ds-drawer__empty">Loading groups…</p>';
    api(GROUPS_API).then(function (body) {
      var groups = Array.isArray(body) ? body : (body && body.groups) || [];
      if (!groups.length) {
        els.groups.innerHTML = '<p class="ds-drawer__empty">No groups yet — make one in '
          + '<a href="/admin/groups">Access</a>, then share this from the package’s page.</p>';
        return;
      }
      els.groups.innerHTML = groups.map(function (g) {
        var gid = String(g.id || g.name || '');
        var gname = String(g.name || gid);
        var n = g.member_count;
        var sub = typeof n === 'number' ? n + (n === 1 ? ' member' : ' members') : '';
        return '<div class="pdw-pick" data-group-id="' + esc(gid) + '">'
          + '<input type="checkbox" aria-label="Share with ' + esc(gname) + '">'
          + '<span class="pdw-pick__txt">'
          + '<span class="pdw-pick__name">' + esc(gname) + '</span>'
          + (sub ? '<span class="pdw-pick__sub">' + esc(sub) + '</span>' : '')
          + '</span>'
          // The tier's system word (what the API and the audit log call it)
          // rides the accessible name rather than a bare `title`: a tooltip
          // is not a label, and these two buttons are the one place in the
          // product where the reader's word and the system's differ.
          + '<span class="fbar-seg" role="group" aria-label="Access tier for ' + esc(gname) + '">'
          + '<button type="button" class="fbar-seg__btn" data-tier="available" aria-pressed="false"'
          + ' aria-label="Optional (available)">Optional</button>'
          + '<button type="button" class="fbar-seg__btn" data-tier="required" aria-pressed="false"'
          + ' aria-label="Automatic (required)">Automatic</button>'
          + '</span>'
          + '</div>';
      }).join('');
    }).catch(function (e) {
      st.groupsLoaded = false;
      els.groups.innerHTML = '<p class="ds-drawer__empty">Could not load the groups: '
        + esc(e.message) + '</p>';
    });
  }

  /* Chosen tiers, as [{group_id, requirement}]. A ticked group with no tier
     clicked means Optional. */
  function chosenGrants() {
    var out = [];
    els.groups.querySelectorAll('[data-group-id]').forEach(function (row) {
      var box = row.querySelector('input[type="checkbox"]');
      if (!box || !box.checked) return;
      var on = row.querySelector('.fbar-seg__btn.is-active');
      out.push({
        group_id: row.getAttribute('data-group-id'),
        requirement: (on && on.dataset.tier) || 'available',
      });
    });
    return out;
  }

  /* ── Create ───────────────────────────────────────────────────────── */

  function submit() {
    var name = els.name.value.trim();
    // The slug follows the name, so a create can only be missing one if the
    // admin cleared it by hand — re-derive rather than bounce them.
    var slug = els.slug.value.trim() || slugify(name);
    els.slug.value = slug;
    els.err.hidden = true;
    if (!name) {
      fail('A name is required — the slug derives from it.');
      els.name.focus({ preventScroll: true });
      return;
    }
    if (!slug) {
      fail('That name has no URL-safe characters in it — give the slug a value.');
      els.slug.focus({ preventScroll: true });
      return;
    }

    els.submit.disabled = true;
    els.submit.textContent = 'Creating…';
    var grants = chosenGrants();

    api(PKG_API, {
      method: 'POST',
      body: JSON.stringify({
        name: name,
        slug: slug,
        description: els.desc.value.trim() || null,
        icon: els.icon.value.trim() || null,
        color: els.color.value.trim() || null,
        cover_image_url: els.coverUrl.value || null,
        status: els.status.value || 'prod',
        category: els.category.value.trim() || null,
      }),
    }).then(function (created) {
      // The create endpoint answers with `{id}` and nothing else, so the
      // package handed to the caller is what we sent plus that id — a
      // callback reading `pkg.name` would otherwise get `undefined` (which
      // is exactly what the chip and the toast used to show).
      var pkg = { id: created.id, name: name, slug: slug };
      // Grants are secondary: a failed one does NOT roll the package back
      // (it exists, and both Access editors can write the grant), so the
      // count rides out to the caller for its own message.
      return Promise.allSettled(grants.map(function (g) {
        return api(GRANTS_API, {
          method: 'POST',
          body: JSON.stringify({
            group_id: g.group_id,
            resource_type: 'data_package',
            resource_id: pkg.id,
            requirement: g.requirement,
          }),
        });
      })).then(function (results) {
        var failures = results.filter(function (r) { return r.status === 'rejected'; }).length;
        var host = st && st.chipHost;
        var done = (st && st.onCreated) || function () {};
        if (host && host.addChip) host.addChip({ id: pkg.id, name: pkg.name });
        close();
        // The package exists by now, so a caller's own error must not be
        // reported as a failed create on a drawer that has already closed.
        try { done(pkg, failures); } catch (_) { /* the caller's problem */ }
      });
    }).catch(function (e) {
      els.submit.textContent = 'Create package';
      fail('Could not create the package: ' + e.message);
    });
  }

  window.AgnesPackageDrawer = { open: open, close: close };
})();
