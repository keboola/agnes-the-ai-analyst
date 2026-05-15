/**
 * FilterState — client-side persistence for filter UI widgets.
 *
 * Why this exists:
 *   ~7 admin/user pages have similar filter bars (search input, category
 *   buttons, dropdowns, "hide dismissed" checkboxes). Each used to roll
 *   its own localStorage glue inline in <script> blocks. This utility
 *   centralises the pattern so a page only declares *what* to persist,
 *   not *how*.
 *
 * Public API (attached to window.FilterState):
 *   save(scopeKey, params)               — persist a key→value map.
 *   load(scopeKey) -> object             — restore params or {}.
 *   clear(scopeKey)                      — erase stored state.
 *   bindInputs(scopeKey, descriptors)    — hydrate + auto-persist DOM inputs.
 *
 * Storage layout:
 *   Key:   "agnes:filters:<scopeKey>"   (e.g. "agnes:filters:cm_v1")
 *   Value: JSON object, e.g. {"q":"foo","category":"finance","hide":true}
 *   Versioning is the caller's responsibility — bump the suffix
 *   (cm_v1 → cm_v2) when you change the filter shape; the old store
 *   simply becomes orphaned.
 *
 * Usage example (Corporate Memory page wiring):
 *
 *   FilterState.bindInputs('cm_v1', [
 *     { id: 'searchInput', param: 'q', event: 'input' },
 *     { id: 'sortSelect',  param: 'sort' },
 *     { id: 'hideDismissed', param: 'hideDismissed' },          // checkbox
 *     {
 *       id: 'categoryGroup', param: 'category',
 *       buttonGroup: {
 *         selector: '.filter-btn[data-category]',
 *         dataAttr: 'category',
 *       },
 *     },
 *   ]);
 *
 * Browser support:
 *   Depends on localStorage + ES6 (let/const, arrow fns, template
 *   strings, Array.isArray). No async, no modules — plain <script src>
 *   compatible with the rest of this codebase. All storage access is
 *   wrapped in try/catch (Safari private mode, quota exceeded, etc.).
 */
(function () {
    'use strict';

    var PREFIX = 'agnes:filters:';

    // ---- internal helpers ----------------------------------------------------

    function isNonEmptyString(v) {
        return typeof v === 'string' && v.length > 0;
    }

    function isPlainObject(v) {
        return v !== null && typeof v === 'object' && !Array.isArray(v);
    }

    function isAllowedValue(v) {
        if (v === null || v === undefined) return true;
        var t = typeof v;
        if (t === 'string' || t === 'boolean' || t === 'number') return true;
        if (Array.isArray(v)) {
            return v.every(function (x) { return typeof x === 'string'; });
        }
        return false;
    }

    function safeGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (err) {
            console.warn('[FilterState] localStorage.getItem failed:', err);
            return null;
        }
    }

    function safeSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (err) {
            console.warn('[FilterState] localStorage.setItem failed:', err);
        }
    }

    function safeRemove(key) {
        try {
            window.localStorage.removeItem(key);
        } catch (err) {
            console.warn('[FilterState] localStorage.removeItem failed:', err);
        }
    }

    function storageKey(scopeKey) {
        return PREFIX + scopeKey;
    }

    function validateScope(scopeKey, method) {
        if (!isNonEmptyString(scopeKey)) {
            console.warn('[FilterState.' + method + '] scopeKey must be a non-empty string; got:', scopeKey);
            return false;
        }
        return true;
    }

    // Apply a stored value to an element based on its tag/type.
    function applyValueToElement(el, value, descriptor) {
        if (descriptor.buttonGroup) {
            var group = descriptor.buttonGroup;
            var buttons = document.querySelectorAll(group.selector);
            buttons.forEach(function (btn) {
                if (btn.dataset[group.dataAttr] === value) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
            return;
        }
        if (el.tagName === 'INPUT' && el.type === 'checkbox') {
            el.checked = Boolean(value);
            return;
        }
        // <select>, <input type="text">, <input type="search">, <textarea>, etc.
        el.value = value == null ? '' : String(value);
    }

    // Read an element's current value back out.
    function readValueFromElement(el, descriptor) {
        if (descriptor.buttonGroup) {
            var group = descriptor.buttonGroup;
            var active = document.querySelector(group.selector + '.active');
            return active ? active.dataset[group.dataAttr] || '' : '';
        }
        if (el.tagName === 'INPUT' && el.type === 'checkbox') {
            return el.checked;
        }
        return el.value;
    }

    // ---- public API ---------------------------------------------------------

    function save(scopeKey, params) {
        if (!validateScope(scopeKey, 'save')) return;
        if (!isPlainObject(params)) {
            console.warn('[FilterState.save] params must be a plain object; got:', params);
            return;
        }
        var clean = {};
        Object.keys(params).forEach(function (k) {
            var v = params[k];
            if (isAllowedValue(v)) {
                clean[k] = v;
            } else {
                console.warn('[FilterState.save] dropping unsupported value for "' + k + '":', v);
            }
        });
        try {
            safeSet(storageKey(scopeKey), JSON.stringify(clean));
        } catch (err) {
            console.warn('[FilterState.save] JSON.stringify failed:', err);
        }
    }

    function load(scopeKey) {
        if (!validateScope(scopeKey, 'load')) return {};
        var raw = safeGet(storageKey(scopeKey));
        if (!raw) return {};
        var parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (err) {
            console.warn('[FilterState.load] JSON.parse failed for scope "' + scopeKey + '":', err);
            return {};
        }
        if (!isPlainObject(parsed)) {
            console.warn('[FilterState.load] stored shape is not an object for scope "' + scopeKey + '"; ignoring.');
            return {};
        }
        return parsed;
    }

    function clear(scopeKey) {
        if (!validateScope(scopeKey, 'clear')) return;
        safeRemove(storageKey(scopeKey));
    }

    function bindInputs(scopeKey, descriptors) {
        if (!validateScope(scopeKey, 'bindInputs')) return;
        if (!Array.isArray(descriptors)) {
            console.warn('[FilterState.bindInputs] descriptors must be an array; got:', descriptors);
            return;
        }

        var stored = load(scopeKey);

        descriptors.forEach(function (d) {
            if (!d || !isNonEmptyString(d.id) || !isNonEmptyString(d.param)) {
                console.warn('[FilterState.bindInputs] skipping invalid descriptor:', d);
                return;
            }
            var el = document.getElementById(d.id);
            if (!el) {
                console.warn('[FilterState.bindInputs] no element with id "' + d.id + '"');
                return;
            }

            // Hydrate from storage if we have a value for this param.
            if (Object.prototype.hasOwnProperty.call(stored, d.param)) {
                try {
                    applyValueToElement(el, stored[d.param], d);
                } catch (err) {
                    console.warn('[FilterState.bindInputs] hydrate failed for "' + d.id + '":', err);
                }
            }

            // Idempotency: skip if already bound for this scope+id.
            var boundFlag = scopeKey + ':' + d.param;
            var existing = el.dataset.__filterStateBound || '';
            if (existing.split('|').indexOf(boundFlag) !== -1) {
                return;
            }
            el.dataset.__filterStateBound = existing
                ? existing + '|' + boundFlag
                : boundFlag;

            // Pick a sensible default event.
            var evtName = d.event;
            if (!evtName) {
                if (el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'search')) {
                    evtName = 'input';
                } else {
                    evtName = 'change';
                }
            }

            var persist = function () {
                var current = load(scopeKey);
                current[d.param] = readValueFromElement(el, d);
                save(scopeKey, current);
            };

            if (d.buttonGroup) {
                // For button groups the "element" is conceptually the group;
                // attach click listeners to each button. The descriptor's
                // own el is just the marker for idempotency tracking.
                var buttons = document.querySelectorAll(d.buttonGroup.selector);
                buttons.forEach(function (btn) {
                    btn.addEventListener('click', persist);
                });
            } else {
                el.addEventListener(evtName, persist);
            }
        });
    }

    window.FilterState = {
        save: save,
        load: load,
        clear: clear,
        bindInputs: bindInputs,
    };

    // ---- MANUAL SMOKE TEST --------------------------------------------------
    // Paste in console:
    //   window.__filterStateDevSmoke = true;
    //   // then reload the page (or re-eval the script).
    (function () {
        if (typeof window === 'undefined' || window.__filterStateDevSmoke !== true) return;
        try {
            var scope = '__smoke_' + Date.now();
            var sample = { q: 'hi', n: 3, flag: true, tags: ['a', 'b'] };
            save(scope, sample);
            var got = load(scope);
            var ok = got && got.q === 'hi' && got.n === 3 && got.flag === true &&
                Array.isArray(got.tags) && got.tags.length === 2 &&
                got.tags[0] === 'a' && got.tags[1] === 'b';
            if (!ok) throw new Error('roundtrip mismatch: ' + JSON.stringify(got));
            clear(scope);
            var after = load(scope);
            if (!isPlainObject(after) || Object.keys(after).length !== 0) {
                throw new Error('clear did not empty store: ' + JSON.stringify(after));
            }
            console.info('[FilterState] smoke OK');
        } catch (err) {
            console.error('[FilterState] smoke FAILED:', err);
        }
    })();
})();
