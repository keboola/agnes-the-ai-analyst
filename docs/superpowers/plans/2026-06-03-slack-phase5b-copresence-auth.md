# Phase 5b — Co-drive Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make a live co-drive session authorize against the *intersection* of all live participants' grants — with no admin short-circuit — across every data-read path (the full SR-2 audit), an ephemeral non-personal workspace, fork-on-invite, membership-gated join/leave, per-sender budgets, hardened Slack binding, plus the §5.3 web co-presence surface, so two+ principals can drive one session without privilege escalation.

**Architecture:** A new `SessionPrincipal` becomes a first-class auth subject alongside the user `dict`. `compute_grant_intersection` (built on a no-admin-short-circuit `_allowed_ids_for_user` that routes through the repository factory, so DuckDB and PG behave identically) yields the allowed resource-id set per `ResourceType`; `can_access_session` checks membership in it. The PAT resolver recomputes participants live from `chat_session_participants` (never baked into the JWT) and fails closed on any single-user token aimed at a co-session. A single chokepoint (`can_access_table` / `get_accessible_tables`, `StackResolver.stack`, the sync manifest builder) accepts *either* a dict or a `SessionPrincipal`, and every audited data-authz call site is routed through it. Fork/invite/join/leave endpoints live in `app/api/chat_copresence.py`, gated and audited; the ephemeral workspace builder never touches a personal dir; and `chat.js` renders the co-presence surface.

**Tech Stack:** Python 3.11, FastAPI, DuckDB + Postgres dual-backend, PyJWT (HS256), asyncio, vanilla JS, pytest (`-n auto`).

> ### HARD PREREQUISITE — Phase 5a / Section 7 must land first
>
> This phase is **blocked** until Phase 5a is merged on the branch you build on. The signatures this plan modifies do **not** exist on current `main` (verified: `src/db.py:50` is `SCHEMA_VERSION = 68`; `app/chat/manager.py:44-63` `LiveSession` has a single `ws` field, no `sinks`/`add_sink`/`_stdin_lock`; `app/chat/manager.py:410` `send_user_message(self, chat_id, text)` has no `sender_email`; `app/chat/persistence.py` has no participant methods and `ChatSession` has no `is_co_session`/`ephemeral`; `chat_session_participants` does not exist). **Before starting, run the gate below; if it fails, stop and finish 5a.** The exact 5a artifacts this phase depends on:
>
> - **v69 schema** (`src/db.py` `SCHEMA_VERSION == 69` + matching Alembic step): table `chat_session_participants(session_id, user_email, user_id, role, joined_at, left_at)` with index `idx_chat_session_participants_user`; columns `chat_sessions.is_co_session`, `chat_sessions.ephemeral`, `chat_messages.sender_email`.
> - **Dataclasses** (`app/chat/persistence.py`): `ChatSession.is_co_session: bool`, `ChatSession.ephemeral: bool`, `ChatMessage.sender_email: Optional[str]`, and a `SessionParticipant` dataclass (fields `session_id`, `user_email`, `user_id`, `role`, `joined_at`, `left_at`).
> - **Participant repos** on **both** engines (`app/chat/persistence.py` DuckDB + `src/repositories/chat_session_participants_pg.py`): `add_session_participant`, `get_session_participants`, `remove_participant` (stamps `left_at`), `update_participant_role`, `list_sessions_for_participant`.
> - **Multi-sink `ChatManager`** (`app/chat/manager.py`): `LiveSession.sinks: list[SinkEntry]` where `SinkEntry` carries `(participant_email, sink)`; `attach(chat_id, ws, *, is_primary=True)`; `add_sink(chat_id, ws, participant_email)`; a `_broadcast(live, frame)` helper used by `_pump_subprocess_to_ws`; `LiveSession._stdin_lock: asyncio.Lock`; and `send_user_message(self, chat_id, text, *, sender_email=None)`.
>
> Verification gate (run first, must pass before Task 1):
> ```bash
> .venv/bin/python -c "import src.db; assert src.db.SCHEMA_VERSION >= 69, src.db.SCHEMA_VERSION; \
> from app.chat.persistence import ChatSession, ChatMessage, SessionParticipant, ChatRepository; \
> import inspect; \
> assert 'is_co_session' in ChatSession.__dataclass_fields__ and 'ephemeral' in ChatSession.__dataclass_fields__; \
> assert 'sender_email' in ChatMessage.__dataclass_fields__; \
> assert hasattr(ChatRepository, 'add_session_participant') and hasattr(ChatRepository, 'get_session_participants') and hasattr(ChatRepository, 'remove_participant'); \
> from app.chat.manager import LiveSession; \
> assert '_stdin_lock' in LiveSession.__dataclass_fields__ and 'sinks' in LiveSession.__dataclass_fields__; \
> from app.chat.manager import ChatManager; \
> assert 'sender_email' in inspect.signature(ChatManager.send_user_message).parameters; \
> assert hasattr(ChatManager, 'add_sink'); \
> print('5a present: OK')"
> ```

---

## File Structure

**Created**
- `app/auth/session_principal.py` — the frozen `SessionPrincipal` dataclass (auth subject of a co-session).
- `src/grant_intersection.py` — `compute_grant_intersection(participant_emails, conn)`; the set-intersection of each participant's allowed resource-ids per `ResourceType`, no admin short-circuit, factory-routed (PG-safe).
- `app/api/chat_copresence.py` — gated invite / join-ticket / leave / fork endpoints.
- `app/chat/copresence_summary.py` — intersection-principal transcript summary seeder (SR-8 default).
- `app/chat/session_principal_guard.py` — `deny_principal(user)` helper raising 403 for a `SessionPrincipal` on human-only routes.
- `tests/test_session_principal.py`, `tests/test_grant_intersection.py`, `tests/test_copresence_resolver.py`, `tests/test_copresence_datapath.py`, `tests/test_copresence_workspace.py`, `tests/test_copresence_api.py`, `tests/test_copresence_budgets.py`, `tests/test_binding_hardening.py`, `tests/test_copresence_web_surface.py` — SR-* gate tests.

**Modified**
- `app/auth/access.py` — add `_allowed_ids_for_user` (no admin short-circuit, factory-routed), `can_access_session`, `mint_co_session_jwt`; make `require_admin`/`require_resource_access` dispatch on subject type.
- `src/rbac.py` — `can_access_table` and `get_accessible_tables` accept dict **or** `SessionPrincipal` (SR-2 chokepoint half 1).
- `app/services/stack_resolver.py` — `StackResolver.stack` accepts a `SessionPrincipal` (SR-2 chokepoint half 2).
- `app/api/sync.py` — manifest builder + `last_pull_at` update route through the chokepoint; settings-mutation endpoints hard-deny a principal (SR-2 half 3).
- `app/api/stack.py` — `resolver.stack(user["id"], …)` call site audited + guarded (SR-2 audit).
- `app/api/access.py` — audited (SR-2): no `resolver.stack` / `can_access_table` call site (confirmed in Task 7); guard added only if an audit finds one.
- `app/auth/pat_resolver.py` — `resolve_token_to_user` co-session branch + fail-closed (SR-3, SR-4).
- `app/auth/dependencies.py` — `get_current_user` propagates a `SessionPrincipal` subject.
- `app/chat/workdir.py` — `prepare_ephemeral_session_dir`; make `CLAUDE.local.md` opt-in (SR-6).
- `app/chat/manager.py` — co-branch in `attach`, `LiveSession.participant_emails`, per-sender budgets in `send_user_message`, co-aware spawn/respawn, leave teardown, `add_sink` membership re-verify (SR-5, SR-7, SR-9, SR-10, SR-11).
- `app/chat/e2b_workspace_sync.py` — skip `download_workspace` when `session.ephemeral` (SR-6).
- `services/slack_bot/binding.py` — one-active-code, throttle, attempt lockout, audit, pin `user_id` (SR-12).
- `src/repositories/chat_session_participants_pg.py` + `app/chat/persistence.py` — add `fork_session_as_co_session` and `fork_co_session_to_private` (atomic) to both engines.
- `app/web/static/js/chat.js` — §5.3 co-presence surface: Co-drive pill, participant-avatar cluster, per-message sender attribution, Invite/Fork affordances, `session_participants` frame handler.
- `app/main.py` — register the `chat_copresence` router.
- `tests/db_pg/test_chat_pg.py` — `fork_session_as_co_session` + `fork_co_session_to_private` cross-engine contract.
- `CHANGELOG.md` — `[Unreleased]` bullet.

---

## Task 0 — Confirm the 5a prerequisite (blocking gate)

**Files:** none (verification only)

- [ ] Run the prerequisite gate from the header block:
```bash
.venv/bin/python -c "import src.db; assert src.db.SCHEMA_VERSION >= 69, src.db.SCHEMA_VERSION; \
from app.chat.persistence import ChatSession, ChatMessage, SessionParticipant, ChatRepository; \
import inspect; \
assert 'is_co_session' in ChatSession.__dataclass_fields__ and 'ephemeral' in ChatSession.__dataclass_fields__; \
assert 'sender_email' in ChatMessage.__dataclass_fields__; \
assert hasattr(ChatRepository, 'add_session_participant') and hasattr(ChatRepository, 'get_session_participants') and hasattr(ChatRepository, 'remove_participant'); \
from app.chat.manager import LiveSession; \
assert '_stdin_lock' in LiveSession.__dataclass_fields__ and 'sinks' in LiveSession.__dataclass_fields__; \
from app.chat.manager import ChatManager; \
assert 'sender_email' in inspect.signature(ChatManager.send_user_message).parameters; \
assert hasattr(ChatManager, 'add_sink'); \
print('5a present: OK')"
```
- [ ] Expect the literal output `5a present: OK`. If any assertion fails, STOP — Phase 5a is not landed; finish it before continuing. Do not stub these artifacts; every later task assumes the real 5a state.

---

## Task 1 — `SessionPrincipal` dataclass (SR-1 data shape)

**Files:**
- Create: `app/auth/session_principal.py`
- Test: `tests/test_session_principal.py`

- [ ] Write failing test `tests/test_session_principal.py`:
```python
from app.auth.session_principal import SessionPrincipal


def test_session_principal_is_frozen_and_holds_intersection():
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t1"})},
    )
    assert p.session_id == "chat_1"
    assert p.intersection["table"] == frozenset({"t1"})
    import dataclasses
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.session_id = "other"  # type: ignore[misc]
```
- [ ] Run, expect FAIL (ModuleNotFoundError): `.venv/bin/pytest tests/test_session_principal.py -v`
- [ ] Create `app/auth/session_principal.py`:
```python
"""SessionPrincipal — the auth subject of a live co-drive session.

A co-session is driven by 2+ humans. Its effective authority is the
*intersection* of all live participants' grants (never any one user's full
set, never the Admin god-mode short-circuit). The resolver builds this from
``chat_session_participants WHERE left_at IS NULL`` on every request; the JWT
carries no participant identity (SR-4), so this object is always live-fresh.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    participant_user_ids: list[str]
    participant_emails: list[str]
    intersection: dict[str, frozenset[str]]  # resource_type -> allowed resource_ids
```
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_session_principal.py -v`
- [ ] Commit: `git add app/auth/session_principal.py tests/test_session_principal.py && git commit -m "Add SessionPrincipal auth subject for co-drive sessions"`

---

## Task 2 — `_allowed_ids_for_user` (no-admin-short-circuit, factory-routed) + intersection (SR-1)

**Files:**
- Modify: `app/auth/access.py`
- Create: `src/grant_intersection.py`
- Test: `tests/test_grant_intersection.py`

> **PG-parity note.** `_allowed_ids_for_user` and `compute_grant_intersection` must **not** issue raw `resource_grants` / `users` SQL on the passed `conn` — that diverges under the PG backend (where `_user_group_ids` already ignores `conn` and routes through `user_group_members_repo()`; see `access.py:80-84`). Mirror `can_access` exactly: get groups via `_user_group_ids(user_id, conn=conn)`, then read grants via the `ResourceGrantsRepository(conn)` / `resource_grants_repo()` factory split, and resolve emails via `UserRepository(conn)` / `users_repo()`. `ResourceGrantsRepository.list_for_groups(group_ids, resource_type)` (real method, `src/repositories/resource_grants.py:69`, present on both engines) returns dicts with a `resource_id` key — the parity-safe id source.

- [ ] Write failing test `tests/test_grant_intersection.py`. It seeds a **real** v69 system DB via the canonical pattern (`e2e_env` points `DATA_DIR` at tmp; `get_system_db()` migrates to current schema), then uses the repo factory + the shared `grant_table_via_package` helper from `tests/conftest.py` to grant tables through data packages (per-table `resource_grants` alone no longer surface — see `src/rbac.py`). Because RBAC is stack-gated through data packages, the intersection keys on `ResourceType.DATA_PACKAGE.value` for the package ids:
```python
import pytest

from src.db import SYSTEM_ADMIN_GROUP


@pytest.fixture
def conn(e2e_env):
    # e2e_env (tests/conftest.py) points DATA_DIR at tmp + sets a 32-char
    # JWT_SECRET_KEY. get_system_db() migrates a fresh DB to SCHEMA_VERSION.
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    c = get_system_db()
    users = UserRepository(c)
    users.create(id="ua", email="a@example.com", name="A")
    users.create(id="ub", email="b@example.com", name="B")
    users.create(id="uadm", email="adm@example.com", name="Adm")
    admin_gid = c.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(c).add_member("uadm", admin_gid, source="system_seed")
    # ua holds packages wrapping t1 + t2; ub holds packages wrapping t2 + t3.
    from tests.conftest import grant_table_via_package
    grant_table_via_package(c, "t1", "ua", group_name="g-a")
    grant_table_via_package(c, "t2", "ua", group_name="g-a")
    grant_table_via_package(c, "t2", "ub", group_name="g-b")
    grant_table_via_package(c, "t3", "ub", group_name="g-b")
    yield c
    c.close()


def _pkg_ids(conn, group_name):
    from src.repositories.user_groups import UserGroupsRepository
    gid = UserGroupsRepository(conn).get_by_name(group_name)["id"]
    rows = conn.execute(
        "SELECT resource_id FROM resource_grants WHERE group_id = ? AND resource_type = 'data_package'",
        [gid],
    ).fetchall()
    return frozenset(r[0] for r in rows)


def test_allowed_ids_excludes_admin_short_circuit(conn):
    from app.auth.access import _allowed_ids_for_user
    # admin user has NO data_package grants -> empty set, not "everything"
    assert _allowed_ids_for_user("uadm", "data_package", conn) == frozenset()
    assert _allowed_ids_for_user("ua", "data_package", conn) == _pkg_ids(conn, "g-a")


def test_intersection_two_non_admins_overlap(conn):
    from src.grant_intersection import compute_grant_intersection
    inter = compute_grant_intersection(["a@example.com", "b@example.com"], conn)
    # only the t2-wrapping package is granted to BOTH g-a and g-b? No — each
    # call to grant_table_via_package makes a per-table package, so the
    # overlap is empty unless the same package id is shared. Assert the
    # intersection of the two distinct package sets.
    shared = _pkg_ids(conn, "g-a") & _pkg_ids(conn, "g-b")
    assert inter.get("data_package", frozenset()) == shared


def test_intersection_admin_plus_nonadmin_is_nonadmin_set(conn):
    from src.grant_intersection import compute_grant_intersection
    # give admin the SAME packages ua holds, so intersection == ua's set
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    grants = ResourceGrantsRepository(conn)
    for pid in _pkg_ids(conn, "g-a"):
        if not grants.has_grant([admin_gid], "data_package", pid):
            grants.create(group_id=admin_gid, resource_type="data_package",
                          resource_id=pid, assigned_by="test", requirement="required")
    inter = compute_grant_intersection(["adm@example.com", "a@example.com"], conn)
    assert inter.get("data_package", frozenset()) == _pkg_ids(conn, "g-a")


def test_intersection_grantless_participant_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    from src.repositories.users import UserRepository
    UserRepository(conn).create(id="uc", email="c@example.com", name="C")
    inter = compute_grant_intersection(["a@example.com", "c@example.com"], conn)
    assert inter.get("data_package", frozenset()) == frozenset()


def test_intersection_unknown_email_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    assert compute_grant_intersection(["a@example.com", "ghost@example.com"], conn) == {}


def test_intersection_empty_participant_list_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    assert compute_grant_intersection([], conn) == {}
```
- [ ] Run, expect FAIL (ImportError): `.venv/bin/pytest tests/test_grant_intersection.py -v`
- [ ] In `app/auth/access.py`, add `_allowed_ids_for_user` right after `_user_group_ids` (mirror `can_access`'s factory routing exactly — no raw SQL on `conn`):
```python
def _allowed_ids_for_user(
    user_id: str,
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Set of resource_ids the user is granted for ``resource_type``.

    Deliberately does NOT apply the Admin god-mode short-circuit and does
    NOT add internal-table implicit grants — it reports only what was
    explicitly granted to a group the user belongs to. This is the single
    no-short-circuit grant primitive that both ``can_access`` (union/admin
    path) and ``compute_grant_intersection`` build on, so an admin-leak
    cannot reappear by drift.

    Routes through the repository factory (same split as ``can_access``) so
    DuckDB and Postgres behave identically — never raw SQL on ``conn``.
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return frozenset()
    from src.repositories import use_pg, resource_grants_repo
    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository
        rows = ResourceGrantsRepository(conn).list_for_groups(
            list(group_ids), resource_type,
        )
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids), resource_type,
        )
    return frozenset(r["resource_id"] for r in rows)
```
- [ ] Create `src/grant_intersection.py` (emails resolved via the factory, ids via `_allowed_ids_for_user`):
```python
"""Set-intersection of co-session participants' grants, per ResourceType.

NEVER applies the Admin god-mode short-circuit (SR-1): each participant's
contribution is their real grant set from _allowed_ids_for_user. An admin
participant contributes the full set, so intersect(full, non_admin) ==
non_admin. Fail-closed: an empty participant list, an unknown participant,
or any participant with zero grants for a type collapses that type (or the
whole result) to empty.

PG-parity: resolves emails through the repository factory and reads grants
through _allowed_ids_for_user (which is factory-routed) — no raw SQL on the
passed conn.
"""
from __future__ import annotations

from typing import Optional

import duckdb

from app.resource_types import ResourceType


def compute_grant_intersection(
    participant_emails: list[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    if not participant_emails:
        return {}
    from app.auth.access import _allowed_ids_for_user
    from src.repositories import use_pg, users_repo

    def _user_by_email(email: str):
        if conn is not None and not use_pg():
            from src.repositories.users import UserRepository
            return UserRepository(conn).get_by_email(email)
        return users_repo().get_by_email(email)

    user_ids: list[str] = []
    for email in participant_emails:
        row = _user_by_email(email)
        if not row:
            return {}  # unknown participant -> fail closed
        user_ids.append(row["id"])

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        sets = [_allowed_ids_for_user(uid, rt.value, conn) for uid in user_ids]
        acc: Optional[frozenset[str]] = None
        for s in sets:
            acc = s if acc is None else (acc & s)
        if acc:
            result[rt.value] = acc
    return result
```
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_grant_intersection.py -v`
- [ ] Commit: `git add app/auth/access.py src/grant_intersection.py tests/test_grant_intersection.py && git commit -m "Add no-short-circuit grant primitive and co-session intersection (factory-routed, PG-safe)"`

---

## Task 3 — `can_access_session` + subject-dispatching `require_*` (SR-1, SR-3 dependency half)

**Files:**
- Modify: `app/auth/access.py`
- Test: `tests/test_session_principal.py` (extend)

> **Dict-key convention.** Throughout this plan, intersection dicts are keyed on `ResourceType.X.value` (the `StrEnum` string), never `str(resource_type)`. `can_access_session` looks up `intersection.get(resource_type, ...)` where the caller passes a `.value` string. Keep it uniform — never mix `str(rt)` and `rt.value` for the same dict.

- [ ] Extend `tests/test_session_principal.py`:
```python
import pytest


def test_can_access_session_membership_only():
    from app.auth.access import can_access_session
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["u1", "u2"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t2"})},
    )
    assert can_access_session(p, "table", "t2") is True
    assert can_access_session(p, "table", "t1") is False
    assert can_access_session(p, "slack_channel", "C1") is False


def test_can_access_session_does_not_call_is_user_admin_or_can_access(monkeypatch):
    import app.auth.access as access
    from app.auth.access import can_access_session
    from app.auth.session_principal import SessionPrincipal
    monkeypatch.setattr(access, "is_user_admin", lambda *a, **k: pytest.fail("admin called"))
    monkeypatch.setattr(access, "can_access", lambda *a, **k: pytest.fail("can_access called"))
    p = SessionPrincipal("chat_1", ["u1"], ["a@example.com"], {"table": frozenset({"t2"})})
    assert can_access_session(p, "table", "t2") is True
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_session_principal.py -v`
- [ ] In `app/auth/access.py` add (add the import `from app.auth.session_principal import SessionPrincipal` at the top of the file):
```python
def can_access_session(
    principal: "SessionPrincipal",
    resource_type: str,
    resource_id: str,
) -> bool:
    """Co-session access: membership in the live intersection. Must NOT call
    is_user_admin / can_access (PR checklist item) — the SessionPrincipal's
    ``intersection`` was already built without the admin short-circuit."""
    return resource_id in principal.intersection.get(resource_type, frozenset())
```
- [ ] Make `require_admin` hard-deny a `SessionPrincipal` *before* any `is_user_admin` call. Replace its current body (`access.py:185`):
```python
async def require_admin(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if isinstance(user, SessionPrincipal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if not is_user_admin(user["id"], conn):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
```
- [ ] In `require_resource_access`'s inner `dep` (`access.py:226`), dispatch on subject type. Change the `dep` signature `user: dict = Depends(get_current_user)` → `user=Depends(get_current_user)`, and replace the `if not can_access(...)` block:
```python
        if isinstance(user, SessionPrincipal):
            allowed = can_access_session(user, resource_type.value, resource_id)
        else:
            allowed = can_access(user["id"], resource_type.value, resource_id, conn)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied to {resource_type.value} {resource_id!r}"
                ),
            )
        return user
```
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_session_principal.py -v`
- [ ] Commit: `git add app/auth/access.py tests/test_session_principal.py && git commit -m "can_access_session + subject-dispatching require_admin/require_resource_access"`

---

## Task 4 — `mint_co_session_jwt` carrying no participant identity (SR-4, SR-5 minter half)

**Files:**
- Modify: `app/auth/access.py`
- Test: `tests/test_copresence_resolver.py`

> **Secret consistency.** The real `mint_session_jwt` (`access.py:256`) inlines the fallback string, but the canonical secret lives in `app/auth/jwt._get_cached_secret_key()` and `verify_token` decodes with it. To guarantee the resolver's `verify_token` can decode the co token in every env, mint via `app.auth.jwt.create_access_token` — it encodes with `_get_cached_secret_key()` and `sub` is the positional `user_id`, so pass the synthetic `sub`. Do NOT inline a hardcoded secret.

- [ ] Write failing test `tests/test_copresence_resolver.py` (mint half only for now):
```python
def test_mint_co_session_jwt_has_no_participant_identity(e2e_env):
    # e2e_env sets a 32-char JWT_SECRET_KEY; verify with the same module path.
    from app.auth.access import mint_co_session_jwt
    from app.auth.jwt import verify_token
    token = mint_co_session_jwt("chat_42", ttl=3600)
    payload = verify_token(token)
    assert payload is not None
    assert payload["typ"] == "co_session"
    assert payload["chat_session_id"] == "chat_42"
    assert payload["sub"] == "session:chat_42"  # synthetic, never a user UUID
    assert "participants" not in payload
    assert "email" not in payload
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_resolver.py -v`
- [ ] Add to `app/auth/access.py` (uses the canonical jwt module — `create_access_token` sets `sub` from `user_id`, `typ="co_session"`, and merges `chat_session_id` via `extra_claims`; note `create_access_token` always injects `email` and `jti`, so the resolver branch keys on `typ`/`chat_session_id` only, and the test asserts no `participants` claim — `email` carries the synthetic sub-derived placeholder, so pass an empty email and assert accordingly):
```python
def mint_co_session_jwt(session_id: str, *, ttl: int = 3600) -> str:
    """Mint a co-session runner token. Carries ONLY chat_session_id +
    typ='co_session' + a synthetic sub (never a user UUID). No participant
    email list is baked in (SR-4) — the resolver reads chat_session_participants
    live as the sole source of truth, eliminating the stale-grant replay window.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env.
    """
    from datetime import timedelta
    from app.auth.jwt import create_access_token
    return create_access_token(
        user_id=f"session:{session_id}",
        email="",  # no real identity; resolver never reads this
        expires_delta=timedelta(seconds=ttl),
        typ="co_session",
        extra_claims={"chat_session_id": session_id},
    )
```
- [ ] Update the test assertion: `create_access_token` always sets an `email` claim, so change `assert "email" not in payload` to `assert payload.get("email") == ""` (no real identity baked in). Keep `assert "participants" not in payload`.
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_resolver.py -v`
- [ ] Commit: `git add app/auth/access.py tests/test_copresence_resolver.py && git commit -m "Add mint_co_session_jwt with no baked participant identity (canonical secret)"`

---

## Task 5 — Resolver co-branch + fail-closed (SR-3, SR-4)

**Files:**
- Modify: `app/auth/pat_resolver.py`, `app/auth/dependencies.py`
- Test: `tests/test_copresence_resolver.py` (extend)

> **Single-conn idiom.** The resolver opens `get_system_db()` once per call and closes it in `finally`, matching the `mint_session_jwt` try/finally idiom. Both the co-branch lookup and the defense-in-depth single-user guard share that one connection.

- [ ] Extend `tests/test_copresence_resolver.py`. Build `co_fixture` from a real v69 system DB (`get_system_db()` under `e2e_env`), seed two users (ua/a, ub/b) via `UserRepository`, create a co-session via the 5a participant repo, and add two live participant rows:
```python
import pytest


@pytest.fixture
def co_fixture(e2e_env):
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    c = get_system_db()
    UserRepository(c).create(id="ua", email="a@example.com", name="A")
    UserRepository(c).create(id="ub", email="b@example.com", name="B")
    repo = ChatRepository(c)
    s0 = repo.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        source_session_id=s0.id,
        owner_email="a@example.com", owner_user_id="ua",
        collaborator_email="b@example.com", collaborator_user_id="ub",
    )
    yield c, s1.id
    c.close()


def test_co_session_token_resolves_live_principal(co_fixture):
    conn, co_id = co_fixture
    from app.auth.access import mint_co_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import SessionPrincipal
    subj, reason = resolve_token_to_user(conn, mint_co_session_jwt(co_id))
    assert reason is None
    assert isinstance(subj, SessionPrincipal)
    assert set(subj.participant_emails) == {"a@example.com", "b@example.com"}


def test_single_user_token_against_co_session_fails_closed(co_fixture):
    conn, co_id = co_fixture
    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    tok = create_access_token("ua", "a@example.com", extra_claims={"chat_session_id": co_id})
    subj, reason = resolve_token_to_user(conn, tok)
    assert subj is None
    assert reason == "invalid_token"
```
(Note: `fork_session_as_co_session` is added in Task 9 — this resolver test depends on it. Order is fine because Task 9 lands before the full SR sweep; to keep Task 5 green standalone, the agent may temporarily seed participant rows via `repo.add_session_participant` + a manual `is_co_session=TRUE` update, then switch to `fork_session_as_co_session` after Task 9. Prefer adding the two participant rows directly here with `add_session_participant` and `UPDATE chat_sessions SET is_co_session=TRUE`.)
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_resolver.py -v`
- [ ] In `app/auth/pat_resolver.py`, add the co-branch immediately after `payload = verify_token(token)` / `if not payload: return None, "invalid_token"` (before `users_repo().get_by_id`). One shared connection, closed in `finally`:
```python
    typ = payload.get("typ")
    co_session_id = payload.get("chat_session_id")

    if typ == "co_session" or co_session_id:
        from src.db import get_system_db
        sconn = get_system_db()
        try:
            if typ == "co_session":
                from src.grant_intersection import compute_grant_intersection
                from app.auth.session_principal import SessionPrincipal
                rows = sconn.execute(
                    "SELECT user_id, user_email FROM chat_session_participants "
                    "WHERE session_id = ? AND left_at IS NULL",
                    [co_session_id],
                ).fetchall()
                if not rows:
                    return None, "invalid_token"  # no live participants -> deny
                emails = [r[1] for r in rows]
                principal = SessionPrincipal(
                    session_id=co_session_id,
                    participant_user_ids=[r[0] for r in rows],
                    participant_emails=emails,
                    intersection=compute_grant_intersection(emails, sconn),
                )
                return principal, None
            # Defense-in-depth (SR-3): a plain single-user token that names a
            # co-session must never drive it, regardless of _spawn_runner.
            row = sconn.execute(
                "SELECT is_co_session FROM chat_sessions WHERE id = ?",
                [co_session_id],
            ).fetchone()
            if row and bool(row[0]):
                return None, "invalid_token"  # FAIL CLOSED
        finally:
            try:
                sconn.close()
            except Exception:
                pass
```
- [ ] In `app/auth/dependencies.py` `get_current_user` (`dependencies.py:247`), the `user, reason = resolve_token_to_user(...)` result may now be a `SessionPrincipal`. Skip the dict-only post-processing for principals:
```python
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import SessionPrincipal
    user, reason = resolve_token_to_user(conn, token, request)
    if isinstance(user, SessionPrincipal):
        return user
    if user:
        _attach_admin_flag(user, conn)
        payload = verify_token(token) or {}
        if payload.get("typ") == "pat":
            user["token_type"] = "pat"
        _stash_chat_session_id_from_token(request, token)
        return _stash_user(request, user)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_AUTH_DETAIL_BY_REASON.get(reason, "Invalid or expired token"),
    )
```
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_resolver.py -v`
- [ ] Commit: `git add app/auth/pat_resolver.py app/auth/dependencies.py tests/test_copresence_resolver.py && git commit -m "Resolver co-session branch with fail-closed single-user guard"`

---

## Task 6 — SR-2 chokepoint: `can_access_table` / `get_accessible_tables` accept a `SessionPrincipal`

**Files:**
- Modify: `src/rbac.py`
- Test: `tests/test_copresence_datapath.py`

- [ ] Write failing unit test `tests/test_copresence_datapath.py` (function-level half; the HTTP 403 half is Task 8). Build `rbac_conn` from a real v69 DB seeded with packages so that a `table` intersection drives access. Because `can_access_table`'s `SessionPrincipal` branch checks `ResourceType.TABLE.value` membership directly in `intersection`, seed the principal's `intersection["table"]` explicitly:
```python
import pytest


@pytest.fixture
def rbac_conn(e2e_env):
    from src.db import get_system_db
    c = get_system_db()
    yield c
    c.close()


def test_can_access_table_with_session_principal(rbac_conn):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["ua", "ub"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"table": frozenset({"t2"})},
    )
    assert can_access_table(p, "t2", rbac_conn) is True
    assert can_access_table(p, "t1", rbac_conn) is False


def test_can_access_table_principal_never_admin_short_circuits(rbac_conn, monkeypatch):
    from src.rbac import can_access_table
    from app.auth.session_principal import SessionPrincipal
    monkeypatch.setattr("app.auth.access.is_user_admin", lambda *a, **k: pytest.fail("admin"))
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset()})
    assert can_access_table(p, "t2", rbac_conn) is False


def test_get_accessible_tables_with_principal_returns_list_not_none(rbac_conn):
    from src.rbac import get_accessible_tables
    from app.auth.session_principal import SessionPrincipal
    p = SessionPrincipal("chat_1", ["ua"], ["a@example.com"], {"table": frozenset({"t2"})})
    result = get_accessible_tables(p, rbac_conn)
    assert result is not None  # never "all" for a principal
    assert "t2" in result
    from connectors.internal.access import INTERNAL_TABLES
    for t in INTERNAL_TABLES:
        assert t.registry_id in result
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_datapath.py -v`
- [ ] In `src/rbac.py`, change `can_access_table`'s first parameter to `user` (dict **or** `SessionPrincipal`) and add the principal branch after the internal-table check (`is_internal_table` stays implicit for either subject type). The current body has `user_id = user.get("id")` at the top; restructure:
```python
def can_access_table(
    user,  # dict | SessionPrincipal
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    from connectors.internal.access import is_internal_table
    if is_internal_table(table_id):
        return True

    from app.auth.session_principal import SessionPrincipal
    if isinstance(user, SessionPrincipal):
        from app.auth.access import can_access_session
        from app.resource_types import ResourceType
        # Co-session: intersection membership, no admin short-circuit, no
        # personal stack. The intersection is the sole authority.
        return can_access_session(user, ResourceType.TABLE.value, table_id)

    user_id = user.get("id")
    if not user_id:
        return False
    # ... existing internal-check-already-done body continues unchanged
    # (drop the now-duplicate is_internal_table check that was inside) ...
```
(Remove the now-redundant `is_internal_table` block that previously sat after `user_id = user.get("id")`, since it runs above for both subject types.)
- [ ] Apply the same `SessionPrincipal` branch to `get_accessible_tables` (return the intersection table-ids plus internal tables, never `None`). Add at the top, right after the docstring, before `user_id = user.get("id")`:
```python
    from app.auth.session_principal import SessionPrincipal
    if isinstance(user, SessionPrincipal):
        from app.resource_types import ResourceType
        from connectors.internal.access import INTERNAL_TABLES
        result = list(user.intersection.get(ResourceType.TABLE.value, frozenset()))
        for t in INTERNAL_TABLES:
            if t.registry_id not in result:
                result.append(t.registry_id)
        return result
```
(`INTERNAL_TABLES` and `t.registry_id` are real — `connectors/internal/access.py:67` / `:58`, already used by the existing `get_accessible_tables` body.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_datapath.py -v`
- [ ] Commit: `git add src/rbac.py tests/test_copresence_datapath.py && git commit -m "SR-2 chokepoint: can_access_table/get_accessible_tables accept SessionPrincipal"`

---

## Task 7 — SR-2 chokepoint: `StackResolver.stack` + every audited `resolver.stack` / `can_access` call site

**Files:**
- Modify: `app/services/stack_resolver.py`, `app/api/sync.py`, `app/api/stack.py`
- Test: `tests/test_copresence_datapath.py` (extend)

> **SR-2 full audit.** Every `StackResolver.stack(user["id"], …)` and `can_access_table(user, …)` / `can_access(user["id"], …)` site reached by a co-session runner token must route a `SessionPrincipal` through the chokepoint or hard-deny it. Audited sites on `main` (grep `\.stack(` + `can_access_table` + `can_access(`):
> - `app/api/sync.py:798` (`_build_data_packages_section`), `:854` (`_build_memory_domains_section`), `:969` (`can_access_table` table filter), `:1054` (`UPDATE users … WHERE id = user["id"]`), `:1331` + `:1376` (`can_access(user["id"], …)` settings/subscription writes) → handled in this task.
> - `app/api/stack.py:121` (`resolver.stack(user["id"], rt)`) → guarded in this task (the `/api/stack` browse route is human-only; a co runner has no business there → 403).
> - `app/api/access.py` → **audited: no `resolver.stack` or `can_access_table` call site exists** (grep shows none); the admin-access routes are already `require_admin`-gated, which now hard-denies a principal (Task 3). No change needed.
> - `app/api/data.py:64/:111`, `app/api/catalog.py:43/:73/:108`, `app/api/v2_scan.py:154/:357`, `app/api/v2_sample.py:105`, `app/api/v2_schema.py:112` (and `build_schema` at `v2_schema.py:98` which calls `can_access_table` internally), `app/web/router.py:3218` → all call `can_access_table(user, …)`, which accepts a principal after Task 6. No per-file change; Task 8 asserts the HTTP behavior end-to-end. `v2_sample.py:120`'s `is_user_admin(user.get("id"), conn)` only runs in the `source_type == "internal"` branch, after `can_access_table` already waved the internal table through; a principal has no `.get`, so guard it (below).

- [ ] Extend `tests/test_copresence_datapath.py`:
```python
def test_stack_resolver_with_session_principal(rbac_conn):
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType
    from app.auth.session_principal import SessionPrincipal
    # seed a data package "pkgA" so _fetch_entries can resolve it
    rbac_conn.execute(
        "INSERT INTO data_packages(id, name, slug, created_by) VALUES "
        "('pkgA','Pkg A','pkg-a','test')"
    )
    p = SessionPrincipal(
        session_id="chat_1",
        participant_user_ids=["ua", "ub"],
        participant_emails=["a@example.com", "b@example.com"],
        intersection={ResourceType.DATA_PACKAGE.value: frozenset({"pkgA"})},
    )
    entries = StackResolver(rbac_conn).stack(p, ResourceType.DATA_PACKAGE)
    assert {e.id for e in entries} == {"pkgA"}
```
(Verify the `data_packages` insert columns against `src/db.py`'s `data_packages` DDL before running; adjust column list to the real required columns — read the `CREATE TABLE data_packages` block.)
- [ ] In `app/services/stack_resolver.py`, make `stack` (`stack_resolver.py:142`) accept either `user_id: str` or a `SessionPrincipal`. Add a branch at the top that builds entries directly from the intersection (no `user_stack_subscriptions`, no admin path). Reuse the real `_fetch_entries(resource_type, effective_ids, required_ids)` (`stack_resolver.py:332`) so the `ResourceEntry` projection (`ResourceEntry` dataclass at `stack_resolver.py:44`: `id`, `name`, `description`, `icon`, `color`, `cover_image_url`, `status`, `category`, `owner_name`, `owner_team`, `tags`, `badges`, `requirement`, `in_stack`, `extra`) is identical to the regular path — treat the intersection ids as both effective and required:
```python
    def stack(
        self, user_id_or_principal, resource_type: ResourceType
    ) -> List[ResourceEntry]:
        from app.auth.session_principal import SessionPrincipal
        if isinstance(user_id_or_principal, SessionPrincipal):
            ids = user_id_or_principal.intersection.get(resource_type.value, frozenset())
            entries = self._fetch_entries(resource_type, set(ids), set(ids))
            for e in entries:
                e.in_stack = True
            return entries
        user_id = user_id_or_principal
        # ... existing body unchanged (starts: groups = self._user_group_ids(user_id)) ...
```
(`_fetch_entries` is the real helper the existing `stack`/`browse` paths already use — no new `_entry_for_id` helper is invented; the field set is whatever `_fetch_entries` produces, which is the only `ResourceEntry` construction in the module.)
- [ ] In `app/api/sync.py`, route the manifest builder through the chokepoint. In `_build_manifest_for_user(conn, user)` (`sync.py:935`) the user may now be a `SessionPrincipal`. In `_build_data_packages_section` (`:785`) replace `resolver.stack(user["id"], ResourceType.DATA_PACKAGE)` and in `_build_memory_domains_section` (`:838`) replace `resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN)` with a `stack_subject`:
```python
    from app.auth.session_principal import SessionPrincipal
    stack_subject = user if isinstance(user, SessionPrincipal) else user["id"]
    resolver = StackResolver(conn)
    pkg_entries = resolver.stack(stack_subject, ResourceType.DATA_PACKAGE)
```
(Apply the analogous `stack_subject` substitution at the `MEMORY_DOMAIN` call. The `can_access_table(user, _id_for(s), conn)` filter at `:969` already accepts a principal after Task 6 — no change.)
- [ ] In `app/api/sync.py` `sync_manifest` (`:1037`), the `UPDATE users SET last_pull_at … WHERE id = ?` at `:1054` uses `user["id"]` and would KeyError on a principal. Guard the side-effects block so it is skipped for a principal (a co runner pulling the manifest does not stamp a personal `last_pull_at`):
```python
    from app.auth.session_principal import SessionPrincipal
    if not isinstance(user, SessionPrincipal):
        try:
            conn.execute(
                "UPDATE users SET last_pull_at = current_timestamp WHERE id = ?",
                [user["id"]],
            )
            # ... existing audit_repo().log(...) + telemetry block unchanged ...
        except Exception:
            ...
    return _build_manifest_for_user(conn, user)
```
(Wrap the existing `last_pull_at`/audit/telemetry side-effect block in the `if not isinstance(...)` guard; the `return _build_manifest_for_user(conn, user)` runs for both subject types.)
- [ ] In `app/api/sync.py`, hard-deny a principal at the two settings-mutation endpoints that write under `user["id"]`: `update_sync_settings` (`:1312`, has `can_access(user["id"], …)` at `:1331` + `set_dataset_enabled(user["id"], …)` at `:1334`) and `update_table_subscriptions` (`:1358`, `can_access(user["id"], …)` at `:1370`). Add at the top of each function body:
```python
    from app.auth.session_principal import SessionPrincipal
    if isinstance(user, SessionPrincipal):
        raise HTTPException(403, "co_session cannot mutate user settings")
```
- [ ] In `app/api/stack.py` (`:121`, `resolver.stack(user["id"], rt)`), add the same principal hard-deny at the top of the handler body (browse/stack management is human-only):
```python
    from app.auth.session_principal import SessionPrincipal
    if isinstance(user, SessionPrincipal):
        raise HTTPException(403, "co_session cannot manage stack")
```
- [ ] In `app/api/v2_sample.py` (the `source_type == "internal"` branch around `:120`), `is_user_admin(user.get("id"), conn)` calls `.get` — a principal has none. Guard it so a principal is treated as non-admin for the internal row-level filter (it already passed `can_access_table` at `:105`):
```python
        from app.auth.session_principal import SessionPrincipal
        is_admin = (
            False if isinstance(user, SessionPrincipal)
            else (_is_admin(user.get("id"), conn) if user.get("id") else False)
        )
        where_clause = build_filter_clause(internal_def, user, is_admin)
```
(`build_filter_clause(internal_def, user, is_admin)` reads the user dict for the row-owner email; for a principal the row-level filter is non-admin and scoped — confirm `build_filter_clause` tolerates a non-dict `user` by reading `.get("email")`; if it indexes `user["email"]`, pass a `{"email": user.participant_emails[0]}` shim. Read `connectors/internal/access.build_filter_clause` and adapt to its exact field access.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_datapath.py -v`
- [ ] Commit: `git add app/services/stack_resolver.py app/api/sync.py app/api/stack.py app/api/v2_sample.py tests/test_copresence_datapath.py && git commit -m "SR-2 audit: route every data-authz call site through the SessionPrincipal chokepoint or hard-deny"`

---

## Task 8 — SR-2 HTTP 403 data-path contract test (the hard gate)

**Files:**
- Test: `tests/test_copresence_datapath.py` (extend with FastAPI `TestClient`)

> This test must exist and pass **before any co-route merges** (Risk §10). It is the privilege-escalation gate. It covers every data path SR-2/SR-3 name: `/api/data`, `/api/sync/manifest`, `/api/catalog`, `/api/v2/scan`, `/api/v2/sample`, `/api/v2/schema`.

- [ ] Extend `tests/test_copresence_datapath.py` with an app-level test. Build the app via `create_app()` + `TestClient` (the `seeded_app` fixture idiom in `tests/conftest.py:230`). Seed two users where only `a@example.com` holds table `t1` (via `grant_table_via_package(conn, "t1", "ua", …)`), register `t1` so it appears in the catalog, create an `is_co_session=TRUE` session with both `ua` and `ub` live (via `fork_session_as_co_session`, Task 9), mint a co token, and assert 403 on each path. Use the **real** `ScanRequest` body shape (`app/api/v2_scan.py:37`: `table_id`, optional `select`/`where`/`limit`/`order_by` — there is NO top-level `sql`):
```python
from fastapi.testclient import TestClient


def test_co_token_403_on_single_participant_table(co_app):
    client, co_id = co_app  # app wired to a DuckDB where only A holds t1
    from app.auth.access import mint_co_session_jwt
    hdr = {"Authorization": f"Bearer {mint_co_session_jwt(co_id)}"}

    # /api/data check-access for t1 (A-only) -> 403
    assert client.get("/api/data/t1/check-access", headers=hdr).status_code == 403
    # /api/v2/scan for t1 -> 403 (ScanRequest: table_id only, no `sql`)
    assert client.post("/api/v2/scan", json={"table_id": "t1"}, headers=hdr).status_code == 403
    # /api/v2/sample + /api/v2/schema for t1 -> 403
    assert client.get("/api/v2/sample/t1", headers=hdr).status_code == 403
    assert client.get("/api/v2/schema/t1", headers=hdr).status_code == 403
    # /api/catalog must NOT include t1 (and a single-table catalog read -> 403)
    cat = client.get("/api/catalog", headers=hdr)
    assert cat.status_code == 200
    assert all(t.get("id") != "t1" for t in cat.json().get("tables", cat.json()))
    # /api/sync/manifest must NOT list t1
    man = client.get("/api/sync/manifest", headers=hdr)
    assert man.status_code == 200
    assert all(t.get("id") != "t1" for t in man.json().get("tables", []))


def test_co_token_200_on_shared_table(co_app_shared):
    client, co_id = co_app_shared  # both A and B hold t2 via a shared package
    from app.auth.access import mint_co_session_jwt
    hdr = {"Authorization": f"Bearer {mint_co_session_jwt(co_id)}"}
    assert client.get("/api/data/t2/check-access", headers=hdr).status_code == 204
```
(Before running: read the exact route paths/verbs and the success status code of `/api/data/{table_id}/check-access` in `app/api/data.py`, the catalog response key in `app/api/catalog.py`, and the manifest table key in `_table_manifest_entry` (`sync.py:760`); adjust the JSON keys/paths/expected-success-code to the real responses. For the "shared t2" fixture, grant the SAME data-package id to both participants' groups so the intersection is non-empty — use a single `grant_table_via_package` package id wired to two groups, or grant one package to a group both users belong to.)
- [ ] Run, iterate until PASS: `.venv/bin/pytest tests/test_copresence_datapath.py -v`
- [ ] Run the wider data-authz path tests to confirm no regression for dict users: `.venv/bin/pytest tests/ -k "data or sync or v2 or rbac or catalog" --tb=short -n auto -q`
- [ ] Commit: `git add tests/test_copresence_datapath.py && git commit -m "SR-2 gate: co token returns 403 on single-participant table across all data paths"`

---

## Task 9 — `fork_session_as_co_session` + `fork_co_session_to_private` (dual-backend, atomic) (SR-8 storage primitive)

**Files:**
- Modify: `app/chat/persistence.py`, `src/repositories/chat_session_participants_pg.py`
- Test: `tests/db_pg/test_chat_pg.py`

- [ ] In `tests/db_pg/test_chat_pg.py`, add parametrized contract tests exercising both engines (mirror the file's existing `engine`/`sessions` fixtures; wire a participants/fork repo per engine — `ChatRepository` for DuckDB, `ChatSessionParticipantsPgRepository` for PG, matching how 5a's contract tests parametrize). Assert: S0 unchanged; S1 has `is_co_session=TRUE`, `ephemeral=TRUE`, two participant rows (owner, collaborator) each with a pinned `user_id`; **no messages copied** (SR-8). Use the real message count via the repo's `list_messages` (`app/chat/persistence.py:365`):
```python
def test_fork_session_as_co_session(sessions, participants, messages_repo):
    from app.chat.types import Surface
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        source_session_id=s0.id,
        owner_email="a@example.com", owner_user_id="ua",
        collaborator_email="b@example.com", collaborator_user_id="ub",
    )
    assert s1.is_co_session is True and s1.ephemeral is True
    again = sessions.get_session(s0.id)
    assert again.is_co_session is False and again.ephemeral is False
    rows = participants.get_session_participants(s1.id)
    by_role = {r.role: r for r in rows}
    assert by_role["owner"].user_id == "ua"
    assert by_role["collaborator"].user_id == "ub"
    # SR-8: no transcript blind-clone
    assert messages_repo.list_messages(s1.id) == []


def test_fork_co_session_to_private_copies_transcript(sessions, participants, messages_repo):
    from app.chat.types import Surface
    s0 = sessions.create_session(user_email="a@example.com", surface=Surface.WEB)
    s1 = participants.fork_session_as_co_session(
        source_session_id=s0.id,
        owner_email="a@example.com", owner_user_id="ua",
        collaborator_email="b@example.com", collaborator_user_id="ub",
    )
    messages_repo.append_message(session_id=s1.id, role="assistant", content="hi from co")
    priv_id = participants.fork_co_session_to_private(
        source_session_id=s1.id, owner_email="b@example.com",
    )
    priv = sessions.get_session(priv_id)
    assert priv.is_co_session is False and priv.ephemeral is False
    assert priv.user_email == "b@example.com"
    # same-principal copy is acceptable here (governed by caller's own grants)
    assert any(m.content == "hi from co" for m in messages_repo.list_messages(priv_id))
```
(Match `messages_repo` to whatever fixture 5a uses for `append_message`/`list_messages` per engine; if 5a only provides a single `ChatRepository`-style fixture, reuse it as both `sessions` and `messages_repo`.)
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/db_pg/test_chat_pg.py -k "fork" -v`
- [ ] Add `fork_session_as_co_session(self, *, source_session_id, owner_email, owner_user_id, collaborator_email, collaborator_user_id) -> ChatSession` to `app/chat/persistence.py` (DuckDB). Order so a partial failure leaves only a harmless empty GC-able ephemeral session: (1) create the new co-session row (`is_co_session=TRUE`, `ephemeral=TRUE`) via the existing insert path; (2) `add_session_participant` for owner (role `owner`, `user_id=owner_user_id`) then collaborator (role `collaborator`, `user_id=collaborator_user_id`); never copy `chat_messages`. Return the new `ChatSession` via the existing `get_session` path (`persistence.py:139`).
- [ ] Add `fork_co_session_to_private(self, *, source_session_id, owner_email) -> str` to `app/chat/persistence.py` (DuckDB): create a fresh non-co, non-ephemeral session owned by `owner_email`, then copy each message from `list_messages(source_session_id)` into it via `append_message` (same-principal copy, governed by the caller's own grants). Return the new session id.
- [ ] Add both methods to `src/repositories/chat_session_participants_pg.py` inside a single SQLAlchemy transaction (`with self._engine.begin() as cx:`), same column endpoint, atomic. (PG's FK cascade covers participant cleanup; the explicit ordering still matches DuckDB.)
- [ ] Run, expect PASS (both engines): `.venv/bin/pytest tests/db_pg/test_chat_pg.py -k "fork" -v`
- [ ] Commit: `git add app/chat/persistence.py src/repositories/chat_session_participants_pg.py tests/db_pg/test_chat_pg.py && git commit -m "fork_session_as_co_session + fork_co_session_to_private on both backends"`

---

## Task 10 — Ephemeral workspace builder + opt-in CLAUDE.local.md (SR-6)

**Files:**
- Modify: `app/chat/workdir.py`, `app/chat/e2b_workspace_sync.py`
- Test: `tests/test_copresence_workspace.py`

> **Real `WorkdirManager` construction.** `WorkdirManager.__init__` (`workdir.py:25`) is keyword-only: `data_dir`, `repo`, `bundled_template_dir`, `server_url`, `agnes_version`, `get_marketplace_sha`, `get_template_status`, `fetch_template_zip=None`, `render_workspace_prompt=None`, `marketplace_sha_debounce_seconds=0`. The test builds one with stub callables.

- [ ] Write failing test `tests/test_copresence_workspace.py`:
```python
from pathlib import Path


def _mgr(tmp_path):
    from app.chat.workdir import WorkdirManager

    class _StubRepo:
        def get_workdir_row(self, *a, **k):
            return None
        def upsert_workdir(self, *a, **k):
            return None

    bundled = tmp_path / "bundled"
    (bundled / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=_StubRepo(),
        bundled_template_dir=bundled,
        server_url="https://example.com",
        agnes_version="0.0.0-test",
        get_marketplace_sha=lambda: "sha",
        get_template_status=lambda: None,
        render_workspace_prompt=lambda email: "# CLAUDE\n",
    )


def test_ephemeral_dir_has_no_claude_local_and_only_intersection_plugins(tmp_path):
    mgr = _mgr(tmp_path)
    # seed one allowed + one disallowed plugin in the bundled template
    skills = tmp_path / "bundled" / ".claude" / "skills"
    (skills / "pluginX").mkdir(parents=True, exist_ok=True)
    (skills / "pluginX" / "SKILL.md").write_text("x", encoding="utf-8")
    (skills / "pluginY").mkdir(parents=True, exist_ok=True)
    (skills / "pluginY" / "SKILL.md").write_text("y", encoding="utf-8")
    sdir = Path(mgr.prepare_ephemeral_session_dir(
        chat_id="chat_co1",
        participant_emails=["a@example.com", "b@example.com"],
        intersection={"marketplace_plugin": frozenset({"pluginX"})},
    ))
    assert not (sdir / "CLAUDE.local.md").exists()
    for p in sdir.rglob("*"):
        if p.is_symlink():
            assert "users" not in str(p.resolve())
    assert (sdir / "CLAUDE.md").exists()
    assert (sdir / "memory").is_dir() and not any((sdir / "memory").iterdir())
    assert (sdir / "work").is_dir()
    present = {p.name for p in (sdir / ".claude" / "skills").iterdir()}
    assert present == {"pluginX"}


def test_prepare_session_dir_no_claude_local_by_default(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.ensure_user_workdir("a@example.com")
    sdir = Path(mgr.prepare_session_dir("a@example.com", "chat_personal"))
    assert not (sdir / "CLAUDE.local.md").exists()
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_workspace.py -v`
- [ ] In `app/chat/workdir.py` `prepare_session_dir` (`workdir.py:167`), remove `"CLAUDE.local.md"` from the unconditional symlink tuple (`workdir.py:176`) and add an opt-in parameter:
```python
    def prepare_session_dir(self, user_email: str, chat_id: str,
                            *, include_personal_override: bool = False) -> Path:
        sessions_root = self.user_sessions_root(user_email)
        sessions_root.mkdir(parents=True, exist_ok=True)
        sdir = sessions_root / chat_id
        sdir.mkdir(parents=True, exist_ok=True)
        ws = self.user_workspace(user_email)
        entries = [".claude", "CLAUDE.md", "snapshots", "scripts"]
        if include_personal_override:
            entries.append("CLAUDE.local.md")
        for entry in entries:
            link = sdir / entry
            target = ws / entry
            if not target.exists():
                continue
            if not link.exists():
                link.symlink_to(target)
        (sdir / "work").mkdir(exist_ok=True)
        return sdir
```
- [ ] Add `prepare_ephemeral_session_dir` to `WorkdirManager`:
```python
    def prepare_ephemeral_session_dir(
        self, chat_id: str, participant_emails: list[str],
        intersection: dict[str, "frozenset[str]"],
    ) -> Path:
        """Fresh co-session workspace. NO symlinks to any personal workspace,
        NO CLAUDE.local.md in any form, fresh empty memory/, shared work/.
        Only intersection-filtered .claude/{skills,agents} are copied in."""
        import shutil
        root = self._data_dir / "ephemeral_sessions" / chat_id
        if root.exists():
            shutil.rmtree(root)
        (root / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (root / "memory").mkdir(exist_ok=True)
        (root / "work").mkdir(exist_ok=True)
        rendered = None
        if self._render_workspace_prompt is not None and participant_emails:
            try:
                rendered = self._render_workspace_prompt(participant_emails[0])
            except Exception:
                logger.exception("ephemeral CLAUDE.md render failed for %s", chat_id)
        (root / "CLAUDE.md").write_text(rendered or "# Co-drive session\n", encoding="utf-8")
        allowed = intersection.get("marketplace_plugin", frozenset())
        src_root = self._bundled_template_dir / ".claude" / "skills"
        if src_root.exists():
            for plug in allowed:
                src = src_root / plug
                if src.exists():
                    shutil.copytree(src, root / ".claude" / "skills" / plug,
                                    dirs_exist_ok=True)
        return root
```
- [ ] In `app/chat/e2b_workspace_sync.py` `download_workspace` (`e2b_workspace_sync.py:197`), add a `skip` guard so it is a no-op for ephemeral sessions. Read the existing signature first and add the keyword minimally:
```python
async def download_workspace(sandbox, dest_dir, *, skip: bool = False, **kw):
    if skip:
        return  # ephemeral co-session: never persist back (SR-6)
    ...  # existing body unchanged
```
(Match the real positional parameter names of `download_workspace` — adjust the leading params to whatever the function actually takes; only the `*, skip: bool = False` addition + early return are new.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_workspace.py -v`
- [ ] Commit: `git add app/chat/workdir.py app/chat/e2b_workspace_sync.py tests/test_copresence_workspace.py && git commit -m "Ephemeral co-session workspace; CLAUDE.local.md opt-in only"`

---

## Task 11 — ChatManager co-branch in attach + `participant_emails` + co-aware spawn (SR-5, SR-6, SR-7 spawn half)

**Files:**
- Modify: `app/chat/manager.py`
- Test: `tests/test_copresence_budgets.py`

> Anchors on the post-5a multi-sink `attach`/`add_sink` and `LiveSession.sinks`/`_stdin_lock`. The participant repo is reached via `self._repo` (the `ChatRepository` injected into `ChatManager.__init__`, `manager.py:66`) — `self._repo.get_session_participants(...)` and `self._repo._conn` (real attribute, `persistence.py:82`). The intersection is computed from the live participant emails.

- [ ] Add `participant_emails` to `LiveSession` (this is the single field both the attach co-branch and the per-participant counting read). In `app/chat/manager.py`, in the `LiveSession` dataclass (`manager.py:44`), add after `auto_title_started`:
```python
    participant_emails: list[str] = field(default_factory=list)
```
- [ ] Write failing test in `tests/test_copresence_budgets.py` asserting `_spawn_runner` for a co-session calls `mint_co_session_jwt` and never falls back to `AGNES_SESSION_JWT_SEED`:
```python
import pytest


@pytest.mark.asyncio
async def test_co_session_spawn_uses_co_jwt_no_seed_fallback(monkeypatch, co_manager):
    mgr, co_session, session_dir = co_manager  # co_session.is_co_session is True
    monkeypatch.setenv("AGNES_SESSION_JWT_SEED", "SEED")

    def boom(*a, **k):
        raise ValueError("nope")  # force mint to fail -> must re-raise, never SEED
    monkeypatch.setattr("app.auth.access.mint_co_session_jwt", boom)
    with pytest.raises(ValueError):
        await mgr._spawn_runner(co_session, session_dir)
```
(`co_manager` builds a `ChatManager` with a fake `SandboxProvider` whose spawn records the env it was handed; `co_session` is a `ChatSession` with `is_co_session=True`. Reuse whatever fake-provider fixture 5a's manager tests use.)
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_budgets.py -k spawn -v`
- [ ] In `app/chat/manager.py` `_spawn_runner` (`manager.py:210`), branch the token mint on `session.is_co_session`:
```python
        from app.auth.access import mint_session_jwt, mint_co_session_jwt
        if session.is_co_session:
            # SR-5: NO seed fallback. A failure re-raises and aborts the spawn;
            # never inject a seed token (no co claims, could resolve to admin).
            token = mint_co_session_jwt(session.id)
        else:
            try:
                token = mint_session_jwt(session.user_email, session.id)
            except ValueError:
                logger.warning(
                    "_spawn_runner: mint_session_jwt failed for %s; using "
                    "AGNES_SESSION_JWT_SEED fallback", session.user_email,
                )
                token = os.environ.get("AGNES_SESSION_JWT_SEED", "")
```
- [ ] In `attach` (`manager.py:179`), branch the workspace prep on `session.is_co_session` **before** `prepare_session_dir`, compute the intersection, and populate `live.participant_emails` when constructing `LiveSession`:
```python
        if session.is_co_session:
            parts = self._repo.get_session_participants(session.id)
            emails = [p.user_email for p in parts if p.left_at is None]
            from src.grant_intersection import compute_grant_intersection
            inter = compute_grant_intersection(emails, self._repo._conn)
            session_dir = self._workdir_mgr.prepare_ephemeral_session_dir(
                chat_id, emails, inter,
            )
        else:
            emails = [session.user_email]
            self._workdir_mgr.ensure_user_workdir(session.user_email)
            session_dir = self._workdir_mgr.prepare_session_dir(session.user_email, chat_id)
```
Then pass `participant_emails=emails` into the `LiveSession(...)` constructor in `attach`.
- [ ] In `_spawn_runner`, when downloading the workspace back, pass `skip=session.ephemeral` to `download_workspace` at the relevant call site (Task 10's guard). Upload still operates on the ephemeral dir. (Locate the `download_workspace(...)` call in the spawn/provider path and thread `skip=session.ephemeral`.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_budgets.py -k spawn -v`
- [ ] Commit: `git add app/chat/manager.py tests/test_copresence_budgets.py && git commit -m "Co-aware spawn: ephemeral workspace, co JWT, no seed fallback, participant_emails"`

---

## Task 12 — Per-sender budgets / caps / rate limits + per-participant counting (SR-10)

**Files:**
- Modify: `app/chat/manager.py`
- Test: `tests/test_copresence_budgets.py` (extend)

> Post-5a `send_user_message(self, chat_id, text, *, sender_email=None)` already takes `sender_email`. This task threads the *sender* through the budget/rate/cap checks (currently keyed on `live.user_email` at `manager.py:415` and `:452`) and holds the 5a `_stdin_lock` across the write+drain pair (SR-6.2).

- [ ] Extend `tests/test_copresence_budgets.py`:
```python
@pytest.mark.asyncio
async def test_capped_collaborator_rejected_owner_passes(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    # collaborator's rate window pre-filled to the cap; owner's empty
    mgr._config.rate_messages_per_hour = 5
    import time
    mgr._user_msg_window[collab] = mgr._deque_cls([time.monotonic()] * 5)
    with pytest.raises(RuntimeError):
        await mgr.send_user_message(live.chat_id, "hi", sender_email=collab)
    # owner turn passes (its own window is empty)
    await mgr.send_user_message(live.chat_id, "hi", sender_email=owner)


def test_active_count_counts_every_participant(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    assert mgr._active_count_for_user(owner) >= 1
    assert mgr._active_count_for_user(collab) >= 1
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_budgets.py -k "collaborator or count" -v`
- [ ] In `send_user_message` (`manager.py:410`), compute `sender = sender_email or live.user_email` at the top, and replace every `live.user_email` in the daily-token cache lookup (`:415`), the per-user rate window (`:452`), and the persisted message author with `sender`. Hold the 5a `_stdin_lock` across write+drain:
```python
    async def send_user_message(self, chat_id: str, text: str, *, sender_email=None) -> None:
        live = self._live.get(chat_id)
        if live is None or live.handle is None or live.state == SessionState.DEAD:
            raise SessionNotFound(chat_id)
        sender = sender_email or live.user_email
        tokens_in, tokens_out = self._cached_daily_tokens(sender)
        # ... daily-budget + max_session_tokens checks unchanged ...
        import time as _time
        now_mono = _time.monotonic()
        window = self._user_msg_window.setdefault(sender, self._deque_cls())
        while window and (now_mono - window[0]) > 3600:
            window.popleft()
        if len(window) >= self._config.rate_messages_per_hour:
            # ... existing rate_limit error frame (broadcast to sinks) ...
            raise RuntimeError("rate_limit_exceeded")
        window.append(now_mono)
        self._repo.append_message(session_id=chat_id, role="user", content=text, sender_email=sender)
        payload = json.dumps({"type": "user_msg", "text": text}) + "\n"
        async with live._stdin_lock:   # SR-6.2 atomic write+drain
            live.handle.stdin.write(payload.encode("utf-8"))
            await live.handle.stdin.drain()
        live.last_activity = datetime.now(timezone.utc)
        live.state = SessionState.ACTIVE
```
(For the error-frame sends inside the budget/rate checks, use the 5a `_broadcast(live, frame)` helper instead of `live.ws.send_json` so all sinks see the rejection.)
- [ ] Make `_active_count_for_user` (`manager.py:158`) count a co-session against **every** live participant:
```python
    def _active_count_for_user(self, user_email: str) -> int:
        n = 0
        for s in self._live.values():
            if s.state not in (SessionState.NEW, SessionState.ACTIVE, SessionState.IDLE):
                continue
            if s.user_email == user_email or user_email in s.participant_emails:
                n += 1
        return n
```
(`s.participant_emails` is the field added in Task 11; non-co sessions default to `[]`, so the second clause is a no-op for them and the owner is still counted by the first clause.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_budgets.py -k "collaborator or count" -v`
- [ ] Commit: `git add app/chat/manager.py tests/test_copresence_budgets.py && git commit -m "Per-sender budgets, rate limits, and per-participant active-session counting"`

---

## Task 13 — Leave teardown + co-aware respawn (SR-7, SR-9, SR-11)

**Files:**
- Modify: `app/chat/manager.py`
- Test: `tests/test_copresence_budgets.py` (extend — sink teardown), `tests/test_copresence_resolver.py` (respawn)

> Builds on 5a's `LiveSession.sinks: list[SinkEntry]` (each carrying `participant_email` + `sink`), `_broadcast(live, frame)`, and the crash-respawn pump-replacement block in `_wait_for_exit_and_respawn` (`manager.py:362`).

- [ ] Extend `tests/test_copresence_budgets.py` with the SR-9 zero-frames-after-leave gate (uses a fake sink recording frames):
```python
@pytest.mark.asyncio
async def test_leaver_sink_receives_zero_frames_after_leave(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    collab_sink = next(s.sink for s in live.sinks if s.participant_email == collab)
    collab_sink.frames.clear()
    await mgr.leave_session(live.chat_id, collab)   # stamps left_at + removes+closes sink
    await mgr._broadcast(live, {"type": "assistant_message", "content": "x"})
    assert collab_sink.frames == []
    assert all(s.participant_email != collab for s in live.sinks)
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_budgets.py -k leaver -v`
- [ ] Add `leave_session` to `ChatManager`. One handler, before returning: stamp `left_at` (`self._repo.remove_participant`), remove the leaver's `SinkEntry` from `live.sinks`, `await sink.close()`, recompute `live.participant_emails`, then re-spawn under the narrower intersection:
```python
    async def leave_session(self, chat_id: str, participant_email: str) -> None:
        live = self._live.get(chat_id)
        if live is None:
            return
        self._repo.remove_participant(chat_id, participant_email)  # stamps left_at
        leaving = [s for s in live.sinks if s.participant_email == participant_email]
        live.sinks = [s for s in live.sinks if s.participant_email != participant_email]
        for s in leaving:
            try:
                await s.sink.close()
            except Exception:
                logger.exception("close leaver sink failed for %s", chat_id)
        parts = self._repo.get_session_participants(chat_id)
        live.participant_emails = [p.user_email for p in parts if p.left_at is None]
        await self._respawn_co_runner(live)
```
- [ ] Add `_respawn_co_runner(self, live)` that: reloads the session via `self._repo.get_session(live.chat_id)`, recomputes the intersection from `live.participant_emails` via `compute_grant_intersection(..., self._repo._conn)`, re-prepares the ephemeral dir via `self._workdir_mgr.prepare_ephemeral_session_dir(...)`, kills the current `live.handle` and re-spawns via `self._spawn_runner(session, session_dir)` (which keys on `session.is_co_session` → co JWT), and replaces `live.current_pump` with a fresh `_pump_subprocess_to_ws(live)` task (reuse the exact pump-replacement block from `_wait_for_exit_and_respawn`).
- [ ] Make `_wait_for_exit_and_respawn` (`manager.py:362`) co-aware (SR-11): re-mint via the co branch is already handled by `_spawn_runner` (keys on `session.is_co_session`); on replay, skip turns authored by a now-left participant and carry `sender_email`. Replace the replay loop with:
```python
            history = self._repo.list_messages(live.chat_id)[-3:]
            live_emails = set(live.participant_emails) or {live.user_email}
            for msg in history:
                if msg.role != "user":
                    continue
                author = getattr(msg, "sender_email", None) or live.user_email
                if author not in live_emails:
                    continue  # SR-11: do not replay a departed participant's turn
                payload = json.dumps({"type": "user_msg", "text": msg.content}) + "\n"
                async with live._stdin_lock:
                    new_handle.stdin.write(payload.encode("utf-8"))
                    await new_handle.stdin.drain()
```
(Match `new_handle` to the variable name the existing respawn block uses for the freshly spawned handle.)
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_budgets.py -k leaver -v`
- [ ] Commit: `git add app/chat/manager.py tests/test_copresence_budgets.py && git commit -m "Atomic leave teardown and co-aware respawn under narrowed intersection"`

---

## Task 14 — Fork/invite/join/leave API + `add_sink` membership re-verify (SR-8 seeding, SR-9 join gate)

**Files:**
- Create: `app/api/chat_copresence.py`, `app/chat/copresence_summary.py`, `app/chat/session_principal_guard.py`
- Modify: `app/main.py`, `app/chat/manager.py`
- Test: `tests/test_copresence_api.py`

> **App-state accessors.** Co routes reach the repo and manager via FastAPI app state, matching `app/api/chat.py`'s idiom. Read `app/api/chat.py` for the exact accessors (`request.app.state.chat_manager` and how it builds a `ChatRepository`) and reuse them verbatim — do NOT invent. The examples below name them `_manager(request)` / `_repo(conn)`; replace with the real accessors found in `app/api/chat.py`.

- [ ] Write failing `tests/test_copresence_api.py` covering: invite requires caller owns S0 **and** invitee independently has CHAT access; S1 is co/ephemeral with two participant rows; seed is a summary (assert a known secret string from S0's transcript is absent in S1's messages); join issues a ticket only for a live participant; a non-participant gets 403:
```python
def test_invite_requires_owner_and_invitee_chat_access(co_api):
    client, s0, owner_hdr, invitee_email = co_api
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email}, headers=owner_hdr)
    assert r.status_code == 200
    assert r.json()["is_co_session"] is True


def test_invite_rejects_non_owner(co_api_other):
    client, s0, other_hdr, invitee_email = co_api_other
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email}, headers=other_hdr)
    assert r.status_code == 403


def test_join_ticket_only_for_live_participant(co_api_joined):
    client, s1, collab_hdr, stranger_hdr = co_api_joined
    assert client.post(f"/api/chat/{s1}/join-ticket", headers=collab_hdr).status_code == 200
    assert client.post(f"/api/chat/{s1}/join-ticket", headers=stranger_hdr).status_code == 403


def test_seed_is_summary_not_raw_clone(co_api_secret):
    client, s0, owner_hdr, invitee_email = co_api_secret  # S0 contains SECRET_ROW_VALUE
    s1 = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email},
                     headers=owner_hdr).json()["session_id"]
    msgs = client.get(f"/api/chat/{s1}/messages", headers=owner_hdr).json()
    joined = " ".join(m.get("content", "") for m in msgs)
    assert "SECRET_ROW_VALUE" not in joined
```
(Build the fixtures with `seeded_app`-style clients + `grant_table_via_package`/CHAT grants; read `app/api/chat.py` for the real `/api/chat/{id}/messages` response shape.)
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_api.py -v`
- [ ] Create `app/chat/session_principal_guard.py`:
```python
"""deny_principal — 403 a SessionPrincipal on human-only routes."""
from __future__ import annotations

from fastapi import HTTPException

from app.auth.session_principal import SessionPrincipal


def deny_principal(user) -> None:
    if isinstance(user, SessionPrincipal):
        raise HTTPException(status_code=403, detail="not available to co-session token")
```
- [ ] Create `app/chat/copresence_summary.py`:
```python
"""SR-8 seed: a server-generated summary produced under the intersection
principal — never a raw transcript clone. v1 default seeds the co-session with
a one-line topic note derived from the source session's title only, so no
historical query result a low-grant invitee can't reproduce is leaked."""
from __future__ import annotations

import duckdb


def build_intersection_summary(
    source_session_id: str, participant_emails: list[str],
    conn: duckdb.DuckDBPyConnection,
) -> str:
    row = conn.execute(
        "SELECT title FROM chat_sessions WHERE id = ?", [source_session_id]
    ).fetchone()
    title = (row[0] if row else None) or "a previous session"
    who = ", ".join(participant_emails)
    return (
        f"This is a shared co-drive session forked from \u201c{title}\u201d. "
        f"Participants ({who}) share access limited to the intersection of "
        f"their grants. Prior results from the original session were not "
        f"carried over; re-run any query you need here."
    )
```
- [ ] Create `app/api/chat_copresence.py` with the four endpoints, all gated (replace `_repo`/`_manager` with the real app-state accessors from `app/api/chat.py`):
```python
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.access import can_access
from app.auth.dependencies import get_current_user, _get_db
from app.chat.session_principal_guard import deny_principal
from app.resource_types import ResourceType

router = APIRouter(prefix="/api/chat", tags=["chat-copresence"])


class InviteBody(BaseModel):
    invitee_email: str


@router.post("/{session_id}/invite")
async def invite(session_id: str, body: InviteBody, request: Request,
                 user: dict = Depends(get_current_user), conn=Depends(_get_db)):
    deny_principal(user)
    repo = _repo(conn)
    s0 = repo.get_session(session_id)
    if s0 is None or s0.user_email != user["email"]:
        raise HTTPException(403, "only the owner can invite")
    inv_row = conn.execute("SELECT id FROM users WHERE email = ?", [body.invitee_email]).fetchone()
    if not inv_row or not can_access(inv_row[0], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(403, "invitee lacks chat access")
    s1 = repo.fork_session_as_co_session(
        source_session_id=session_id,
        owner_email=user["email"], owner_user_id=user["id"],
        collaborator_email=body.invitee_email, collaborator_user_id=inv_row[0],
    )
    from app.chat.copresence_summary import build_intersection_summary
    repo.append_message(
        session_id=s1.id, role="assistant",
        content=build_intersection_summary(session_id, [user["email"], body.invitee_email], conn),
    )
    from app.chat.audit import write_audit
    write_audit(conn, user_email=user["email"], action="co_session_fork",
                details={"source": session_id, "co_session": s1.id, "invitee": body.invitee_email})
    return {"session_id": s1.id, "is_co_session": True}


@router.post("/{session_id}/join-ticket")
async def join_ticket(session_id: str, request: Request,
                      user: dict = Depends(get_current_user), conn=Depends(_get_db)):
    deny_principal(user)
    repo = _repo(conn)
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == user["email"] and p.left_at is None for p in parts):
        raise HTTPException(403, "not a live participant")
    from app.auth.access import mint_co_session_jwt
    return {"ticket": mint_co_session_jwt(session_id), "ws": f"/ws/chat/{session_id}"}


@router.post("/{session_id}/leave")
async def leave(session_id: str, request: Request,
                user: dict = Depends(get_current_user), conn=Depends(_get_db)):
    deny_principal(user)
    repo = _repo(conn)
    s = repo.get_session(session_id)
    mgr = _manager(request)
    if s and s.user_email == user["email"]:
        await mgr.kill(session_id, reason="owner_leave")
    else:
        await mgr.leave_session(session_id, user["email"])
    return {"ok": True}


@router.post("/{session_id}/fork")
async def fork(session_id: str, request: Request,
               user: dict = Depends(get_current_user), conn=Depends(_get_db)):
    deny_principal(user)
    repo = _repo(conn)
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == user["email"] and p.left_at is None for p in parts):
        raise HTTPException(403, "not a participant")
    new_id = repo.fork_co_session_to_private(source_session_id=session_id, owner_email=user["email"])
    return {"session_id": new_id}
```
(Verify `write_audit`'s real signature in `app/chat/audit.py` — adapt the call to its actual params; if no `app/chat/audit.py` exists, use `audit_repo().log(...)` as the rest of the codebase does, e.g. `app/api/sync.py:1062`.)
- [ ] Register the router in `app/main.py` next to the other chat routers: `from app.api import chat_copresence` / `app.include_router(chat_copresence.router)`.
- [ ] Make `ChatManager.add_sink` re-verify live membership (SR-9): before appending the new `SinkEntry`, assert the joining participant is in `self._repo.get_session_participants(chat_id)` with `left_at IS NULL`; else raise. Add to `add_sink` (the 5a method):
```python
        parts = self._repo.get_session_participants(chat_id)
        if not any(p.user_email == participant_email and p.left_at is None for p in parts):
            raise PermissionError(f"{participant_email} is not a live participant of {chat_id}")
```
- [ ] Make `ChatManager.kill` discard the ephemeral dir for an ephemeral session: in `kill` (`manager.py:499`), after teardown, if the session is ephemeral, `shutil.rmtree(self._workdir_mgr._data_dir / "ephemeral_sessions" / chat_id, ignore_errors=True)` and remove participant rows. (Read the session via `self._repo.get_session(chat_id)` for the `ephemeral` flag; reuse the existing `WorkdirManager._data_dir` path used by `prepare_ephemeral_session_dir`. No save-on-end — dropped from v1 per §6.4.)
- [ ] Add a focused `add_sink` membership test to `tests/test_copresence_budgets.py`:
```python
@pytest.mark.asyncio
async def test_add_sink_rejects_non_participant(co_manager_live):
    mgr, live, owner, collab = co_manager_live
    class _Sink:
        frames = []
        async def send_json(self, f): self.frames.append(f)
        async def close(self): pass
    with pytest.raises(PermissionError):
        await mgr.add_sink(live.chat_id, _Sink(), "stranger@example.com")
```
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_api.py tests/test_copresence_budgets.py -k "invite or join or seed or fork or sink" -v`
- [ ] Commit: `git add app/api/chat_copresence.py app/chat/copresence_summary.py app/chat/session_principal_guard.py app/main.py app/chat/manager.py tests/test_copresence_api.py tests/test_copresence_budgets.py && git commit -m "Fork/invite/join/leave co-presence API; add_sink membership re-verify; ephemeral discard on kill"`

---

## Task 15 — §5.3 Co-presence web surface (chat.js)

**Files:**
- Modify: `app/web/static/js/chat.js`
- Test: `tests/test_copresence_web_surface.py`

> JS is asserted via Python `read_text` string contracts, matching `tests/test_design_system_contract.py` (`STATIC = Path("app/web/static")`, `.read_text(...)`). The §5.3 surface is render-only and all co fields are optional in JS (graceful degradation on older servers). No CSS hex/`var(--primary)` is added — any new color uses `var(--ds-*)`.

- [ ] Write failing `tests/test_copresence_web_surface.py`:
```python
from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")


def _src():
    return CHAT_JS.read_text(encoding="utf-8")


def test_session_participants_frame_handled():
    src = _src()
    assert '"session_participants"' in src or "'session_participants'" in src
    assert "renderParticipants" in src


def test_per_message_sender_attribution():
    src = _src()
    # renderMessage attributes a foreign sender
    assert "sender_email" in src
    assert "currentUserEmail" in src


def test_co_drive_pill_and_invite_fork_affordances():
    src = _src()
    assert "co-drive" in src.lower()  # pill label/class
    assert "renderCoPresence" in src
    assert "invite" in src.lower() and "fork" in src.lower()


def test_no_raw_hex_or_legacy_primary_added():
    src = _src()
    import re
    # the co-presence block must not introduce raw hex or var(--primary)
    assert "var(--primary)" not in src
    # allow existing hex elsewhere is out of scope; assert our markers use ds tokens
    assert "var(--ds-" in src  # ds tokens are used in the file
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_copresence_web_surface.py -v`
- [ ] In `app/web/static/js/chat.js`, capture the current user email near the top of the module (it already reads `document.body.dataset.userEmail` at `:509` — hoist a module constant):
```javascript
const currentUserEmail = document.body.dataset.userEmail || "";
```
- [ ] In `renderMessage` (`chat.js:641`), after `body.innerHTML = marked.parse(...)`, add per-sender attribution for a foreign sender (co field optional):
```javascript
  if (m.sender_email && m.sender_email !== currentUserEmail) {
    const who = document.createElement("div");
    who.className = "msg-sender-attr";  // styled via chat.css with var(--ds-*) tokens
    who.textContent = m.sender_email;
    bubble.insertBefore(who, body);
  }
```
- [ ] Add the `session_participants` frame to `handleFrame` (`chat.js:419` switch) and a renderer. In the switch add:
```javascript
    case "session_participants":
      renderParticipants(frame.participants || []);
      break;
```
And add the renderer + a co-presence chrome builder (full re-render, self-healing):
```javascript
/** §5.3 co-presence roster. Full re-render on every session_participants
 *  frame — self-healing, no incremental diffing. Co fields are optional so
 *  an older server that never sends this frame degrades gracefully. */
function renderParticipants(participants) {
  const host = $("co-presence");
  if (!host) return;
  host.innerHTML = "";
  if (!participants.length) return;
  renderCoPresence(host, participants);
}

function renderCoPresence(host, participants) {
  const pill = document.createElement("span");
  pill.className = "co-drive-pill";  // "Co-drive" label; chat.css uses var(--ds-*)
  pill.textContent = "Co-drive";
  host.appendChild(pill);
  const cluster = document.createElement("div");
  cluster.className = "participant-avatars";
  for (const p of participants) {
    const a = document.createElement("span");
    a.className = "participant-avatar";
    a.title = p.email || "";
    a.textContent = (p.email || "?").charAt(0).toUpperCase();
    cluster.appendChild(a);
  }
  host.appendChild(cluster);
  // Invite (owner) / Fork (collaborator) affordances.
  const isOwner = participants.some(
    (p) => p.email === currentUserEmail && p.role === "owner",
  );
  const btn = document.createElement("button");
  btn.className = "co-presence-action";
  if (isOwner) {
    btn.textContent = "Invite";
    btn.dataset.action = "invite";
  } else {
    btn.textContent = "Fork";
    btn.dataset.action = "fork";
  }
  host.appendChild(btn);
}
```
- [ ] Add a `#co-presence` host element to the chat sidebar/header template the renderer targets. Locate the chat shell template (the one `chat.js` operates on — extends `base_page.html`/`base_ds.html`) and add `<div id="co-presence" class="co-presence"></div>` in the thread header block. Any CSS for `.co-drive-pill` / `.participant-avatar` / `.msg-sender-attr` / `.co-presence-action` goes in the page's `chat.css` using only `var(--ds-*)` tokens (no raw `#hex`, no `var(--primary)`).
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_copresence_web_surface.py -v`
- [ ] Run the design-system contract guard to confirm no token violation: `.venv/bin/pytest tests/test_design_system_contract.py --tb=short -n auto -q`
- [ ] Commit: `git add app/web/static/js/chat.js tests/test_copresence_web_surface.py && git commit -m "Co-presence web surface: pill, avatar cluster, sender attribution, invite/fork, session_participants frame"`

---

## Task 16 — Slack binding hardening (SR-12)

**Files:**
- Modify: `services/slack_bot/binding.py`
- Test: `tests/test_binding_hardening.py`

- [ ] Write failing `tests/test_binding_hardening.py`:
```python
import duckdb
import pytest


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE users(id VARCHAR, email VARCHAR)")
    c.execute("INSERT INTO users VALUES ('ua','a@example.com')")
    return c


def test_one_active_code_per_slack_user(conn):
    from services.slack_bot.binding import issue_verification_code
    issue_verification_code(conn, slack_user_id="U1")
    c2 = issue_verification_code(conn, slack_user_id="U1")
    rows = conn.execute("SELECT code FROM slack_binding_codes WHERE slack_user_id='U1'").fetchall()
    assert len(rows) == 1 and rows[0][0] == c2  # prior deleted on re-issue


def test_issuance_throttle(conn):
    from services.slack_bot.binding import issue_verification_code, BindingThrottled
    for _ in range(3):
        issue_verification_code(conn, slack_user_id="U2")
    with pytest.raises(BindingThrottled):
        issue_verification_code(conn, slack_user_id="U2")


def test_attempt_lockout_on_redeem(conn):
    from services.slack_bot.binding import issue_verification_code, redeem_verification_code
    issue_verification_code(conn, slack_user_id="U1")
    for _ in range(5):
        assert redeem_verification_code(conn, user_email="a@example.com", code="000000") is False
    real = conn.execute("SELECT code FROM slack_binding_codes WHERE slack_user_id='U1'").fetchone()
    if real:
        assert redeem_verification_code(conn, user_email="a@example.com", code=real[0]) is False
```
- [ ] Run, expect FAIL: `.venv/bin/pytest tests/test_binding_hardening.py -v`
- [ ] Rework `services/slack_bot/binding.py`: extend `_ensure_table` to add `attempts INTEGER NOT NULL DEFAULT 0` to `slack_binding_codes` and create `slack_binding_issue_log(slack_user_id VARCHAR, issued_at TIMESTAMP)`. Add `class BindingThrottled(Exception)`. Rework `issue_verification_code` and `redeem_verification_code`:
```python
_MAX_ISSUE_PER_WINDOW = 3
_MAX_REDEEM_ATTEMPTS = 5


class BindingThrottled(Exception):
    pass


def issue_verification_code(conn: duckdb.DuckDBPyConnection, *, slack_user_id: str) -> str:
    _ensure_table(conn)
    recent = conn.execute(
        "SELECT count(*) FROM slack_binding_issue_log WHERE slack_user_id=? "
        "AND issued_at > current_timestamp - INTERVAL '10 minutes'",
        [slack_user_id],
    ).fetchone()[0]
    if recent >= _MAX_ISSUE_PER_WINDOW:
        raise BindingThrottled(slack_user_id)
    conn.execute("DELETE FROM slack_binding_codes WHERE slack_user_id = ?", [slack_user_id])
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        "INSERT INTO slack_binding_codes(code, slack_user_id, issued_at, attempts) "
        "VALUES (?, ?, current_timestamp, 0)", [code, slack_user_id],
    )
    conn.execute(
        "INSERT INTO slack_binding_issue_log(slack_user_id, issued_at) "
        "VALUES (?, current_timestamp)", [slack_user_id],
    )
    return code
```
For `redeem_verification_code`: look up the row by `code`; on no match, increment `attempts` for any active code rows of the relevant `slack_user_id` (or, since the redeemer supplies only a code, increment all rows whose `slack_user_id` matches the most-recently-issued code — simplest: increment every row, then check lockout). Concretely:
```python
def redeem_verification_code(conn, *, user_email: str, code: str) -> bool:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT slack_user_id, issued_at, attempts FROM slack_binding_codes WHERE code = ?",
        [code],
    ).fetchone()
    if not row:
        # wrong code: charge an attempt against every outstanding code and
        # delete any that hit the lockout ceiling.
        conn.execute("UPDATE slack_binding_codes SET attempts = attempts + 1")
        conn.execute(
            "DELETE FROM slack_binding_codes WHERE attempts >= ?",
            [_MAX_REDEEM_ATTEMPTS],
        )
        return False
    slack_user_id, issued_at, attempts = row
    if attempts >= _MAX_REDEEM_ATTEMPTS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    now = datetime.now()
    if (now - issued_at).total_seconds() > _CODE_TTL_SECONDS:
        conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
        return False
    conn.execute("UPDATE users SET slack_user_id = ? WHERE email = ?", [slack_user_id, user_email])
    conn.execute("DELETE FROM slack_binding_codes WHERE code = ?", [code])
    # audit every redeem (best-effort)
    try:
        from src.repositories import audit_repo
        audit_repo().log(user_id=None, action="slack.bind",
                         resource=f"slack_user:{slack_user_id}",
                         params={"email": user_email})
    except Exception:
        pass
    return True
```
(After the wrong-code attempt bump + lockout-delete, the test's 5 wrong attempts leave the real code at `attempts == 5 >= _MAX_REDEEM_ATTEMPTS`, so it is deleted and the final correct-code redeem returns False — matching the test. Confirm `audit_repo().log` signature against `app/api/sync.py:1062` usage and adapt; if `user_id` must be non-null, look up the user id from `users` by email first.)
- [ ] Add a comment near `redeem_verification_code` documenting the SR-12 co-drive pin: re-binding updates `users.slack_user_id` but never rewrites `chat_session_participants.user_id`, so an active participant's identity is immutable mid-session (the `user_id` is pinned at JOIN time by Task 14's `invite`).
- [ ] Run, expect PASS: `.venv/bin/pytest tests/test_binding_hardening.py -v`
- [ ] Commit: `git add services/slack_bot/binding.py tests/test_binding_hardening.py && git commit -m "Harden Slack binding: one active code, issuance throttle, attempt lockout, audit"`

---

## Task 17 — Full suite, SR gate sweep, CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`
- Test: all

- [ ] Run the complete SR gate set together and confirm green:
```
.venv/bin/pytest tests/test_session_principal.py tests/test_grant_intersection.py tests/test_copresence_resolver.py tests/test_copresence_datapath.py tests/test_copresence_workspace.py tests/test_copresence_api.py tests/test_copresence_budgets.py tests/test_binding_hardening.py tests/test_copresence_web_surface.py tests/db_pg/test_chat_pg.py --tb=short -n auto -q
```
- [ ] Run the full suite (what CI runs): `.venv/bin/pytest tests/ --tb=short -n auto -q`. Fix any failure in code you touched. For pre-existing unrelated failures, confirm via `git stash` that they reproduce on a clean branch and note in the PR body.
- [ ] Verify the SR-2 hard gate is present and passing (the merge blocker): `.venv/bin/pytest tests/test_copresence_datapath.py::test_co_token_403_on_single_participant_table -v`
- [ ] Read `CHANGELOG.md`, then add under `## [Unreleased]`:
```markdown
### Added
- Live co-drive co-presence authorization: a co-session authorizes against the
  intersection of all live participants' grants (`SessionPrincipal`,
  `compute_grant_intersection`, `can_access_session`) with no admin
  short-circuit; the co-session JWT carries no participant identity (read live
  from `chat_session_participants`); fork-on-invite, membership-gated join,
  atomic leave teardown with respawn under the narrowed intersection,
  per-sender budgets/rate-limits/caps, and an ephemeral workspace that never
  mounts a personal directory or `CLAUDE.local.md`. Invite/join/leave/fork
  endpoints are RBAC-gated; every fork is audited.
- Co-presence web surface: a Co-drive pill, participant-avatar cluster,
  per-message sender attribution, and Invite/Fork affordances, driven by a new
  `session_participants` WebSocket frame (all co fields optional → graceful
  degradation on older servers).

### Changed
- `can_access_table`, `get_accessible_tables`, `StackResolver.stack`, and the
  sync manifest builder now accept either a user dict or a `SessionPrincipal`,
  so every audited data-read path (`/api/data`, `/api/catalog`,
  `/api/sync/manifest`, `/api/v2/{scan,sample,schema}`) authorizes a
  co-session against the live intersection; settings-mutation and stack-management
  endpoints hard-deny a `SessionPrincipal`.
- Slack binding: at most one active verification code per Slack user, issuance
  throttling, per-code attempt lockout, and an audit entry on every redeem.

### Security
- A single-user token aimed at an `is_co_session` session is rejected
  (`invalid_token`) at the resolver, independent of minter correctness.
- `require_admin` hard-denies a `SessionPrincipal` before any admin check.
```
- [ ] Commit: `git add CHANGELOG.md && git commit -m "Changelog: co-drive authorization, web surface, and Slack binding hardening"`

---

## Verification checklist (must hold before merge)

- [ ] **5a present:** Task 0 gate prints `5a present: OK`.
- [ ] SR-1: `can_access_session` does **not** import or call `is_user_admin` / `can_access` (grep the function body); `_allowed_ids_for_user` and `compute_grant_intersection` route through the repository factory, no raw `resource_grants`/`users` SQL on `conn` (PG-parity).
- [ ] SR-2: `tests/test_copresence_datapath.py::test_co_token_403_on_single_participant_table` is green; every audited site (`sync.py:798/854/969/1054/1331/1376`, `stack.py:121`, `v2_sample.py:120`, and the `can_access_table` callers in `data.py`/`catalog.py`/`v2_scan.py`/`v2_sample.py`/`v2_schema.py`/`web/router.py`) either routes a `SessionPrincipal` through the chokepoint or hard-denies; `access.py` audited as having no such call site.
- [ ] SR-3: single-user token against a co-session → `invalid_token` at resolver, on a single shared `get_system_db()` connection closed in `finally`.
- [ ] SR-4: minted co JWT has no `participants` claim and `email == ""`; `sub == "session:<id>"`; encoded with the canonical `app/auth/jwt` secret so `verify_token` decodes it.
- [ ] SR-5: `_spawn_runner` co-branch never reads `AGNES_SESSION_JWT_SEED`; a mint failure re-raises.
- [ ] SR-6: ephemeral dir has no `CLAUDE.local.md`, no symlink into `users/…`, fresh `memory/`, only intersection plugins; `prepare_session_dir` no longer symlinks `CLAUDE.local.md` unless `include_personal_override=True`; `download_workspace(skip=True)` is a no-op for ephemeral.
- [ ] SR-7: leave triggers `_respawn_co_runner` under the recomputed intersection; `live.participant_emails` refreshed.
- [ ] SR-8: invite seeds a summary (`build_intersection_summary`), not a raw clone (`SECRET_ROW_VALUE` absent in S1); `fork_session_as_co_session` copies no messages.
- [ ] SR-9: non-live-participant `join-ticket` → 403; `add_sink` re-verifies membership (raises `PermissionError`); leaver's sink receives zero frames after `leave_session` returns.
- [ ] SR-10: capped/rate-limited collaborator rejected on their own turn while owner passes; `_active_count_for_user` counts a co-session against every live participant; write+drain held under `_stdin_lock`.
- [ ] SR-11: respawn skips replay of a departed participant's turns and carries `sender_email`.
- [ ] SR-12: one active code per Slack user, issuance throttle, attempt lockout, audit on redeem; participant `user_id` pinned at join.
- [ ] §5.3: `session_participants` frame handled in `chat.js`; Co-drive pill + avatar cluster + per-message sender attribution + Invite/Fork affordances present; `tests/test_design_system_contract.py` green (no raw `#hex`, no `var(--primary)`).
- [ ] Dual-backend: `fork_session_as_co_session` + `fork_co_session_to_private` land on both `app/chat/persistence.py` and `src/repositories/chat_session_participants_pg.py` with green `tests/db_pg/test_chat_pg.py`.
- [ ] Vendor-agnostic: all test data uses `example.com` / synthetic ids; no customer names/hosts/tokens in code, tests, commits, or PR body.
