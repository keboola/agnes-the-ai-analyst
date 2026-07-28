"""Contract tests for the baked nodejs-dashboard scaffold.

The scaffold under app/initial_workspace_default/scaffolds/nodejs-dashboard/
is `cp -R`'d by the agnes-data-apps-extras skill into a managed data-app repo.
It must satisfy the upstream data-app-python-js runtime contract
(keboola-config/, nginx :8888 fronting the app on :3000, POST / health,
uv/supervisord) and stay dependency-free of any @keboola/design package.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "initial_workspace_default" / "scaffolds" / "nodejs-dashboard"


def test_scaffold_has_runtime_contract():
    assert (ROOT / "keboola-config").is_dir()
    assert (ROOT / "keboola-config" / "nginx" / "sites" / "default.conf").is_file()
    assert (ROOT / "keboola-config" / "supervisord" / "services" / "app.conf").is_file()
    assert (ROOT / "keboola-config" / "setup.sh").is_file()
    assert (ROOT / "server" / "index.ts").is_file()
    assert (ROOT / "server" / "agnesQuery.ts").is_file()
    assert (ROOT / "supervisord.conf").is_file()
    assert (ROOT / "src" / "App.tsx").is_file()
    assert (ROOT / "src" / "main.tsx").is_file()
    assert (ROOT / "vite.config.ts").is_file()
    assert (ROOT / "tailwind.config.js").is_file()
    assert (ROOT / "CLAUDE.md.tmpl").is_file()

    pkg = (ROOT / "package.json").read_text()
    assert '"vite"' in pkg and '"tailwindcss"' in pkg

    # no CDN <script> smuggling in index.html
    assert "cdn." not in (ROOT / "index.html").read_text()


def test_agnesquery_uses_env_token():
    src = (ROOT / "server" / "agnesQuery.ts").read_text()
    assert "AGNES_TOKEN" in src
    assert "AGNES_URL" in src
    assert "/api/query" in src


def test_no_keboola_design_dependency():
    pkg = (ROOT / "package.json").read_text()
    assert "@keboola/design" not in pkg


def test_nginx_routes_8888_to_3000():
    conf = (ROOT / "keboola-config" / "nginx" / "sites" / "default.conf").read_text()
    assert "8888" in conf
    assert "3000" in conf


def test_claude_md_tmpl_has_app_context_skeleton():
    tmpl = (ROOT / "CLAUDE.md.tmpl").read_text()
    assert "# App context (maintained by Agnes)" in tmpl


def test_server_index_has_health_check():
    src = (ROOT / "server" / "index.ts").read_text()
    assert "app.post" in src or "app.post(" in src
    assert "'/'" in src or '"/"' in src


def test_server_index_is_runtime_valid():
    """Guard the two runtime bugs an npm/type check wouldn't catch: ESM (`"type":
    "module"`) needs explicit `.js` import extensions, and the built SPA lives at
    the project-root `dist/` (two levels up from `server/dist/index.js`)."""
    src = (ROOT / "server" / "index.ts").read_text()
    # ESM import must carry the .js extension (Node refuses extensionless in ESM).
    assert 'from "./agnesQuery.js"' in src, "ESM import needs the .js extension"
    assert 'from "./agnesQuery"' not in src.replace('from "./agnesQuery.js"', "")
    # Static dir must point at the root-level Vite dist, not server/dist.
    assert '"..", "..", "dist"' in src, "distDir must resolve to the project-root dist/"
