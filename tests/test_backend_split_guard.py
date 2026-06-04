"""Backend-split guard — a ratchet that pins the DuckDB→Postgres migration debt.

Two bug classes broke the Postgres app-state backend after the module-by-module
migration (see #499/#513/#518/#522 and the repo method-parity guard in
``tests/db_pg/test_repo_method_parity.py``):

* **#513** — constructing a backend-aware repo directly (``XRepository(conn)``)
  instead of going through the ``src.repositories`` factory. On a PG instance
  the factory returns the ``*Pg`` repo; a direct ``XRepository(conn)`` hits the
  always-DuckDB connection instead.
* **#518** — calling ``get_system_db()`` (always DuckDB) in a request/handler
  path and reading/writing system state off it, bypassing the factory.

You cannot *prove* by inspection that every such site was found — sampling
always misses some (a 125-agent audit did). The only mechanical guarantee is a
ratchet: this test enumerates every current site, pins them in an allow-list,
and FAILS when a NEW one appears. Two further invariants keep it honest:

* the allow-list may not contain a *stale* entry — once a site is migrated to
  the factory, its entry must be removed (the residual list always reflects
  reality), so the allow-list shrinking to legit-only is the definition of
  "migration finished";
* a planted violation must be detected (the detector actually works).

NOTE on legitimacy: a grandfathered entry is NOT automatically a bug. Some are
genuinely DuckDB-only by design (analytics rebuild in ``src/orchestrator.py`` /
``src/profiler.py`` / ``src/catalog_export.py``; cloud-chat PG-only persistence;
DuckDB-only CLI maintenance commands). The migration is "done" when every
remaining entry is one of those — verified by deleting entries as their callers
are routed through the factory (or confirmed DuckDB-only) until only the
permanent ones remain. Today the lists are the full residual.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "cli", "services", "src")

# Repository infra that legitimately owns the raw connection / getter.
_INFRA_EXCLUDE = ("/repositories/", "src/db.py", "src/db_pg.py")


# ---------------------------------------------------------------------------
# detectors (parametrised so the meta-tests can run them on a synthetic file)
# ---------------------------------------------------------------------------

def _rel(p: Path) -> str:
    p = p.resolve()
    try:
        return p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # Path outside the repo (e.g. a tmp file in the meta-tests) — key by its
        # absolute path; it will never match a residual allow-list entry.
        return p.as_posix()


def backend_aware_repo_classes() -> set[str]:
    """Every repository class that has a DuckDB+PG pair (so a direct
    constructor bypasses the backend switch)."""
    repo_dir = REPO_ROOT / "src" / "repositories"
    pg_stems = {p.stem[:-3] for p in repo_dir.glob("*_pg.py")}
    classes: set[str] = set()
    for stem in pg_stems:
        for fname in (f"{stem}.py", f"{stem}_pg.py"):
            f = repo_dir / fname
            if not f.exists():
                continue
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.ClassDef) and node.name.endswith("Repository"):
                    classes.add(node.name)
    return classes


def scan_direct_instantiations(files, classes) -> dict[str, set[str]]:
    """``{relpath: {RepoClassName, ...}}`` for direct ``XRepository(...)`` calls."""
    found: dict[str, set[str]] = {}
    for p in files:
        p = Path(p)
        try:
            tree = ast.parse(p.read_text())
        except (SyntaxError, OSError):
            continue
        hits = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in classes
        }
        if hits:
            found[_rel(p)] = hits
    return found


def _production_files() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            rp = p.as_posix()
            if "/repositories/" in rp or "/tests/" in rp:
                continue
            out.append(p)
    return out


def _get_system_db_caller_files() -> set[str]:
    result = subprocess.run(
        ["grep", "-rlE", "--include=*.py", r"get_system_db\(\)", *SCAN_DIRS],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    files = set()
    for rp in result.stdout.splitlines():
        if "/tests/" in rp or any(x in rp for x in _INFRA_EXCLUDE):
            continue
        files.add(rp)
    return files


# ---------------------------------------------------------------------------
# the residual allow-lists (the live migration debt — shrink, never grow)
# ---------------------------------------------------------------------------

_GRANDFATHERED_DIRECT_INSTANTIATION: dict[str, set[str]] = {
    "app/api/admin_chat.py": {"AuditRepository"},
    "app/api/admin_mcp.py": {"AuditRepository"},
    "app/api/cli_auth.py": {"AccessTokenRepository", "AuditRepository"},
    # cowork_bundle.py — fully migrated to the factory (setup_tokens_repo /
    # users_repo / access_token_repo / audit_repo); entry removed.
    "app/api/data_packages.py": {"AuditRepository", "TableRegistryRepository"},
    "app/api/mcp/tools_generator.py": {"MCPSourceRepository", "ToolRegistryRepository"},
    "app/api/mcp_per_table.py": {"TableRegistryRepository"},
    # mcp_user_secrets.py — migrated to mcp_sources_repo()/per_user_secrets_repo(); entry removed.
    "app/api/memory.py": {"AuditRepository", "KnowledgeRepository"},
    "app/api/memory_domain_suggestions.py": {"AuditRepository"},
    "app/api/memory_domains.py": {"AuditRepository", "KnowledgeRepository"},
    "app/api/recipes.py": {"AuditRepository"},
    # stack.py — _emit_event migrated to usage_repo(); entry removed.
    "app/api/stack_views.py": {"KnowledgeRepository", "UsageRepository"},
    "app/auth/access.py": {"ResourceGrantsRepository", "UserGroupMembersRepository", "UserGroupsRepository", "UserRepository"},
    # Sanctioned `if not use_pg(): XRepository(conn) else: x_repo()` escape hatch
    # (test-isolation / DuckDB-mode), same pattern as app/auth/access.py — NOT a
    # raw backend-split. On Postgres these route through the factory.
    "app/services/stack_resolver.py": {"DataPackagesRepository", "MemoryDomainsRepository", "ResourceGrantsRepository", "UserGroupMembersRepository", "UserGroupsRepository", "UserStackSubscriptionsRepository"},
    # cloud-chat PG-only persistence (legit by design, see module docstring):
    # ChatRepository.__init__ instantiates the *Pg repos only under `use_pg()`
    # and dispatches every method through them — not a backend-split.
    "app/chat/persistence.py": {"ChatMessagePgRepository", "ChatSessionPgRepository", "UserWorkdirPgRepository", "ChatSessionParticipantPgRepository"},
    # main.py lifespan seed-admin: group membership now routes through
    # user_group_members_repo() (factory); only the UserRepository read at the
    # cowork-bundle path remains a direct DuckDB instantiation.
    "app/main.py": {"UserRepository"},
    # Sanctioned `if conn is not None and not use_pg(): UserRepository(conn) else:
    # users_repo()` escape hatch (same pattern as app/auth/access.py). On Postgres
    # the read routes through the factory.
    "src/grant_intersection.py": {"UserRepository"},
    # app/web/router.py — migrated to table_registry_repo()/sync_state_repo()
    # (catalog detail pages); entry removed as the residual shrank.
    "cli/commands/admin_data_semantics.py": {"BqMetadataCacheRepository", "ColumnMetadataRepository", "DataPackagesRepository", "MetricRepository", "TableRegistryRepository"},
    # Slack bot: the whole subsystem reads off `repo._conn` (= the DuckDB system
    # connection that app.state.chat_repo is built on), consistently — user
    # lookup, verification codes, and bindings all live there. Migrating Slack
    # identity to the factory is a separate subsystem-wide effort; until then
    # both handlers stay grandfathered as a coherent DuckDB-conn unit.
    "services/slack_bot/commands.py": {"UserRepository"},
    "services/slack_bot/events.py": {"UserRepository"},
    "src/catalog_export.py": {"TableRegistryRepository"},
    "src/claude_md.py": {"ClaudeMdTemplateRepository"},
    # src/orchestrator.py — rebuild/sync_state/view_ownership migrated to the
    # factory; entry removed.
    # src/profiler.py — get_table_map migrated to metric_repo(); entry removed.
    "src/store_guardrails/purge.py": {"StoreEntitiesRepository", "StoreSubmissionsRepository"},
    "src/store_guardrails/reaper.py": {"AuditRepository"},
    "src/store_guardrails/runner.py": {"AuditRepository", "StoreEntitiesRepository", "StoreSubmissionsRepository"},
    "src/welcome_template.py": {"WelcomeTemplateRepository"},
}

_GRANDFATHERED_GET_SYSTEM_DB: set[str] = {
    "app/api/admin.py",
    # app/api/bq_metadata_refresh.py — vestigial conn params removed; the file
    # is fully factory-routed (bq_metadata_cache_repo / table_registry_repo).
    "app/api/cache_warmup.py",
    "app/api/health.py",
    "app/api/mcp_http.py",
    # app/api/query.py — the bq-metadata VIEW-hint lookup (_view_targets_in)
    # now routes through the factory; no remaining get_system_db caller.
    "app/api/scripts.py",
    "app/api/sync.py",
    "app/api/upload.py",
    # app/api/v2_sample.py — internal-table sampling now routes through
    # connectors.internal.access.sample_internal_rows (use_pg() dispatch).
    "app/auth/access.py",
    "app/auth/dependencies.py",
    # app/auth/pat_resolver.py — co-session resolution now routes through
    # chat_session_participants_repo()/chat_session_repo() (factory); entry
    # removed as the residual shrank.
    "app/auth/providers/google.py",
    "app/auth/providers/password.py",
    "app/auth/router.py",
    "app/main.py",
    "app/marketplace_server/git_router.py",
    "app/web/router.py",
    "cli/commands/admin.py",
    "cli/commands/admin_data_semantics.py",
    "cli/commands/admin_metrics.py",
    "services/verification_detector/__main__.py",
    "src/catalog_export.py",
    "src/rbac.py",
}


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------

def test_no_new_direct_backend_aware_repo_instantiation():
    """No NEW ``XRepository(conn)`` for a factory-backed repo. New backend-aware
    state access must go through ``src.repositories`` factory functions."""
    classes = backend_aware_repo_classes()
    found = scan_direct_instantiations(_production_files(), classes)
    new = {
        f: sorted(hits - _GRANDFATHERED_DIRECT_INSTANTIATION.get(f, set()))
        for f, hits in found.items()
        if hits - _GRANDFATHERED_DIRECT_INSTANTIATION.get(f, set())
    }
    assert not new, (
        "New direct backend-aware repository instantiation(s) detected — route "
        "these through the src.repositories factory (e.g. users_repo()), they "
        "bypass the DuckDB/Postgres backend switch:\n"
        + "\n".join(f"  {f}: {cs}" for f, cs in sorted(new.items()))
    )


def test_direct_instantiation_allowlist_has_no_stale_entries():
    """Every grandfathered (file, class) must still exist. When a site is
    migrated to the factory its entry must be deleted — that is how the residual
    shrinks and how 'migration finished' becomes mechanically verifiable."""
    classes = backend_aware_repo_classes()
    found = scan_direct_instantiations(_production_files(), classes)
    stale = {
        f: sorted(allowed - found.get(f, set()))
        for f, allowed in _GRANDFATHERED_DIRECT_INSTANTIATION.items()
        if allowed - found.get(f, set())
    }
    assert not stale, (
        "Stale allow-list entries — these were migrated/removed; delete them "
        "from _GRANDFATHERED_DIRECT_INSTANTIATION so the residual stays honest:\n"
        + "\n".join(f"  {f}: {cs}" for f, cs in sorted(stale.items()))
    )


def test_no_new_get_system_db_callers():
    """No NEW ``get_system_db()`` caller outside repo/db infra. Handlers must
    read system state through the backend-aware factory, not the always-DuckDB
    system-db getter."""
    callers = _get_system_db_caller_files()
    new = sorted(callers - _GRANDFATHERED_GET_SYSTEM_DB)
    assert not new, (
        "New get_system_db() caller(s) — on a Postgres instance this reads/writes "
        "the wrong backend. Use the src.repositories factory instead:\n"
        + "\n".join(f"  {f}" for f in new)
    )


def test_get_system_db_allowlist_has_no_stale_entries():
    callers = _get_system_db_caller_files()
    stale = sorted(_GRANDFATHERED_GET_SYSTEM_DB - callers)
    assert not stale, (
        "Stale get_system_db allow-list entries — delete them to keep the "
        "residual honest:\n" + "\n".join(f"  {f}" for f in stale)
    )


# ---------------------------------------------------------------------------
# meta-tests: prove the detector actually catches violations
# ---------------------------------------------------------------------------

def test_detector_flags_a_planted_violation(tmp_path):
    """A synthetic module that constructs a backend-aware repo directly must be
    flagged — guards against the detector silently matching nothing."""
    classes = backend_aware_repo_classes()
    assert "UserRepository" in classes, "expected a known backend-aware repo class"
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from src.repositories.users import UserRepository\n"
        "def handler(conn):\n"
        "    return UserRepository(conn).get_by_id('x')\n"
    )
    found = scan_direct_instantiations([planted], classes)
    assert any("UserRepository" in hits for hits in found.values()), (
        "detector failed to flag a planted direct instantiation"
    )


def test_detector_ignores_factory_call(tmp_path):
    """The factory function form (``users_repo()``) must NOT be flagged."""
    classes = backend_aware_repo_classes()
    clean = tmp_path / "clean.py"
    clean.write_text(
        "from src.repositories import users_repo\n"
        "def handler():\n"
        "    return users_repo().get_by_id('x')\n"
    )
    found = scan_direct_instantiations([clean], classes)
    assert not found, f"factory call wrongly flagged: {found}"
