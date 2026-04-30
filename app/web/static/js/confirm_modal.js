/* Global destructive-confirm dialog.
 *
 * Replaces ad-hoc ``window.confirm(...)`` calls across admin pages with
 * a styled modal that matches the rest of the admin surface. Self-contained:
 * the script injects its own CSS once on first call and builds the modal DOM
 * per-invocation (removed on close), so no template partial / no global
 * ``<div>`` needs to be present in every page.
 *
 * Usage::
 *
 *   if (!await window.confirmDestructive({
 *     title: "Revoke token?",
 *     body: "This token will stop working immediately.",
 *     confirmLabel: "Revoke",
 *   })) return;
 *
 * Returns ``Promise<boolean>``. Closing via Esc / backdrop / Cancel
 * resolves to ``false``; the OK button resolves to ``true``.
 */
(function () {
  if (window.confirmDestructive) return;  // idempotent across hot reloads

  var STYLE_ID = "confirm-destructive-styles";
  var ROOT_CLASS = "confirm-destructive-root";

  function injectStylesOnce() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = [
      "." + ROOT_CLASS + " {",
      "  position: fixed; inset: 0; z-index: 9999;",
      "  display: flex; align-items: center; justify-content: center;",
      "  background: rgba(15, 23, 42, 0.55);",
      "  animation: confirm-fade-in 0.12s ease-out;",
      "}",
      "@keyframes confirm-fade-in { from { opacity: 0; } to { opacity: 1; } }",
      "." + ROOT_CLASS + " .cd-card {",
      "  background: var(--surface, #fff);",
      "  border-radius: 10px;",
      "  box-shadow: 0 10px 30px rgba(0,0,0,0.18);",
      "  padding: 22px 22px 18px;",
      "  width: min(420px, calc(100vw - 32px));",
      "  font-family: var(--font-primary, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif);",
      "}",
      "." + ROOT_CLASS + " h3 {",
      "  margin: 0 0 8px; font-size: 17px; font-weight: 600;",
      "  color: var(--text-primary, #111827);",
      "}",
      "." + ROOT_CLASS + " p {",
      "  margin: 0 0 18px; font-size: 13.5px; line-height: 1.5;",
      "  color: var(--text-secondary, #4b5563);",
      "}",
      "." + ROOT_CLASS + " .cd-actions {",
      "  display: flex; gap: 8px; justify-content: flex-end;",
      "}",
      "." + ROOT_CLASS + " button {",
      "  padding: 8px 16px; border-radius: 8px;",
      "  font-size: 13px; font-weight: 500;",
      "  cursor: pointer; border: 1px solid transparent;",
      "  font-family: inherit;",
      "}",
      "." + ROOT_CLASS + " .cd-cancel {",
      "  background: var(--border-light, #f3f4f6);",
      "  border-color: var(--border-color, #e5e7eb);",
      "  color: var(--text-primary, #111827);",
      "}",
      "." + ROOT_CLASS + " .cd-cancel:hover { filter: brightness(0.97); }",
      "." + ROOT_CLASS + " .cd-ok {",
      "  background: #b91c1c; color: #fff;",
      "}",
      "." + ROOT_CLASS + " .cd-ok:hover { background: #991b1b; }",
      "." + ROOT_CLASS + " .cd-ok.is-primary {",
      "  background: var(--primary, #0073D1);",
      "}",
      "." + ROOT_CLASS + " .cd-ok.is-primary:hover { filter: brightness(0.95); }",
    ].join("\n");
    document.head.appendChild(s);
  }

  /**
   * @param {object} opts
   * @param {string} opts.title
   * @param {string} [opts.body]
   * @param {string} [opts.confirmLabel='OK']
   * @param {string} [opts.cancelLabel='Cancel']
   * @param {('danger'|'primary')} [opts.kind='danger']
   * @returns {Promise<boolean>}
   */
  window.confirmDestructive = function (opts) {
    opts = opts || {};
    var title = String(opts.title || "Are you sure?");
    var body = opts.body == null ? "" : String(opts.body);
    var confirmLabel = String(opts.confirmLabel || "OK");
    var cancelLabel = String(opts.cancelLabel || "Cancel");
    var kind = opts.kind === "primary" ? "primary" : "danger";

    injectStylesOnce();

    return new Promise(function (resolve) {
      var root = document.createElement("div");
      root.className = ROOT_CLASS;
      root.setAttribute("role", "dialog");
      root.setAttribute("aria-modal", "true");
      root.setAttribute("aria-labelledby", "cd-title");

      var card = document.createElement("div");
      card.className = "cd-card";

      var h = document.createElement("h3");
      h.id = "cd-title";
      h.textContent = title;
      card.appendChild(h);

      if (body) {
        var p = document.createElement("p");
        p.textContent = body;
        card.appendChild(p);
      }

      var actions = document.createElement("div");
      actions.className = "cd-actions";

      var cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "cd-cancel";
      cancelBtn.textContent = cancelLabel;

      var okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "cd-ok" + (kind === "primary" ? " is-primary" : "");
      okBtn.textContent = confirmLabel;

      actions.appendChild(cancelBtn);
      actions.appendChild(okBtn);
      card.appendChild(actions);
      root.appendChild(card);
      document.body.appendChild(root);

      // Focus the OK button so Enter immediately confirms — matches
      // ``confirm()``'s default-on-affirmative behavior.
      setTimeout(function () { okBtn.focus(); }, 0);

      function cleanup(value) {
        document.removeEventListener("keydown", onKey);
        if (root.parentNode) root.parentNode.removeChild(root);
        resolve(value);
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); cleanup(false); }
        else if (e.key === "Enter") { e.preventDefault(); cleanup(true); }
      }
      document.addEventListener("keydown", onKey);

      cancelBtn.addEventListener("click", function () { cleanup(false); });
      okBtn.addEventListener("click", function () { cleanup(true); });
      root.addEventListener("click", function (e) {
        if (e.target === root) cleanup(false);
      });
    });
  };
})();
