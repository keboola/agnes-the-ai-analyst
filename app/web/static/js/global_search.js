/* Global header search — combobox over GET /api/knowledge/search (K2, #797).
   Wires the #global-search input + #globalSearchResults listbox shipped by
   _app_header.html. Debounces input, groups results by type (Tables /
   Knowledge / Documents), and links each hit to its detail page:
     - table     -> /catalog/t/<table_id> (falls back to /catalog)
     - knowledge -> /corporate-memory
     - chunk     -> /library
   All API-derived strings are set via textContent — never innerHTML — so a
   malicious document title / table name can't inject markup.
   No global hotkey: Cmd/Ctrl-K is already the admin/chat command palette. */
(function () {
    "use strict";

    var input = document.getElementById("global-search");
    var panel = document.getElementById("globalSearchResults");
    if (!input || !panel) return;

    var DEBOUNCE_MS = 250;
    var MIN_CHARS = 2;

    var GROUPS = [
        { type: "table", heading: "Tables" },
        { type: "knowledge", heading: "Knowledge" },
        { type: "chunk", heading: "Documents" },
    ];
    var TYPE_LABELS = { table: "Table", knowledge: "Knowledge", chunk: "Document" };

    var debounceTimer = null;
    var activeController = null;

    function hrefFor(hit) {
        if (hit.type === "table") {
            return hit.table_id ? "/catalog/t/" + encodeURIComponent(hit.table_id) : "/catalog";
        }
        if (hit.type === "knowledge") return "/corporate-memory";
        if (hit.type === "chunk") return "/library";
        return "#";
    }

    function titleFor(hit) {
        if (hit.type === "table") return hit.name || "Untitled table";
        if (hit.type === "knowledge") return hit.title || "Untitled";
        if (hit.type === "chunk") return hit.filename || "Document";
        return "";
    }

    function setExpanded(open) {
        input.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function closePanel() {
        panel.setAttribute("hidden", "");
        panel.textContent = "";
        setExpanded(false);
    }

    function openPanel() {
        panel.removeAttribute("hidden");
        setExpanded(true);
    }

    function renderMessage(message) {
        panel.textContent = "";
        var el = document.createElement("div");
        el.className = "app-header-search-empty";
        el.textContent = message;
        panel.appendChild(el);
        openPanel();
    }

    function renderResults(results) {
        panel.textContent = "";
        if (!results || !results.length) {
            renderMessage("No results.");
            return;
        }
        GROUPS.forEach(function (group) {
            var hits = results.filter(function (r) { return r.type === group.type; });
            if (!hits.length) return;
            var heading = document.createElement("div");
            heading.className = "app-header-search-group";
            heading.textContent = group.heading;
            panel.appendChild(heading);
            hits.forEach(function (hit) {
                var row = document.createElement("a");
                row.className = "app-header-search-result";
                row.setAttribute("role", "option");
                row.href = hrefFor(hit);

                var titleEl = document.createElement("span");
                titleEl.className = "app-header-search-result-title";
                titleEl.textContent = titleFor(hit);

                var badgeEl = document.createElement("span");
                badgeEl.className = "app-header-search-result-badge";
                badgeEl.textContent = TYPE_LABELS[hit.type] || hit.type;

                row.appendChild(titleEl);
                row.appendChild(badgeEl);
                panel.appendChild(row);
            });
        });
        openPanel();
    }

    function runSearch(query) {
        if (activeController) activeController.abort();
        activeController = ("AbortController" in window) ? new AbortController() : null;
        var url = "/api/knowledge/search?q=" + encodeURIComponent(query) + "&k=8";
        fetch(url, {
            credentials: "same-origin",
            signal: activeController ? activeController.signal : undefined,
        })
            .then(function (r) {
                if (!r.ok) throw new Error("http_" + r.status);
                return r.json();
            })
            .then(function (data) {
                renderResults(data && data.results);
            })
            .catch(function (err) {
                if (err && err.name === "AbortError") return;
                renderMessage("Search failed. Try again.");
            });
    }

    input.addEventListener("input", function () {
        var q = input.value.trim();
        if (debounceTimer) clearTimeout(debounceTimer);
        if (q.length < MIN_CHARS) {
            closePanel();
            return;
        }
        debounceTimer = setTimeout(function () { runSearch(q); }, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            closePanel();
            input.blur();
        }
    });

    document.addEventListener("click", function (e) {
        if (panel.contains(e.target) || e.target === input) return;
        closePanel();
    });
})();
