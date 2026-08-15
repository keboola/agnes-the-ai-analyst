"""Login-time provisioning for Keboola multi-project auth.

Given the projects a Keboola OAuth sign-in discovered (see
``keboola_projects``), this module turns each one into a working Agnes
setup with no hand-copied tokens:

1. a project-scoped PAT minted from the OAuth access token and vaulted as
   the connection's storage token (plus the master slot when the minted
   token verifies as a master token — that is what the semantic-layer sync
   enumerates);
2. a ``source_connections`` row per ``(stack_url, project_id)`` — created if
   missing, its credential rotated only when this same user auto-provisioned
   it (an admin-managed row's token is never overwritten, only an empty slot
   is filled);
3. a ``kbc-{project_id}-{role}`` group per discovered role, the user's
   ``source='keboola_sync'`` memberships diffed to match the discovery on
   every login (access lost upstream = membership removed here);
4. ``tool_grants`` from the project's chat tools to those groups — an
   admin-role group gets every tool, any other role only the non-mutating
   ones (the passthrough policy gate additionally refuses mutating tools to
   non-admin Agnes users regardless);
5. the slow tail — chat-tools enablement (MCP introspection) and the
   semantic-layer refresh — runs as a post-response background task, so a
   login never waits on a package download.

Group *memberships* are the revocation lever, deliberately: ``tool_grants``
rows carry no provenance, so a sync that deleted grants could destroy an
admin's hand-made ones. A user who loses a project upstream drops out of its
groups on next login and reaches nothing, while the group's grants stay for
its remaining members.

Failure posture: discovery itself is the login trust boundary and fails
closed in the callback; everything HERE is per-project best-effort — one
project's PAT mint failing must not block the login or the other projects.
No token material in logs or outcome errors, ever.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv

logger = logging.getLogger(__name__)

#: ``user_group_members.source`` for rows this module owns — diffed on every
#: login, never touching admin/system_seed/google_sync rows.
MEMBERSHIP_SOURCE = "keboola_sync"
MEMBERSHIP_ADDED_BY = "system:keboola-sync"

#: The one Keboola role whose minted PAT is writable and whose group is
#: granted mutating tools. Matches the verify path's exact-string role
#: comparison.
ADMIN_ROLE = "admin"

#: Reserved per-user-secrets ``source_id`` under which a ``select``-mode
#: login stashes its pending discovery (OAuth access token + project list,
#: vault-encrypted) until the user imports. Never collides with derived
#: chat-tools sources (``kbc-chat-{uuid}``).
PENDING_DISCOVERY_SOURCE_ID = "kbc-login-discovery"

#: How long a stored discovery stays importable. Short on purpose — the blob
#: carries the user's OAuth access token, and the token itself is unlikely to
#: outlive this by much anyway.
PENDING_DISCOVERY_TTL_SECONDS = 15 * 60


class DiscoveryStateError(Exception):
    """A ``select``-mode import was attempted with no (or an expired/invalid)
    stored discovery. ``reason`` is machine-readable for the endpoint."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


@dataclass
class ProjectOutcome:
    """What provisioning did (or could not do) for one discovered project."""

    project_id: str
    project_name: str
    role: str
    connection_id: Optional[str] = None
    connection_created: bool = False
    token_stored: bool = False
    master_token_stored: bool = False
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    #: True when a still-valid stored token made minting unnecessary.
    token_reused: bool = False
    error: Optional[str] = None


@dataclass
class ProvisionSummary:
    """Inline provisioning result + the work deferred to the background task."""

    outcomes: List[ProjectOutcome] = field(default_factory=list)
    desired_group_ids: List[str] = field(default_factory=list)
    #: Connections whose chat tools still need enabling (MCP introspection —
    #: slow, background only), with the grants to apply once tools register.
    connections_needing_chat_tools: List[str] = field(default_factory=list)
    deferred_grants: List[Dict[str, str]] = field(default_factory=list)
    semantic_sync_needed: bool = False
    memberships_added: int = 0
    memberships_removed: int = 0
    #: Cleared when any project's role group could not be ensured — the
    #: desired set is then incomplete, and removing memberships against an
    #: incomplete picture would strip access the user still holds upstream
    #: (Devin Review on this PR). Additions stay safe either way.
    membership_removals_safe: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "projects": [asdict(o) for o in self.outcomes],
            "memberships_added": self.memberships_added,
            "memberships_removed": self.memberships_removed,
            "chat_tools_pending": list(self.connections_needing_chat_tools),
            "semantic_sync_scheduled": self.semantic_sync_needed,
        }


def role_slug(role: str) -> str:
    """``readOnly`` → ``readonly`` etc.; empty/exotic roles become ``member``
    so a group name is always addressable."""
    slug = re.sub(r"[^a-z0-9]+", "-", (role or "").lower()).strip("-")
    return slug or "member"


def group_name_for(project_id: str, role: str) -> str:
    return f"kbc-{project_id}-{role_slug(role)}"


def _find_connections(stack_url: str, project_id: str) -> List[Dict[str, Any]]:
    """Every Keboola connection bound to ``(stack_url, project_id)``, sorted
    by id so all callers agree on which row is canonical when a race left a
    duplicate behind (see the reconcile in ``_provision_one``)."""
    from src.repositories import source_connections_repo

    stack = stack_url.rstrip("/")
    rows = []
    for row in source_connections_repo().list(source_type="keboola"):
        config = row.get("config") or {}
        if (config.get("stack_url") or "").rstrip("/") != stack:
            continue
        known = config.get("project_id")
        if known is not None and str(known) == str(project_id):
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("id")))
    return rows


def _find_connection(stack_url: str, project_id: str) -> Optional[Dict[str, Any]]:
    """The canonical Keboola connection bound to ``(stack_url, project_id)``.

    One connection per project per stack is the invariant this module keeps
    (the semantic-layer sync dedupes on exactly that identity); rows without
    a recorded ``project_id`` are an admin's half-configured business and are
    never matched or touched. Should duplicates ever exist, every caller
    deterministically picks the same (smallest-id) row.
    """
    rows = _find_connections(stack_url, project_id)
    return rows[0] if rows else None


def is_project_connected(project_id: str) -> bool:
    """Whether the instance's stack already has a connection for this project
    — the ``imported`` flag on the select-mode projects listing."""
    stack = kv.stack_url() or ""
    return bool(stack) and _find_connection(stack, project_id) is not None


def _unique_connection_name(base: str, project_id: str) -> str:
    """A connection name not yet taken (``name`` is unique): the project name,
    then ``{name} ({project_id})``, then a uuid-suffixed last resort."""
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    candidates = [base, f"{base} ({project_id})", f"{base} ({project_id}) {uuid4().hex[:4]}"]
    for name in candidates:
        if repo.get_by_name(name) is None:
            return name
    return f"{base} ({project_id}) {uuid4().hex[:8]}"


def _may_write_secret(connection: Dict[str, Any], user_email: str, *, slot_key: str) -> bool:
    """Whether this login may write the vault slot ``slot_key`` on
    ``connection``.

    Rotation is allowed only on a row this same user auto-provisioned
    (``config.user_email`` matches — the dedup identity the spec names).
    Anything else (admin-managed, or another user's row) only ever gets an
    EMPTY slot filled: auto-provisioning must never overwrite a credential a
    human placed, but filling a hole makes an admin-created, token-less row
    start working after one login.
    """
    config = connection.get("config") or {}
    owner = (config.get("user_email") or "").strip().lower()
    if owner and owner == (user_email or "").strip().lower():
        return True
    from src.repositories import connection_secrets_repo

    try:
        return not connection_secrets_repo().has(slot_key)
    except Exception:
        return False


def _ensure_group(project: kp.DiscoveredProject) -> Dict[str, Any]:
    from src.repositories import user_groups_repo

    return user_groups_repo().ensure(
        group_name_for(project.id, project.role),
        description=(
            f"Keboola project {project.name or project.id} — role {project.role or 'member'} "
            "(membership synced at Keboola sign-in)"
        ),
        created_by=MEMBERSHIP_ADDED_BY,
    )


def apply_tool_grants(connection_id: str, group_id: str, role: str) -> int:
    """Grant the connection's registered chat tools to ``group_id`` per the
    role policy (admin: everything; other roles: non-mutating only).
    Idempotent — ``add_grant`` is ON CONFLICT DO NOTHING. Returns how many
    tools the group now holds a grant for from this pass."""
    from src.keboola_chat_tools import derived_source_id
    from src.repositories import tool_registry_repo

    registry = tool_registry_repo()
    granted = 0
    admin_like = role == ADMIN_ROLE
    for tool in registry.list_for_source(derived_source_id(connection_id)):
        if admin_like or not tool.get("mutating"):
            registry.add_grant(tool["tool_id"], group_id)
            granted += 1
    return granted


def _verify_project_token(stack_url: str, token: str) -> Optional[Dict[str, Any]]:
    """``/tokens/verify`` payload for ``token``, or None on any upstream
    failure (logged redacted — never the token, never a proxy-echoed body)."""
    import requests as _requests

    from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError

    client = KeboolaStorageClient(url=stack_url, token=token)
    try:
        return client.verify_token()
    except (StorageApiError, _requests.RequestException) as exc:
        logger.info("keboola auto-provision: token verify failed: %s", client._redact(exc))
        return None


def _mint_and_store_tokens(
    connection: Dict[str, Any],
    project: kp.DiscoveredProject,
    access_token: str,
    stack_url: str,
    user_email: str,
    outcome: ProjectOutcome,
) -> None:
    """Mint the project PAT (only when it buys anything), preflight it, and
    vault what we are allowed to.

    Sets ``outcome.token_stored`` / ``master_token_stored`` / ``token_reused``
    / ``error``. The PAT is verified against the stack before anything is
    stored: a token that opens a DIFFERENT project than the one this
    connection is bound to must never land in its vault slot (same invariant
    as the admin secret endpoint's project-mismatch preflight).

    Reuse before mint: ``auto`` mode runs on EVERY login, and a fresh PAT
    each time would pile up still-valid orphaned credentials upstream —
    nothing here can revoke a superseded token (the platform surface has no
    delete). So when the stored token still verifies for this project, and
    minting could not improve the master slot (slot already filled, stored
    token IS a master, the role could not mint one, or the slot is not ours
    to write), nothing is minted at all. (Devin Review on this PR.)
    """
    from app.keboola_identity import project_identity
    from src.keboola_chat_tools import derived_source_id
    from src.repositories import connection_secrets_repo, mcp_sources_repo, shared_secrets_repo

    from app.api.admin_source_connections import master_secret_key

    connection_id = connection["id"]
    secrets = connection_secrets_repo()
    may_storage = _may_write_secret(connection, user_email, slot_key=connection_id)
    may_master = _may_write_secret(connection, user_email, slot_key=master_secret_key(connection_id))
    if not (may_storage or may_master):
        # Neither slot is ours to touch (admin-managed row with both filled)
        # — minting would only create an upstream credential nobody stores.
        return
    if not may_storage and project.role != ADMIN_ROLE:
        # Only the master slot is writable, and a non-admin's PAT is minted
        # read-only — that can never verify as a master token, so there is
        # nothing a mint could store.
        return

    if may_storage:
        stored: Optional[str] = None
        try:
            stored = secrets.get(connection_id) if secrets.has(connection_id) else None
        except Exception:
            stored = None
        if stored:
            info = _verify_project_token(stack_url, stored)
            stored_id, _ = project_identity(info) if info is not None else (None, "")
            if info is not None and stored_id is not None and str(stored_id) == str(project.id):
                stored_master = bool(info.get("isMasterToken"))
                try:
                    master_present = secrets.has(master_secret_key(connection_id))
                except Exception:
                    master_present = False
                if stored_master and may_master and not master_present:
                    # The stored token can fill the empty master slot itself.
                    try:
                        secrets.upsert(master_secret_key(connection_id), stored)
                        outcome.master_token_stored = True
                        master_present = True
                    except Exception:
                        logger.warning(
                            "keboola auto-provision: could not store the master token for connection %s",
                            connection_id,
                            exc_info=True,
                        )
                if master_present or stored_master or project.role != ADMIN_ROLE or not may_master:
                    outcome.token_reused = True
                    return
            # Stored token is stale/foreign — fall through and mint fresh.

    try:
        pat = kp.exchange_project_pat(access_token, project.id, read_only=project.role != ADMIN_ROLE)
    except kp.KeboolaProjectApiError as exc:
        outcome.error = f"pat_exchange: {exc.reason}"
        logger.warning("keboola auto-provision: PAT mint failed for project %s: %s", project.id, exc.reason)
        return

    info = _verify_project_token(stack_url, pat)
    if info is None:
        outcome.error = "pat_verify_failed"
        logger.warning("keboola auto-provision: minted PAT for project %s failed verify", project.id)
        return
    verified_id, verified_name = project_identity(info)
    if verified_id is None or str(verified_id) != str(project.id):
        outcome.error = "pat_project_mismatch"
        logger.warning(
            "keboola auto-provision: minted PAT verifies as project %s, expected %s; refusing to store",
            verified_id,
            project.id,
        )
        return
    is_master = bool(info.get("isMasterToken"))

    if may_storage:
        try:
            secrets.upsert(connection_id, pat)
            outcome.token_stored = True
        except Exception:
            outcome.error = "vault_write_failed"
            logger.warning(
                "keboola auto-provision: could not store the storage token for connection %s",
                connection_id,
                exc_info=True,
            )
            return
        # Keep the chat-tools copy in step, exactly like the admin secret
        # endpoint does on rotation — only an EXISTING derived source is
        # updated, never created here.
        try:
            if mcp_sources_repo().get(derived_source_id(connection_id)) is not None:
                shared_secrets_repo().upsert(derived_source_id(connection_id), pat)
        except Exception:
            logger.warning(
                "keboola auto-provision: stored a token for connection %s but could not re-sync the chat-tools copy",
                connection_id,
                exc_info=True,
            )
        # Refresh the recorded display name on rows this flow owns.
        config = dict(connection.get("config") or {})
        fresh_name = project.name or verified_name
        if fresh_name and config.get("project_name") != fresh_name and config.get("user_email"):
            from src.repositories import source_connections_repo

            config["project_name"] = fresh_name
            try:
                source_connections_repo().update(connection_id, config=config)
            except Exception:
                logger.debug("could not refresh project_name on connection %s", connection_id)

    if is_master and may_master:
        try:
            secrets.upsert(master_secret_key(connection_id), pat)
            outcome.master_token_stored = True
        except Exception:
            logger.warning(
                "keboola auto-provision: could not store the master token for connection %s",
                connection_id,
                exc_info=True,
            )
    if not is_master:
        # Named fallback from the spec: data access works on the plain PAT;
        # the semantic layer needs a master token and is skipped until an
        # admin supplies one for this connection.
        logger.info(
            "keboola auto-provision: project %s token is not a master token — "
            "semantic layer sync will skip this project until one is stored",
            project.id,
        )


def _provision_one(
    project: kp.DiscoveredProject,
    *,
    user: Dict[str, Any],
    access_token: str,
    stack_url: str,
    vault_ok: bool,
    summary: ProvisionSummary,
) -> ProjectOutcome:
    from src.keboola_chat_tools import derived_source_id
    from src.repositories import mcp_sources_repo, source_connections_repo, tool_registry_repo

    outcome = ProjectOutcome(project_id=project.id, project_name=project.name, role=project.role)
    user_email = str(user.get("email") or "")

    # Group first: membership must survive a credential hiccup, or a token
    # blip at login would strip the user's access to an already-working
    # project.
    try:
        group = _ensure_group(project)
        outcome.group_id = group["id"]
        outcome.group_name = group["name"]
    except Exception:
        # The desired membership set is now incomplete — the sync may still
        # ADD, but must not REMOVE against a partial picture (Devin Review
        # on this PR; see ProvisionSummary.membership_removals_safe).
        summary.membership_removals_safe = False
        logger.warning("keboola auto-provision: could not ensure group for project %s", project.id, exc_info=True)

    connection = _find_connection(stack_url, project.id)
    if connection is None:
        if not vault_ok:
            # A connection whose token cannot be stored is a dead row; leave
            # creation to a login after the operator sets AGNES_VAULT_KEY.
            outcome.error = "vault_key_not_configured"
            return outcome
        try:
            connection_id = str(uuid4())
            source_connections_repo().create(
                id=connection_id,
                name=_unique_connection_name(project.name or f"Keboola project {project.id}", project.id),
                source_type="keboola",
                config={
                    "stack_url": stack_url.rstrip("/"),
                    "project_id": project.id,
                    "project_name": project.name,
                    "user_email": user_email,
                    "provisioned_by": "keboola_login",
                },
                created_by=user.get("id"),
            )
            connection = source_connections_repo().get(connection_id)
            outcome.connection_created = connection is not None
        except Exception:
            outcome.error = "connection_create_failed"
            logger.warning(
                "keboola auto-provision: could not create a connection for project %s",
                project.id,
                exc_info=True,
            )
            return outcome
        # Two concurrent logins can both miss the find above and both insert
        # — there is no unique constraint on (stack_url, project_id) (Devin
        # Review on this PR). Re-read and let the smallest id win: both
        # racers agree on the winner, and the loser deletes only the row IT
        # just created, before any secret lands in it. Provisioning then
        # continues against the canonical row (the empty-slot rule stores
        # the token there if it is still missing).
        rivals = _find_connections(stack_url, project.id)
        if rivals and connection is not None and rivals[0]["id"] != connection["id"]:
            try:
                source_connections_repo().delete(connection["id"])
            except Exception:
                logger.warning(
                    "keboola auto-provision: could not remove the duplicate connection %s for project %s",
                    connection["id"],
                    project.id,
                    exc_info=True,
                )
            connection = rivals[0]
            outcome.connection_created = False
    if connection is None:
        outcome.error = "connection_create_failed"
        return outcome
    outcome.connection_id = connection["id"]

    if vault_ok:
        _mint_and_store_tokens(connection, project, access_token, stack_url, user_email, outcome)
        if outcome.master_token_stored:
            summary.semantic_sync_needed = True
    elif not outcome.error:
        outcome.error = "vault_key_not_configured"

    # Chat tools. Three states, three behaviors:
    # - live source with registered tools → the role group's grants right now;
    # - no derived source at all + a usable token → defer to the background
    #   enable (MCP introspection is far too slow for a login) with the
    #   grants to apply once its tools register;
    # - source present but DISABLED (or tool-less) → hands off entirely. The
    #   off-switch is an admin decision; auto-granting (or re-enabling) from
    #   a login would widen access the admin deliberately cut.
    source_row = mcp_sources_repo().get(derived_source_id(connection["id"]))
    source_live = source_row is not None and source_row.get("enabled") is not False
    has_tools = bool(tool_registry_repo().list_for_source(derived_source_id(connection["id"])))
    if outcome.group_id is not None:
        if source_live and has_tools:
            try:
                apply_tool_grants(connection["id"], outcome.group_id, project.role)
            except Exception:
                logger.warning(
                    "keboola auto-provision: could not grant tools of connection %s",
                    connection["id"],
                    exc_info=True,
                )
        elif source_row is None:
            summary.deferred_grants.append(
                {"connection_id": connection["id"], "group_id": outcome.group_id, "role": project.role}
            )
    if source_row is None and (outcome.token_stored or _has_storage_token(connection["id"])):
        summary.connections_needing_chat_tools.append(connection["id"])
    return outcome


def _has_storage_token(connection_id: str) -> bool:
    from src.repositories import connection_secrets_repo

    try:
        return bool(connection_secrets_repo().has(connection_id))
    except Exception:
        return False


def sync_group_memberships(
    user_id: str, desired_group_ids: List[str], *, allow_removals: bool = True
) -> tuple[int, int]:
    """Diff the user's ``source='keboola_sync'`` memberships to the desired
    set: additions for projects gained, removals for projects lost upstream.
    Rows from any other source (admin, system_seed, google_sync) are never
    touched; a pair an admin already owns stays the admin's (``add_member``
    keeps the original source on conflict).

    ``allow_removals=False`` (a group-ensure hiccup left the desired set
    incomplete): a write blip may only fail to add, never strip access the
    user still holds upstream — the next clean login reconciles fully."""
    from src.repositories import user_group_members_repo

    members = user_group_members_repo()
    current = {
        row["group_id"]
        for row in members.list_groups_with_meta_for_user(user_id)
        if row.get("source") == MEMBERSHIP_SOURCE
    }
    desired = set(desired_group_ids)
    added = removed = 0
    for group_id in sorted(desired - current):
        members.add_member(user_id=user_id, group_id=group_id, source=MEMBERSHIP_SOURCE, added_by=MEMBERSHIP_ADDED_BY)
        added += 1
    if not allow_removals:
        if current - desired:
            logger.warning(
                "keboola auto-provision: keeping %d keboola_sync membership(s) for user %s — "
                "the desired set is incomplete this pass (a role group could not be ensured)",
                len(current - desired),
                user_id,
            )
        return added, 0
    for group_id in sorted(current - desired):
        if members.remove_member(user_id, group_id, require_source=MEMBERSHIP_SOURCE):
            removed += 1
    return added, removed


def provision_projects(
    user: Dict[str, Any],
    to_provision: List[kp.DiscoveredProject],
    all_discovered: List[kp.DiscoveredProject],
    access_token: str,
) -> ProvisionSummary:
    """Provision ``to_provision`` and sync memberships against
    ``all_discovered`` (the full filtered discovery — the authoritative
    picture of what the user can reach right now).

    ``auto`` mode passes the same list twice. ``select`` mode passes the
    user's chosen subset as ``to_provision``; membership then covers the
    chosen projects plus every discovered project that is ALREADY connected,
    so re-login keeps previously imported projects in sync without importing
    the rest uninvited.
    """
    from app.secrets_vault import can_store_secrets

    summary = ProvisionSummary()
    stack_url = kv.stack_url() or ""
    if not stack_url:
        raise DiscoveryStateError("not_configured", "No Keboola stack URL configured")
    vault_ok = can_store_secrets()
    if not vault_ok and to_provision:
        logger.warning(
            "keboola auto-provision: AGNES_VAULT_KEY is not configured — "
            "project tokens cannot be stored, connection provisioning is skipped"
        )

    provisioned_ids = set()
    for project in to_provision:
        outcome = _provision_one(
            project,
            user=user,
            access_token=access_token,
            stack_url=stack_url,
            vault_ok=vault_ok,
            summary=summary,
        )
        summary.outcomes.append(outcome)
        provisioned_ids.add(project.id)
        if outcome.group_id:
            summary.desired_group_ids.append(outcome.group_id)

    # Discovered-but-not-provisioned projects (select mode): membership still
    # reflects the ones already connected from earlier imports.
    for project in all_discovered:
        if project.id in provisioned_ids:
            continue
        if _find_connection(stack_url, project.id) is None:
            continue
        try:
            group = _ensure_group(project)
            summary.desired_group_ids.append(group["id"])
        except Exception:
            summary.membership_removals_safe = False  # incomplete desired set — see the dataclass note
            logger.warning(
                "keboola auto-provision: could not ensure group for connected project %s",
                project.id,
                exc_info=True,
            )

    try:
        summary.memberships_added, summary.memberships_removed = sync_group_memberships(
            str(user.get("id")),
            summary.desired_group_ids,
            allow_removals=summary.membership_removals_safe,
        )
    except Exception:
        # Fail-soft, same posture as the Google group sync: a membership
        # write hiccup must not fail the login; the next login re-syncs.
        logger.warning("keboola auto-provision: membership sync failed for user %s", user.get("id"), exc_info=True)
    return summary


async def finish_login_provisioning(summary: ProvisionSummary) -> None:
    """The slow tail, run AFTER the login response: enable chat tools for
    fresh connections (MCP introspection — first run downloads the server),
    apply the deferred grants, then kick the semantic-layer refresh under the
    admin endpoint's own single-flight guard. Never raises."""
    from fastapi import HTTPException

    for connection_id in summary.connections_needing_chat_tools:
        try:
            from app.api.admin_source_connections import enable_chat_tools

            result = await enable_chat_tools(connection_id, _user={"id": MEMBERSHIP_ADDED_BY})
            logger.info(
                "keboola auto-provision: chat tools enabled for connection %s (%s tools)",
                connection_id,
                result.get("tools_registered"),
            )
        except HTTPException as exc:
            logger.warning(
                "keboola auto-provision: chat tools enable failed for connection %s: %s",
                connection_id,
                exc.detail,
            )
            continue
        except Exception:
            logger.warning(
                "keboola auto-provision: chat tools enable failed for connection %s",
                connection_id,
                exc_info=True,
            )
            continue
        # Grants strictly AFTER a successful enable, and only for sources
        # this flow itself enabled — a failed enable has no tools to grant,
        # and a source that was not on the enable list was left alone above
        # (admin-disabled or otherwise not this flow's to widen). Off the
        # event loop: this tail runs as a post-response BackgroundTask on
        # the loop, and the grant writes are plain sync DB calls.
        import asyncio

        for grant in summary.deferred_grants:
            if grant["connection_id"] != connection_id:
                continue
            try:
                await asyncio.to_thread(apply_tool_grants, connection_id, grant["group_id"], grant["role"])
            except Exception:
                logger.warning(
                    "keboola auto-provision: deferred grant failed for connection %s",
                    connection_id,
                    exc_info=True,
                )

    if summary.semantic_sync_needed:
        from app.api.keboola_semantic_layer_refresh import run_semantic_layer_refresh_background

        await run_semantic_layer_refresh_background(trigger="keboola-login")


# ---------------------------------------------------------------------------
# select mode — pending discovery stash + user-driven import
# ---------------------------------------------------------------------------


def store_pending_discovery(user: Dict[str, Any], projects: List[kp.DiscoveredProject], access_token: str) -> bool:
    """Vault the discovery (project list + the OAuth access token needed to
    mint PATs later) for the user's import decision. Returns False — logged,
    never raised — when the vault is unconfigured; the projects page then
    says to re-login after the operator sets the key."""
    from app.secrets_vault import can_store_secrets
    from src.repositories import per_user_secrets_repo

    if not can_store_secrets():
        logger.warning(
            "keboola login: AGNES_VAULT_KEY is not configured — cannot stash the "
            "discovered projects for select-mode import"
        )
        return False
    blob = {
        "v": 1,
        "access_token": access_token,
        "stack_url": kv.stack_url() or "",
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "projects": [asdict(p) for p in projects],
    }
    try:
        per_user_secrets_repo().upsert(PENDING_DISCOVERY_SOURCE_ID, str(user.get("id")), json.dumps(blob))
        return True
    except Exception:
        logger.warning("keboola login: could not stash the discovered projects", exc_info=True)
        return False


def load_pending_discovery(user_id: str) -> Optional[Dict[str, Any]]:
    """The user's stored discovery, or None when absent/expired/corrupt.
    Expiry deletes the blob — it holds a live access token and must not
    outstay its usefulness."""
    from src.repositories import per_user_secrets_repo

    repo = per_user_secrets_repo()
    try:
        raw = repo.get(PENDING_DISCOVERY_SOURCE_ID, user_id)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
        stored_at = datetime.fromisoformat(blob["stored_at"])
    except (ValueError, KeyError, TypeError):
        clear_pending_discovery(user_id)
        return None
    if (datetime.now(timezone.utc) - stored_at).total_seconds() > PENDING_DISCOVERY_TTL_SECONDS:
        clear_pending_discovery(user_id)
        return None
    return blob


def clear_pending_discovery(user_id: str) -> None:
    from src.repositories import per_user_secrets_repo

    try:
        per_user_secrets_repo().delete(PENDING_DISCOVERY_SOURCE_ID, user_id)
    except Exception:
        logger.debug("no pending keboola discovery to clear for user %s", user_id)


def provision_selected(user: Dict[str, Any], selected_ids: List[str]) -> ProvisionSummary:
    """``select``-mode import: provision the chosen subset of the stored
    discovery. Raises :class:`DiscoveryStateError` when the discovery is
    gone/expired (``discovery_expired``) or a requested id was never
    discovered (``unknown_project``) — the endpoint maps both to 4xx."""
    blob = load_pending_discovery(str(user.get("id")))
    if blob is None:
        raise DiscoveryStateError(
            "discovery_expired",
            "No pending Keboola project discovery — sign in with Keboola again to refresh it",
        )
    discovered = [
        kp.DiscoveredProject(id=str(p.get("id")), name=str(p.get("name") or ""), role=str(p.get("role") or ""))
        for p in blob.get("projects") or []
    ]
    by_id = {p.id: p for p in discovered}
    unknown = [pid for pid in selected_ids if pid not in by_id]
    if unknown:
        raise DiscoveryStateError(
            "unknown_project",
            f"Projects not in the discovered list: {', '.join(sorted(unknown))}",
        )
    chosen = [by_id[pid] for pid in dict.fromkeys(selected_ids)]
    return provision_projects(user, chosen, discovered, str(blob.get("access_token") or ""))
