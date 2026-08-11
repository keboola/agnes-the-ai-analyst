# Vendored third-party assets

These files are committed verbatim — no build step — so the cloud-chat
web UI works on a fresh deployment without an offline asset pipeline.

## marked.min.js

- **Project:** [marked](https://github.com/markedjs/marked) — Markdown parser
- **Version:** 12.0.2
- **License:** MIT
- **Source:** https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js
- **Used in:** `app/web/templates/chat.html` (rendering assistant Markdown
  replies in the `/chat` web UI).

## highlight.min.js

- **Project:** [highlight.js](https://github.com/highlightjs/highlight.js) — syntax highlighter
- **Version:** 11.10.0 (CDN "common" build — ~30 languages incl. bash, sql,
  python, json, yaml, javascript)
- **License:** BSD-3-Clause
- **Source:** https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js
- **Used in:** `app/web/templates/chat.html` (code-block highlighting inside
  the `/chat` web UI) and `admin_chat.html`.

## highlight.min.css

- **Project:** highlight.js — `styles/github.min.css` theme
- **Version:** 11.10.0
- **License:** BSD-3-Clause
- **Source:** https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github.min.css

## mermaid.min.js

- **Project:** [mermaid](https://github.com/mermaid-js/mermaid) — diagrams from text
- **Version:** 11.16.1
- **License:** MIT
- **Source:** https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js
- **Used in:** `app/web/static/js/chat.js` (` ```mermaid ` fences in assistant
  replies).
- **Size:** 3.5 MB — by far the largest asset here, so it is **not** loaded
  with the page. `renderMermaidBlocks()` injects the script only when a
  message actually contains a diagram, and caches the promise for the session;
  a user who never sees one never downloads it. Do not move it into a
  `<script>` tag in the template.
- **Why the single-file build and not the ESM one:** `mermaid.esm.min.mjs`
  code-splits and fetches its own chunks at runtime, which a verbatim
  single-file vendoring cannot serve. This build ends with
  `globalThis["mermaid"] = …`, so a plain script tag is enough.

## Updating

To refresh a vendored asset:

```bash
cd app/web/static/vendor
curl -sSL -o marked.min.js  https://cdn.jsdelivr.net/npm/marked@<VER>/marked.min.js
curl -sSL -o highlight.min.js  https://cdnjs.cloudflare.com/ajax/libs/highlight.js/<VER>/highlight.min.js
curl -sSL -o highlight.min.css https://cdnjs.cloudflare.com/ajax/libs/highlight.js/<VER>/styles/github.min.css
curl -sSL -o mermaid.min.js https://cdn.jsdelivr.net/npm/mermaid@<VER>/dist/mermaid.min.js
```

Then update the version numbers above in the same commit.
