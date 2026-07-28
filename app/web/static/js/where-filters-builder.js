/**
 * where-filters-builder.js — structured editor for Keboola `where_filters`.
 *
 * Why this exists:
 *   Registering a Keboola Direct-extract (Storage API) table lets the
 *   operator attach server-side row filters. The only editor used to be a
 *   raw-JSON textarea — error-prone and inaccessible to non-technical
 *   operators (issue #408). This module renders a structured builder on
 *   top of that field: a column + operator + values row repeater, plus a
 *   date-range convenience that emits the two boundary rows. It serialises
 *   to the EXACT JSON array shape the backend already accepts
 *   (`[{column, operator, values}]`), keeping the produced payload
 *   byte-compatible with `connectors/keboola/where_filters.py:parse_filters`
 *   and the `RegisterTableRequest.where_filters` validator.
 *
 * Contract (mirrors connectors/keboola/where_filters.py):
 *   - Each filter entry is `{column, operator, values}`.
 *   - operator ∈ {eq, ne, gt, ge, lt, le}. No LIKE / OR / nesting.
 *   - values is a non-empty list of strings. Multiple values in one entry
 *     are IN'd (eq) / NOT-IN'd (ne).
 *   - Date placeholders (`{{today}}`, `{{last_3_months}}`, …) pass through
 *     verbatim — they are resolved server-side at sync time, never here.
 *
 * Design notes:
 *   - PURE serialisation lives in `serializeFilterRows()` /
 *     `dateRangeRows()` — no DOM, no globals — so they are unit-testable
 *     under plain `node` (tests/test_admin_keboola_where_filters_builder.py).
 *   - The builder is the source of truth, but it writes its JSON into the
 *     EXISTING hidden textarea (`kbWhereFilters` / `editKbWhereFilters`) on
 *     every change. The payload-building functions in admin_tables.html read
 *     that textarea unchanged, so no backend or submit-path edits were
 *     needed. A "raw JSON" escape hatch toggles the textarea visible for
 *     power users; edits there flow back into the builder on toggle-off.
 *
 * Browser support: ES6 (const/let, arrow fns, template literals). No deps.
 */
(function (global) {
  'use strict';

  var OPERATORS = ['eq', 'ne', 'gt', 'ge', 'lt', 'le'];
  var OPERATOR_LABELS = {
    eq: '=  (equals / in)',
    ne: '≠  (not equals / not in)',
    gt: '>  (greater than)',
    ge: '≥  (greater or equal)',
    lt: '<  (less than)',
    le: '≤  (less or equal)',
  };

  // ───────────────────────────── pure helpers ──────────────────────────────

  /**
   * Split a comma-separated values string into a trimmed, non-empty list.
   * Mirrors the Storage API's IN-list semantics; placeholders pass through.
   */
  function splitValues(raw) {
    if (raw == null) return [];
    return String(raw)
      .split(',')
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  /**
   * True iff any value in a parsed filter array contains a comma. The CSV
   * row editor uses comma as its IN-list delimiter, so a stored value like
   * "Smith, John" cannot round-trip through it without silently splitting
   * into two values. When this returns true the builder refuses to render
   * its lossy editor and defers to the raw-JSON textarea (#649 review).
   */
  function parsedHasCommaValue(parsed) {
    if (!Array.isArray(parsed)) return false;
    return parsed.some(function (f) {
      return f && Array.isArray(f.values) && f.values.some(function (v) {
        return typeof v === 'string' && v.indexOf(',') !== -1;
      });
    });
  }

  /**
   * Serialise an array of {column, operator, values} row descriptors into
   * the backend filter-array shape. `values` may be a string (CSV) or an
   * array. Rows with a blank column OR no values are dropped (a half-typed
   * row should not emit an invalid filter). Returns [] when nothing valid.
   */
  function serializeFilterRows(rows) {
    var out = [];
    (rows || []).forEach(function (row) {
      if (!row) return;
      var column = (row.column == null ? '' : String(row.column)).trim();
      if (!column) return;
      var op = OPERATORS.indexOf(row.operator) >= 0 ? row.operator : 'eq';
      var values = Array.isArray(row.values) ? row.values : splitValues(row.values);
      values = values
        .map(function (v) { return String(v).trim(); })
        .filter(function (v) { return v.length > 0; });
      if (!values.length) return;
      out.push({ column: column, operator: op, values: values });
    });
    return out;
  }

  /**
   * Build the boundary rows for an inclusive date range on `column`.
   * `from`/`to` may be ISO dates or date placeholders. Either bound may be
   * blank → that side is omitted. Returns [] when both blank or no column.
   *   from → {column, operator:'ge', values:[from]}
   *   to   → {column, operator:'le', values:[to]}
   */
  function dateRangeRows(column, from, to) {
    var col = (column == null ? '' : String(column)).trim();
    if (!col) return [];
    var rows = [];
    var f = (from == null ? '' : String(from)).trim();
    var t = (to == null ? '' : String(to)).trim();
    if (f) rows.push({ column: col, operator: 'ge', values: [f] });
    if (t) rows.push({ column: col, operator: 'le', values: [t] });
    return rows;
  }

  /**
   * Produce the final JSON string written to the hidden textarea. Returns
   * '' (empty) when there are no valid filters — the submit path treats an
   * empty textarea as "no filters" (null), so this stays byte-compatible.
   */
  function rowsToJSON(rows) {
    var filters = serializeFilterRows(rows);
    return filters.length ? JSON.stringify(filters) : '';
  }

  // ─────────────────────────────── DOM builder ─────────────────────────────
  // The DOM layer is deliberately thin; all logic lives in the pure helpers
  // above so it survives a unit test without a browser.

  function _el(tag, attrs, children) {
    var node = global.document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  /**
   * Attach a structured builder to a host element, syncing into a hidden
   * textarea. `opts.host` (element), `opts.textarea` (element). Idempotent:
   * a second call rebuilds from the textarea's current JSON.
   */
  function attach(opts) {
    var host = opts.host;
    var textarea = opts.textarea;
    if (!host || !textarea) return null;

    var state = { rows: [], rangeColumn: '', rangeFrom: '', rangeTo: '', rawOnly: false };

    function syncTextarea() {
      // In raw-only mode the textarea is the source of truth (it holds
      // comma-bearing values the CSV editor can't represent); never
      // overwrite it from the builder, or the round-trip would corrupt
      // those values (#649 review).
      if (state.rawOnly) return;
      var rows = state.rows.concat(
        dateRangeRows(state.rangeColumn, state.rangeFrom, state.rangeTo)
      );
      textarea.value = rowsToJSON(rows);
    }

    function hydrateFromTextarea() {
      state.rows = [];
      state.rangeColumn = state.rangeFrom = state.rangeTo = '';
      state.rawOnly = false;
      var raw = (textarea.value || '').trim();
      if (raw) {
        try {
          var parsed = JSON.parse(raw);
          if (parsedHasCommaValue(parsed)) {
            // A stored value contains a comma — the CSV row editor would
            // silently split it on the next interaction. Refuse to render
            // the lossy editor; the operator edits the raw JSON instead
            // (#649 review).
            state.rawOnly = true;
          } else if (Array.isArray(parsed)) {
            parsed.forEach(function (f) {
              state.rows.push({
                column: f.column || '',
                operator: f.operator || 'eq',
                values: Array.isArray(f.values) ? f.values.join(', ') : '',
              });
            });
          }
        } catch (e) { /* leave builder empty; raw-JSON hatch shows the text */ }
      }
      render();
    }

    function addRow() {
      state.rows.push({ column: '', operator: 'eq', values: '' });
      render();
      syncTextarea();
    }

    function render() {
      host.innerHTML = '';

      // Raw-only mode: a stored value contains a comma the CSV editor can't
      // represent losslessly. Show a notice instead of the lossy row UI and
      // leave the textarea (raw JSON) untouched as the source of truth.
      if (state.rawOnly) {
        host.appendChild(_el('div', { class: 'form-hint wfb-rawonly-notice' },
          [_el('span', {
            text: 'A filter value contains a comma, which the structured '
              + 'editor can’t represent. Use “Edit raw JSON” '
              + 'to view or change these filters.',
          })]));
        return;
      }

      // Filter rows
      state.rows.forEach(function (row, idx) {
        var colInput = _el('input', {
          class: 'form-input wfb-col', type: 'text', placeholder: 'column',
          value: row.column,
        });
        colInput.addEventListener('input', function () {
          row.column = colInput.value; syncTextarea();
        });

        var opSelect = _el('select', { class: 'form-input wfb-op' });
        OPERATORS.forEach(function (op) {
          var o = _el('option', { value: op, text: OPERATOR_LABELS[op] });
          if (op === row.operator) o.setAttribute('selected', 'selected');
          opSelect.appendChild(o);
        });
        opSelect.value = row.operator;
        opSelect.addEventListener('change', function () {
          row.operator = opSelect.value; syncTextarea();
        });

        var valInput = _el('input', {
          class: 'form-input wfb-val', type: 'text',
          placeholder: 'value(s), comma-separated', value: row.values,
        });
        valInput.addEventListener('input', function () {
          row.values = valInput.value; syncTextarea();
        });

        var rm = _el('button', {
          class: 'btn btn-secondary wfb-remove', type: 'button', text: '×',
          title: 'Remove filter',
        });
        rm.addEventListener('click', function () {
          state.rows.splice(idx, 1); render(); syncTextarea();
        });

        host.appendChild(_el('div', { class: 'wfb-row' },
          [colInput, opSelect, valInput, rm]));
      });

      var addBtn = _el('button', {
        class: 'btn btn-secondary wfb-add', type: 'button',
        text: '+ Add filter',
      });
      addBtn.addEventListener('click', addRow);
      host.appendChild(addBtn);

      // Date-range convenience
      var rangeCol = _el('input', {
        class: 'form-input wfb-range-col', type: 'text',
        placeholder: 'date column', value: state.rangeColumn,
      });
      rangeCol.addEventListener('input', function () {
        state.rangeColumn = rangeCol.value; syncTextarea();
      });
      var rangeFrom = _el('input', {
        class: 'form-input wfb-range-from', type: 'text',
        placeholder: 'from (e.g. {{last_3_months}})', value: state.rangeFrom,
      });
      rangeFrom.addEventListener('input', function () {
        state.rangeFrom = rangeFrom.value; syncTextarea();
      });
      var rangeTo = _el('input', {
        class: 'form-input wfb-range-to', type: 'text',
        placeholder: 'to (e.g. {{today}})', value: state.rangeTo,
      });
      rangeTo.addEventListener('input', function () {
        state.rangeTo = rangeTo.value; syncTextarea();
      });
      host.appendChild(_el('div', { class: 'wfb-range' }, [
        _el('span', { class: 'form-hint wfb-range-label',
          text: 'Date range (optional):' }),
        rangeCol, rangeFrom, rangeTo,
      ]));
    }

    hydrateFromTextarea();
    return {
      rebuild: hydrateFromTextarea,
      sync: syncTextarea,
      _state: state,
    };
  }

  var api = {
    OPERATORS: OPERATORS,
    splitValues: splitValues,
    parsedHasCommaValue: parsedHasCommaValue,
    serializeFilterRows: serializeFilterRows,
    dateRangeRows: dateRangeRows,
    rowsToJSON: rowsToJSON,
    attach: attach,
  };

  // Browser global + CommonJS (node test) dual export.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (global) {
    global.WhereFiltersBuilder = api;
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
