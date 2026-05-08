# News content authoring guide

This page documents the HTML vocabulary the `/home` news perex and `/news` permalink page recognise. Authors should write content using only these allowed tags, attributes, and class names — anything outside the list is silently stripped on save.

## Where the content lives

A single news entity (intro + content) is stored in DuckDB table `news_template`. Every save creates / updates a row with a monotonically increasing `version`. The latest row with `published = TRUE` is what `/home` and `/news` render. The admin can roll back by unpublishing a newer version (web falls back to the next-highest published version automatically).

Drafts older than 30 days that were never published, and superseded published versions older than 30 days, are pruned on save. The currently-displayed published version is never pruned regardless of age.

## How to author

Two equivalent surfaces:

- Web admin UI: **`/admin/news`** — two textareas (intro + full content) with a sandboxed live preview, a "Format help" cheatsheet, and a versions table with `Unpublish` actions.
- CLI:
  ```bash
  agnes admin news show                 # current published
  agnes admin news draft                # active draft (or none)
  agnes admin news edit \
      --intro '<p>Short HTML perex.</p>' \
      --content '<h1>Title</h1><p>Body.</p>'
  agnes admin news edit --from news.yaml      # YAML/JSON {intro, content}
  agnes admin news publish              # flip active draft → published
  agnes admin news unpublish 5          # roll back v5; web shows next-highest published
  agnes admin news versions
  agnes admin news export news.yaml     # round-trip to a file
  ```

Both surfaces sanitize on save through `src/sanitize_news.py` (Rust-backed `nh3`).

## Allowed tags

```
p, br, hr,
h1, h2, h3, h4, h5, h6,
ul, ol, li,
strong, em, b, i, u, s,
code, pre, blockquote,
a, img,
span, div, section,
table, thead, tbody, tr, th, td,
details, summary,
figure, figcaption,
iframe                       (only with allowlisted src — see below)
```

Anything else (`<script>`, `<style>`, `<object>`, `<embed>`, `<base>`, `<link>`, `<meta>`, `<form>`, `<input>`, all event-handler attributes like `onclick`/`onerror`/`onload`) is stripped.

## URL schemes

- `<a href>`, `<img src>`: `http://`, `https://`, `mailto:`, or relative paths (no scheme). `javascript:` and `data:` are blocked.
- `<a target="_blank">`: the sanitizer auto-injects `rel="noopener noreferrer"` so target-blank links can't pivot the parent window.
- `<iframe src>`: must start with one of the video-host prefixes. Any other src strips the entire iframe element:
  - `https://www.youtube.com/embed/…`, `https://youtube.com/embed/…`, `https://www.youtube-nocookie.com/embed/…`
  - `https://player.vimeo.com/video/…`
  - `https://www.loom.com/embed/…`, `https://www.loom.com/share/…`

## Allowed attributes

| Tag | Attributes |
|---|---|
| `a` | `href`, `title`, `target`, `class` (`rel` is auto-managed by the sanitizer; do not set it manually) |
| `img` | `src`, `alt`, `width`, `height` |
| `iframe` | `src`, `title`, `width`, `height`, `allow`, `allowfullscreen`, `frameborder` |
| `span`, `div`, `section`, `p`, `h1–h6`, `table`, `td`, `th`, `blockquote` | `class` |

The `class` attribute is permitted only on the structural tags listed; it's used for the documented vocabulary below. Custom classes that aren't in the documented list will pass the sanitizer but won't render any styling — there's no CSS for them.

## Documented class vocabulary

Use these classes via copy-and-edit. They render consistently on `/home` (perex) and `/news` (full body).

### Callouts — boxed notice with colored left border

```html
<div class="callout">
  <strong>Note:</strong> A neutral callout.
</div>

<div class="callout callout-info">
  <strong>FYI:</strong> Informational, blue.
</div>

<div class="callout callout-warn">
  <strong>Heads up:</strong> Yellow — needs attention.
</div>

<div class="callout callout-success">
  <strong>Done:</strong> Green — successful release / completed.
</div>

<div class="callout callout-danger">
  <strong>Important:</strong> Red — breaking change / required action.
</div>
```

### Video embed — 16:9 wrapper

Wrap the iframe so it scales fluidly on narrow viewports:

```html
<div class="video-embed">
  <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"
          title="Walkthrough video"
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          allowfullscreen></iframe>
</div>
```

Replace the `src` with the YouTube / Vimeo / Loom embed URL — anything else gets the iframe stripped on save.

### Sections — visual breaks between topics

```html
<section class="news-section">
  <h2>Big release this week</h2>
  <p>Body of this section.</p>
</section>
```

### Two- or three-column grid

```html
<div class="news-grid-2">
  <div>
    <h3>Left column</h3>
    <p>…</p>
  </div>
  <div>
    <h3>Right column</h3>
    <p>…</p>
  </div>
</div>
```

`news-grid-3` works the same with three children. Both collapse to a single column below 720px.

### Call-to-action button

```html
<a class="news-cta" href="/setup-advanced">Open advanced setup</a>
```

## Example: a release note

```html
<!-- intro (shown on /home) -->
<p><strong>Agnes 0.40 is live.</strong> Highlights: new <code>agnes admin news</code>
   command, marketplace plugin discovery, and a 3× faster <code>/catalog</code>.</p>

<!-- content (shown on /news) -->
<section class="news-section">
  <h2>What changed</h2>
  <ul>
    <li><strong>News editor.</strong> Admins can publish updates to <code>/home</code>
        + <code>/news</code> from the web UI or CLI. See
        <a href="/admin/news">/admin/news</a>.</li>
    <li><strong>Marketplace.</strong> The Plugin Store now surfaces newly-released
        community plugins on the homepage.</li>
    <li><strong>Catalog speed-ups.</strong> Schema lookups cache locally; first-page
        load drops from ~800ms to ~250ms.</li>
  </ul>
</section>

<div class="callout callout-warn">
  <strong>Action needed:</strong> If you're on a custom plugin that pinned to
  <code>0.39</code>, bump the pin to <code>0.40</code> after upgrading.
</div>

<section class="news-section">
  <h2>Walkthrough</h2>
  <div class="video-embed">
    <iframe src="https://www.loom.com/embed/example-loom-id"
            allowfullscreen></iframe>
  </div>
</section>

<a class="news-cta" href="/setup-advanced">Open advanced setup</a>
```

## What gets stripped

If you find something missing from the published render, it almost certainly fell into one of these buckets:

- A non-allowlisted tag (`<script>`, `<style>`, `<object>`, `<form>`, etc.) → stripped.
- An event handler (`onclick`, `onerror`, `onload`, `onmouseover`, …) → stripped.
- A `javascript:` or `data:` URL on `href`/`src` → URL stripped (element kept, attribute removed).
- An iframe whose `src` is not on the YouTube / Vimeo / Loom allowlist → entire iframe element stripped.
- A `class` attribute on a tag that isn't in the structural list → `class` stripped.

When in doubt, paste the candidate HTML into the `/admin/news` preview pane — the sandboxed iframe shows exactly what users will see.
