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

## Updating

To refresh a vendored asset:

```bash
cd app/web/static/vendor
curl -sSL -o marked.min.js  https://cdn.jsdelivr.net/npm/marked@<VER>/marked.min.js
curl -sSL -o highlight.min.js  https://cdnjs.cloudflare.com/ajax/libs/highlight.js/<VER>/highlight.min.js
curl -sSL -o highlight.min.css https://cdnjs.cloudflare.com/ajax/libs/highlight.js/<VER>/styles/github.min.css
```

Then update the version numbers above in the same commit.
