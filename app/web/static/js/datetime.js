// app/web/static/js/datetime.js
//
// Single source of truth for rendering timestamps in the web UI.
// Renders in the browser's local timezone; preserves the UTC literal in
// the element's title attribute (tooltip + no-JS fallback).
//
// Contract: every <time datetime="ISO_WITH_OFFSET"> in the DOM has its
// text content replaced with the local representation. Idempotent — the
// hydrator sets data-hydrated="1" so AJAX re-runs do not double-format.

(function () {
  "use strict";

  function parseIso(s) {
    if (!s) return null;
    // Defensive: a caller omitting the offset gets treated as UTC. The
    // server serializer (`app/serialization.py`) should make this branch
    // unreachable for API-emitted values.
    if (typeof s === "string" && /T\d{2}:\d{2}/.test(s) && !/(Z|[+\-]\d{2}:?\d{2})$/.test(s)) {
      s = s + "Z";
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function formatDateTime(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
           " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function formatDate(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function formatRelative(iso) {
    var d = parseIso(iso);
    if (!d) return "";
    var sec = Math.round((Date.now() - d.getTime()) / 1000);
    if (sec < 0) sec = 0;
    if (sec < 45) return "just now";
    if (sec < 90) return "1m ago";
    var min = Math.round(sec / 60);
    if (min < 45) return min + "m ago";
    if (min < 90) return "1h ago";
    var hr = Math.round(min / 60);
    if (hr < 24) return hr + "h ago";
    var day = Math.round(hr / 24);
    if (day < 7) return day + "d ago";
    return formatDate(iso);
  }

  function hydrateTimes(root) {
    root = root || document;
    var nodes = root.querySelectorAll("time[datetime]:not([data-hydrated])");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var iso = el.getAttribute("datetime");
      var label = formatDateTime(iso);
      if (!label) continue;
      // Preserve any UTC label currently in the element as the tooltip,
      // unless the caller already set a title.
      if (!el.hasAttribute("title")) {
        var raw = (el.textContent || "").trim();
        if (raw) el.setAttribute("title", raw);
      }
      el.textContent = label;
      el.setAttribute("data-hydrated", "1");
    }
  }

  window.AgnesTime = {
    parse: parseIso,
    formatDateTime: formatDateTime,
    formatDate: formatDate,
    formatRelative: formatRelative,
    hydrateTimes: hydrateTimes,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { hydrateTimes(); });
  } else {
    hydrateTimes();
  }
})();
