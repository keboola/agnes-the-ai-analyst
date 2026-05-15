"""Admin endpoints — table discovery, registry management, instance configuration.

Every gate on this router uses ``require_admin`` from ``app.auth.access``,
which checks Admin user_group membership for both OAuth session and PAT
callers via the same ``_user_group_ids`` lookup.
"""

import logging
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any
import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.audit import AuditRepository
from src.identifier_validation import (
    is_safe_identifier as _is_safe_identifier,
    is_safe_quoted_identifier as _is_safe_quoted_identifier,
)
from src.sql_safe import is_safe_project_id as _is_safe_project_id
from src.scheduler import is_valid_schedule
from src.usage_attribution_helpers import update_flea_attribution

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# Serializes the read-modify-write of state/instance.yaml across the two
# endpoints that mutate the overlay (POST /server-config and POST /configure).
# Without it, two admins saving concurrently would each read the same overlay
# snapshot, merge their disjoint patches, and the second os.replace would silently
# drop the first patch. Single-process FastAPI workers; multi-worker deployments
# would need an OS-level file lock — documented limitation.
_overlay_write_lock = threading.Lock()

# Per-processor advisory locks for /api/admin/run-session-processor.
# Two trigger paths exist for the same processor (scheduler tick + manual
# admin POST). Without serialization, overlapping runs would re-process the
# same /data/user_sessions/* set, double-call the LLM, and pile up duplicate
# `verification_evidence` rows — the dedup short-circuit in
# VerificationProcessor only catches the create+contradiction branches, not
# create_evidence (per ADR Decision 3, which expects evidence to accumulate
# per distinct verification event). Lock is non-blocking → second caller
# gets 409 Conflict so the operator sees what happened instead of stacking
# behind a long-running tick.
_processor_run_locks: dict[str, threading.Lock] = {}
_processor_run_locks_mutex = threading.Lock()


def _get_processor_run_lock(name: str) -> threading.Lock:
    """Per-name lock factory; the registry mutex guards dict insertion so
    two threads simultaneously asking for a never-seen processor don't
    each install their own lock instance."""
    with _processor_run_locks_mutex:
        if name not in _processor_run_locks:
            _processor_run_locks[name] = threading.Lock()
        return _processor_run_locks[name]


# SSRF protection: reject private/internal URLs for keboola_url
import ipaddress as _ipaddress
import socket as _socket
from urllib.parse import urlparse as _urlparse


def _validate_url_not_private(url: str, field_name: str = "url") -> None:
    """Raise 400 if the URL host points to a private/reserved network.

    Uses DNS resolution + ipaddress checks instead of hostname regex,
    which correctly handles all IPv4/IPv6 addresses including abbreviated
    forms (fe80::1, ::1, etc.) and DNS rebinding (resolves at check time).
    """
    try:
        parsed = _urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: not a valid URL")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: missing hostname")

    # Reject well-known dangerous hostnames before DNS resolution
    if host.lower() in ("localhost", "localhost.localdomain"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must not point to a private or reserved network",
        )

    # Resolve hostname to IP addresses and check each one
    try:
        addrinfos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: could not resolve hostname",
        )

    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = _ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_name}: must not point to a private or reserved network",
            )


def _unescape_shell_quoting(s: str | None) -> str | None:
    """Defensive normalization for descriptions arriving via shell-quoting tooling.

    Some operators register tables with bash/curl invocations whose quoting
    injects literal backslash escapes into the payload (e.g. ``Don\\'t`` or
    embedded ``\\n`` instead of real newlines). The backend would otherwise
    persist those bytes verbatim and the UI would render them verbatim too.
    Mirrored in JS as ``unescapeShellQuoting`` in
    ``app/web/templates/admin_tables.html`` for already-stored rows.
    """
    if not s:
        return s
    # Order matters: protect real backslashes first.
    SENTINEL = "\x00"
    return (
        s.replace("\\\\", SENTINEL)
         .replace("\\n", "\n")
         .replace("\\r", "\r")
         .replace("\\t", "\t")
         .replace("\\'", "'")
         .replace('\\"', '"')
         .replace(SENTINEL, "\\")
    )


def _normalize_primary_key(v):
    """Coerce a string primary_key to ``[v]`` for backward compatibility.

    The 0.14.0 contract is ``Optional[List[str]]`` so composite primary keys
    (e.g. session-grain tables keyed on ``(session_id, event_date)``) round-
    trip cleanly. Pre-0.14.0 callers sent a single string; Pydantic v2
    refuses to coerce, so without this validator a CLI script posting
    ``"primary_key": "session_id"`` would now hit a 422. Wrap a bare string
    in a one-element list so old and new callers both work.
    """
    if v is None:
        return v
    if isinstance(v, str):
        return [v]
    return v


# Patches to these section paths must pass _validate_url_not_private. The
# tuple is `(section, *intermediate_keys, leaf_key)` — same SSRF gate the
# /configure wizard applies to keboola_url, so an admin can't sneak
# http://169.254.169.254/ in via the server-config editor's data_source patch.
#
# Intentionally NOT included: ``("ai", "base_url")``. The openai_compat
# provider legitimately points at internal services (LiteLLM proxy on a
# private network, on-cluster vLLM endpoint, etc.) — see
# config/instance.yaml.example "LiteLLM proxy" example. SSRF blocking
# would break those valid setups. Operators with stricter posture should
# enforce the constraint upstream (firewall / egress proxy allowlist).
# Devin ANALYSIS_0001 on PR #141 5f649a4 review.
_URL_BEARING_FIELDS: tuple[tuple[str, ...], ...] = (
    ("data_source", "keboola", "stack_url"),
)


def _validate_urls_in_patch(sections: Dict[str, Dict[str, Any]]) -> None:
    """Apply SSRF protection to every URL-bearing field present in the patch.

    Walks each registered ``(section, *path, leaf)`` against the incoming
    patch and runs ``_validate_url_not_private`` on any string value found.
    Missing intermediate keys / non-dict nodes are silently skipped — the
    patch hasn't touched that field, no validation needed.
    """
    for path in _URL_BEARING_FIELDS:
        section = path[0]
        if section not in sections:
            continue
        node: Any = sections[section]
        for key in path[1:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict):
            value = node.get(path[-1])
            if isinstance(value, str) and value:
                _validate_url_not_private(value, field_name=".".join(path))


_LOCK_TTL_MIN = 60
_LOCK_TTL_MAX = 7 * 24 * 3600  # 604800 — one week


def _validate_materialize_section(sections: Dict[str, Dict[str, Any]]) -> None:
    """Validate the materialize section patch when present.

    Checks field-level constraints that the Pydantic envelope can't enforce
    (it only validates the outer shape, not nested leaf values).
    """
    mat = sections.get("materialize")
    if not isinstance(mat, dict):
        return
    ttl = mat.get("lock_ttl_seconds")
    if ttl is None:
        return
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise HTTPException(
            status_code=422,
            detail="materialize.lock_ttl_seconds must be an integer",
        )
    if ttl < _LOCK_TTL_MIN or ttl > _LOCK_TTL_MAX:
        raise HTTPException(
            status_code=422,
            detail=(
                f"materialize.lock_ttl_seconds must be between "
                f"{_LOCK_TTL_MIN} and {_LOCK_TTL_MAX} "
                f"(got {ttl})"
            ),
        )


# --- Server-config (instance.yaml) editor -----------------------------------
#
# The /admin/server-config UI POSTs a partial dict here keyed by section
# (instance, data_source, email, telegram, jira, theme, server, auth) with
# the field values to merge into instance.yaml. Each save:
#   1. Loads the current instance.yaml (writable overlay first, then static).
#   2. Deep-merges the patch on top.
#   3. Writes to DATA_DIR/state/instance.yaml (the writable overlay).
#   4. Writes one audit_log entry tagged `instance_config.update` containing
#      a sanitized diff (secret-looking keys are masked).
# Hot-reload is OUT OF SCOPE for #91 — the response carries
# `restart_required=True` so the UI can show the banner.

# Sections an admin can mutate. Keep the list explicit so a typo'd section
# in the request body is rejected loudly instead of being silently merged
# into the YAML root and confusing future loads.
_EDITABLE_SECTIONS: tuple[str, ...] = (
    "instance",
    "data_source",
    "email",
    "telegram",
    "jira",
    "theme",
    "server",
    "auth",
    "ai",
    "openmetadata",
    "desktop",
    "corporate_memory",
    "materialize",
    "guardrails",
)

# "Danger-zone" sections — flipping these can lock operators out (auth.*) or
# break OAuth callbacks (server.hostname/host). The UI shows a confirmation
# dialog before submitting them. The API accepts them; this list exists so
# the audit entry can flag the change as high-risk and the UI can surface
# the right warning copy.
_DANGER_SECTIONS: tuple[str, ...] = ("auth", "server")

# Known-but-optional config fields per section. The /admin/server-config UI
# uses this registry alongside the YAML payload to render fields the operator
# might want to set even though they're not currently in instance.yaml.
#
# Schema per field:
#   {
#     "kind": "string" | "secret" | "bool" | "int" | "select" | "object" | "array",
#     "default": <type-appropriate default>  (optional)
#     "hint": "<one-line operator-facing help>"
#     "options": [...]              (only for kind="select")
#     "fields": {<name>: <fieldspec>}  (only for kind="object", nested fields)
#     "item_kind": "string" | ...   (only for kind="array", element type)
#     "required": bool             (defaults False; UI marks the label)
#   }
#
# Subagents 2-4 will populate the bodies. The registry enables the UI to
# render missing-but-known fields with placeholders + hints rather than
# forcing the operator to discover them via the JSON-patch textarea or
# hitting a runtime error first. The smoke fixture below
# (data_source.bigquery.billing_project) proves the renderer wiring works
# end-to-end so subagents 2-4 only have to add registry entries — they
# don't need to touch admin_server_config.html.
_KNOWN_FIELDS: dict[str, dict[str, dict]] = {
    "instance": {
        # No commonly-missing instance-level fields. The example YAML's
        # `name`/`subtitle` are always populated by `agnes setup` so they
        # render via the populated path; nothing to surface here.
    },
    "data_source": {
        "bigquery": {
            "kind": "object",
            "hint": "BigQuery connection knobs (read more in docs/DEPLOYMENT.md)",
            "fields": {
                "billing_project": {
                    "kind": "string",
                    "hint": (
                        "GCP project to bill BQ jobs against. Set when SA can read "
                        "the data project but cannot bill there (e.g. shared read-only "
                        "data project). Defaults to data_source.bigquery.project. "
                        "Mismatch → 403 USER_PROJECT_DENIED on every BQ call."
                    ),
                    # Issue #160 §4.7.5: when this field is empty in the
                    # admin form, the JS template shows "(defaults to <project>)"
                    # as placeholder text — surfacing the access.py:339-340
                    # fallback rule directly in the UI without the operator
                    # having to read source. Path is walked against the
                    # `original` config payload from GET /api/admin/server-config.
                    "placeholder_from": ["data_source", "bigquery", "project"],
                },
                "max_bytes_per_materialize": {
                    "kind": "int",
                    "default": 10737418240,
                    "hint": (
                        "Cost guardrail for query_mode='materialized' BQ scans (dry-run "
                        "check before running). Bytes processed; exceeds → registration "
                        "or sync rejected. 0 disables the gate. Default 10737418240 = 10 GiB."
                    ),
                },
                "bq_max_scan_bytes": {
                    "kind": "int",
                    "default": 5368709120,
                    "hint": (
                        "Cost guardrail for `agnes query --remote` against query_mode='remote' "
                        "BQ rows (dry-run check on the underlying SELECT before execute). "
                        "Bytes processed; exceeds → 400 remote_scan_too_large with a "
                        "`agnes snapshot create` suggestion. 0 disables the gate. Default 5368709120 = 5 GiB."
                    ),
                },
                "query_timeout_ms": {
                    "kind": "int",
                    "default": 600000,
                    "hint": (
                        "DuckDB BigQuery extension query timeout (milliseconds). Applied "
                        "via `SET bq_query_timeout_ms` after every `LOAD bigquery` on "
                        "every BQ-touching DuckDB session (orchestrator remote-view "
                        "ATTACH, BqAccess factory, standalone extractor). Extension "
                        "default is 90 000 ms = 90 s, which is too tight for analyst "
                        "queries against view-backed datasets — bumped to 600 000 ms = "
                        "10 min by default. Set 0 to fall through to the extension "
                        "default. Note: the underlying BQ jobs.query RPC caps the wait "
                        "at ~200 s per call; the extension polls on top, so the "
                        "effective ceiling is this value but each poll round-trip is "
                        "~200 s. DuckDB itself emits a warning when this is set above "
                        "~200 s — that warning is informational, not an error."
                    ),
                },
            },
        },
        "keboola": {
            "kind": "object",
            "hint": "Keboola Storage API connection",
            "fields": {
                "stack_url": {
                    "kind": "string",
                    "hint": (
                        "e.g. https://connection.keboola.com (instance-specific stack URL). "
                        "Validated against private-IP allowlist on save (SSRF guard)."
                    ),
                },
                "project_id": {
                    "kind": "string",
                    "hint": "Keboola project ID (numeric, but kept as string in YAML).",
                },
            },
        },
    },
    "email": {
        # SMTP fields render via the populated path (always set when email
        # is enabled); no commonly-missing optional knobs at this layer.
    },
    "telegram": {
        # Rarely missing; leave empty.
    },
    "jira": {
        # Webhook + REST credentials always present when Jira is configured.
    },
    "theme": {
        # Cosmetic only; rarely missing.
    },
    "server": {
        # TLS / hostname knobs are mostly env-side; nothing to surface here.
    },
    "auth": {
        "allowed_domain": {
            "kind": "string",
            "hint": (
                "Comma-separated list of allowed sign-in email domains (e.g. "
                "'acme.com,acme-internal.com'). Single domain works too. Empty → no "
                "domain restriction (any verified Google identity can sign in)."
            ),
        },
    },
    "ai": {
        "base_url": {
            "kind": "string",
            "hint": (
                "Required for provider='openai_compat' (LiteLLM, OpenRouter, vLLM, etc.). "
                "Ignored when provider='anthropic'. Examples: https://litellm.example.com, "
                "https://openrouter.ai/api/v1."
            ),
        },
        "structured_output": {
            "kind": "select",
            "options": ["strict", "json", "auto"],
            "default": "auto",
            "hint": (
                "JSON-schema enforcement strategy. strict=Layer 1 only "
                "(Anthropic/OpenAI native, fail otherwise). json=Layer 1 + Layer 2 "
                "fallback. auto=all three layers including prompt-based JSON (most "
                "compatible, least strict)."
            ),
        },
    },
    "openmetadata": {
        "url": {
            "kind": "string",
            "hint": "Base URL of your OpenMetadata server (e.g. https://catalog.example.com).",
        },
        "token": {
            "kind": "secret",
            "hint": (
                "JWT bearer token. Use ${OPENMETADATA_TOKEN} env-var reference "
                "(don't paste secret directly)."
            ),
        },
        "cache_ttl_seconds": {
            "kind": "int",
            "default": 3600,
            "hint": "How long to cache catalog responses in-process. Default 3600s (1h).",
        },
        "verify_ssl": {
            "kind": "bool",
            "default": True,
            "hint": (
                "TLS verification. Default true. Set false ONLY for internal CAs / "
                "self-signed certs — sends the JWT over an unverified channel."
            ),
        },
    },
    "desktop": {
        "jwt_issuer": {
            "kind": "string",
            "default": "data-analyst",
            "hint": "JWT iss claim. Match what the desktop app verifies.",
        },
        "jwt_secret": {
            "kind": "secret",
            "hint": "JWT signing secret. Use ${DESKTOP_JWT_SECRET} env-var reference.",
        },
        "url_scheme": {
            "kind": "string",
            "default": "data-analyst",
            "hint": "Custom URL scheme registered by the desktop app (data-analyst://...).",
        },
    },
    # corporate_memory governance — optional. When the section is missing
    # from instance.yaml the system runs in legacy democratic-wiki mode
    # (no admin review). Schema mirrors config/instance.yaml.example
    # lines 224-317; renderer handles arbitrary depth + arrays + maps.
    "corporate_memory": {
        "distribution_mode": {
            "kind": "select",
            "options": ["mandatory_only", "admin_curated", "hybrid"],
            "default": "hybrid",
            "hint": (
                "How knowledge reaches users. mandatory_only = admin-only; "
                "admin_curated = admin + user voting as feedback; "
                "hybrid = default (mandatory from admin + optional from user voting)."
            ),
        },
        "approval_mode": {
            "kind": "select",
            "options": ["review_queue", "auto_publish", "threshold"],
            "default": "review_queue",
            "hint": (
                "How AI-extracted items enter the system. review_queue = admin "
                "approval required (default); auto_publish = live immediately; "
                "threshold = high-confidence auto, low-confidence to queue."
            ),
        },
        "review_period_months": {
            "kind": "int",
            "default": 6,
            "hint": "How often approved/mandatory items are flagged for re-review (months).",
        },
        "notify_on_new_items": {
            "kind": "bool",
            "default": True,
            "hint": "Notify km_admins when new pending items arrive.",
        },
        "sources": {
            "kind": "object",
            "hint": (
                "Knowledge-source ingestion. Each source has its own enabled "
                "flag + base confidence."
            ),
            "fields": {
                "claude_local_md": {
                    "kind": "object",
                    "fields": {
                        "enabled": {"kind": "bool", "default": True},
                        "confidence_base": {
                            "kind": "float",
                            "default": 0.50,
                            "hint": "Confidence assigned to extractions from CLAUDE.local.md (0-1).",
                        },
                    },
                },
                "session_transcripts": {
                    "kind": "object",
                    "fields": {
                        "enabled": {"kind": "bool", "default": True},
                        "confidence_base": {"kind": "float", "default": 0.60},
                        "max_turns_per_session": {
                            "kind": "int",
                            "default": 100,
                            "hint": "Truncate transcripts longer than this many turns.",
                        },
                        "detection_types": {
                            "kind": "array",
                            "item_kind": "string",
                            "default": [
                                "correction",
                                "confirmation",
                                "unprompted_definition",
                            ],
                            "hint": (
                                "Which extraction patterns to detect. Each entry "
                                "is a detection-type tag."
                            ),
                        },
                    },
                },
            },
        },
        "extraction": {
            "kind": "object",
            "fields": {
                "model": {
                    "kind": "string",
                    "default": "claude-haiku-4-5-20251001",
                    "hint": "LLM used to extract knowledge. Override for cost or quality.",
                },
                "sensitivity_check": {"kind": "bool", "default": True},
                "contradiction_check": {"kind": "bool", "default": True},
            },
        },
        "confidence": {
            "kind": "object",
            "hint": "Confidence scoring + decay rules.",
            "fields": {
                "base": {
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "float",
                    "default": {
                        "user_verification.correction": 0.90,
                        "user_verification.unprompted_definition": 0.90,
                        "user_verification.confirmation": 0.60,
                        "admin_mandate": 1.00,
                        "claude_local_md": 0.50,
                        "session_transcript": 0.50,
                    },
                    "hint": (
                        "Base score per source/detection. Keys are 'source_type' "
                        "or 'source_type.detection_type' (the dot is data, not "
                        "nesting)."
                    ),
                },
                "modifiers": {
                    # map<string, map<string, float>>. The renderer's structured
                    # editor for "map of objects with declared subfields" is a
                    # TODO (see admin_server_config.html); for now this falls
                    # back to a JSON textarea — admins editing it see the
                    # schema doc inline via the hint.
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "object",
                    "value_fields": {},  # signals the JSON-textarea fallback
                    "hint": (
                        "Per-key modifier step sizes applied to base when "
                        "optional signals are present (3-level dotted paths). "
                        "Edit as a JSON object — outer keys mirror confidence.base "
                        "keys; inner objects map signal name to bonus float."
                    ),
                },
                "decay": {
                    "kind": "object",
                    "fields": {
                        "mode": {
                            "kind": "select",
                            "options": ["linear", "exponential"],
                            "default": "exponential",
                        },
                        "half_life_months": {
                            "kind": "int",
                            "default": 12,
                            "hint": "Used when mode=exponential.",
                        },
                        "decay_rate_monthly": {
                            "kind": "float",
                            "default": 0.02,
                            "hint": "Used when mode=linear.",
                        },
                        "floor": {
                            "kind": "map",
                            "key_kind": "string",
                            "value_kind": "float",
                            "default": {
                                "admin_mandate": 0.50,
                                "user_verification": 0.40,
                                "default": 0.0,
                            },
                            "hint": (
                                "Per-source minimum confidence — items never decay "
                                "below this floor."
                            ),
                        },
                    },
                },
            },
        },
        "contradiction_detection": {
            "kind": "object",
            "fields": {
                "enabled": {"kind": "bool", "default": True},
                "max_candidates": {
                    "kind": "int",
                    "default": 10,
                    "hint": "Max contradiction candidates to evaluate per new item.",
                },
            },
        },
        "entity_resolution": {
            "kind": "object",
            "fields": {
                "enabled": {"kind": "bool", "default": True},
                "entities": {
                    "kind": "map",
                    "key_kind": "string",
                    "value_kind": "array",
                    "value_item_kind": "string",
                    "default": {
                        "metrics": ["churn", "MRR", "ARR", "NPS", "CAC", "LTV"],
                        "products": ["Platform", "API", "Dashboard"],
                    },
                    "hint": (
                        "Domain-entity vocabulary. Key = domain category; value = "
                        "canonical names list."
                    ),
                },
            },
        },
        "domain_owners": {
            "kind": "map",
            "key_kind": "string",
            "value_kind": "array",
            "value_item_kind": "string",
            "hint": (
                "Per-domain admin emails. Key = domain name; value = email list."
            ),
        },
        "domains": {
            "kind": "array",
            "item_kind": "string",
            "default": [
                "finance",
                "engineering",
                "product",
                "data",
                "operations",
                "infrastructure",
            ],
            "hint": (
                "Knowledge domains analysts can target. Each must match a key "
                "in domain_owners."
            ),
        },
    },
    # materialize — file-lock TTL for the concurrent-materialize safety net.
    # A single field; more knobs may follow as the feature matures.
    "materialize": {
        "lock_ttl_seconds": {
            "kind": "int",
            "default": 86400,
            "hint": (
                "How long (seconds) before a stale materialize lock file is "
                "reclaimed. The lock is a .parquet.lock sibling file; if the "
                "holder process is hard-killed, the next attempt reclaims the "
                "lock once the file's mtime is older than this TTL. "
                "Default 86400 (24 h). Min 60, max 604800 (7 days). "
                "Lower only if you know materializes never exceed the new value "
                "and your host regularly hard-kills processes."
            ),
        },
    },
    "guardrails": {
        "min_description_chars": {
            "kind": "int",
            "default": 60,
            "hint": (
                "Minimum character floor for skill / agent / plugin "
                "descriptions on flea-market uploads (the inline content "
                "guardrail). Real-world Claude skill descriptions cluster "
                "150–220 chars; the default 60 is the bottom of the bar "
                "to catch placeholders. Bump to 100+ to push submitters "
                "closer to the ecosystem norm. Min 1."
            ),
        },
        "min_command_description_chars": {
            "kind": "int",
            "default": 25,
            "hint": (
                "Minimum character floor for slash-command descriptions. "
                "Tighter than skills because commands are one-verb "
                "actions (\"run tests\", \"format code\"). Default 25. Min 1."
            ),
        },
        "min_distinct_words": {
            "kind": "int",
            "default": 5,
            "hint": (
                "Minimum number of DISTINCT words in any description "
                "string. Defends against padding-only descriptions like "
                "\"description description description\" that hit the "
                "character count but say nothing. Default 5. Min 1."
            ),
        },
        "min_body_chars": {
            "kind": "int",
            "default": 200,
            "hint": (
                "Minimum body-content floor for skill / agent files "
                "(the markdown after the YAML frontmatter). Real skill "
                "bodies run 500–2000 chars; the default 200 is a "
                "\"one paragraph\" floor that catches stubs. Min 1."
            ),
        },
        "enabled": {
            "kind": "bool",
            "default": True,
            "hint": (
                "Master kill-switch for the LLM guardrail tier. When "
                "False (or when ANTHROPIC_API_KEY / LLM_API_KEY is "
                "absent), uploads still run the inline mechanical "
                "checks but skip the LLM security + content-quality "
                "review and auto-approve. Default True."
            ),
        },
        "review_model": {
            "kind": "select",
            "default": "haiku",
            "options": ["haiku", "sonnet", "opus"],
            "hint": (
                "Anthropic model tier for the LLM security + content "
                "review. Haiku is the cheapest and fastest; Sonnet / "
                "Opus catch subtler prompt-injection + vague descriptions "
                "at proportionally higher per-upload cost."
            ),
        },
        "blocked_quota_per_day": {
            "kind": "int",
            "default": 50,
            "hint": (
                "Per-submitter cap on `blocked_llm` + `review_error` "
                "rows in the trailing 24h. Bounds the worst case where "
                "a bot loops on bundles that survive inline checks but "
                "trip the async LLM reviewer. Inline failures are "
                "hard-rejected upstream (no row, not counted). 0 "
                "disables the quota. Default 50."
            ),
        },
        "blocked_bundle_ttl_days": {
            "kind": "int",
            "default": 30,
            "hint": (
                "How many days to keep a blocked bundle's bytes on disk. "
                "The submission row + sha256 + size always survive; only "
                "the bytes get removed. 0 disables the purge entirely. "
                "Default 30."
            ),
        },
        "stuck_review_grace_seconds": {
            "kind": "int",
            "default": 1800,
            "hint": (
                "How long a submission may stay at `status='pending_llm'` "
                "before the reaper flips it to `review_error`. Default "
                "1800 (30 min) comfortably exceeds Sonnet / Opus p99 "
                "wall time. 0 disables the reaper."
            ),
        },
    },
}

# Keys whose values must be redacted from the audit diff. We match
# substring (case-insensitive) so `client_secret`, `api_token`,
# `webapp_secret_key`, `bot_token`, `password`, `smtp_password`, etc. all
# get masked even when nested.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "api_key",
)


def _is_secret_key(key: str) -> bool:
    """True if a config key holds a credential and should be masked in audit logs."""
    k = key.lower()
    return any(pat in k for pat in _SECRET_KEY_PATTERNS)


def _mask(value: Any) -> str:
    """Replacement value used in the audit diff for secret fields.

    We deliberately do NOT preserve length or any hint about the secret —
    the diff is read by other admins, and there's no operator value to
    leaking "the new SMTP password is 16 chars". `***` is enough to show
    that the field changed without exposing it.
    """
    if value in (None, ""):
        return "<empty>"
    return "***"


# Sentinel values produced by `_mask`. Any patch leaf that arrives at a
# secret-keyed slot still bearing one of these strings means the caller
# round-tripped the GET payload (which redacts secret-keyed children inside
# nested objects) without changing the value — `_strip_redacted_sentinels`
# drops the leaf so deep-merge preserves whatever the overlay already had,
# rather than persisting the placeholder on top of the real secret.
_REDACTED_SENTINELS: frozenset = frozenset({"***", "<empty>"})


def _strip_redacted_sentinels(value: Any, key_hint: str = "") -> Any:
    """Recursively drop secret-keyed leaves whose value is a redaction sentinel.

    Symmetric with `_redact`: the GET handler masks secret-keyed children
    inside nested objects so the form never shows cleartext, and this
    function is the write-side counterpart that ensures the placeholder
    doesn't make a round-trip back into the overlay. Defense-in-depth
    alongside the client-side `scrubRedactedSecrets` in
    `admin_server_config.html` — an API caller (CLI / script) that forgets
    to scrub still can't corrupt secrets via this endpoint.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if _is_secret_key(k) and isinstance(v, str) and v in _REDACTED_SENTINELS:
                continue
            out[k] = _strip_redacted_sentinels(v, k)
        return out
    if isinstance(value, list):
        return [_strip_redacted_sentinels(item, key_hint) for item in value]
    return value


def _redact(value: Any, key_hint: str = "") -> Any:
    """Recursively mask secret-looking fields in a config subtree.

    `key_hint` is the parent key — used so a string value like
    ``"${KEBOOLA_TOKEN}"`` under ``token_env`` is masked even though the
    value itself isn't a credential, because the key signals it points at
    one.
    """
    if isinstance(value, dict):
        return {k: (_mask(v) if _is_secret_key(k) else _redact(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key_hint) for item in value]
    if key_hint and _is_secret_key(key_hint):
        return _mask(value)
    return value


def _diff_dicts(before: dict, after: dict, path: str = "") -> List[Dict[str, Any]]:
    """Flat list of changed fields between two dicts.

    Output: [{"path": "email.smtp_host", "before": "...", "after": "..."}].
    Diff is computed on RAW values, then each row's `before`/`after` is
    masked via `_mask` when the leaf key matches `_is_secret_key` — pre-
    masking the inputs would collapse a secret rotation (e.g. password A
    → password B) into "no diff" because both sides redact to ``"***"``,
    and the audit log would then silently fail to record one of the most
    security-relevant changes. Compare raw, redact when emitting.

    Recurses into a dict on either side (treating the missing side as
    `{}`) so adding a brand-new section reports per-field paths
    (`email.smtp_host`) rather than a single opaque `email` blob — that
    keeps the audit row useful when an admin populates a section for the
    first time.
    """
    changes: List[Dict[str, Any]] = []
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        new_path = f"{path}.{key}" if path else key
        b_val = before.get(key)
        a_val = after.get(key)
        b_is_dict = isinstance(b_val, dict)
        a_is_dict = isinstance(a_val, dict)
        # Dict-vs-dict (or dict-vs-None) → recurse for per-field paths.
        if b_is_dict and a_is_dict:
            changes.extend(_diff_dicts(b_val, a_val, new_path))
        elif b_is_dict and a_val is None:
            changes.extend(_diff_dicts(b_val, {}, new_path))
        elif a_is_dict and b_val is None:
            changes.extend(_diff_dicts({}, a_val, new_path))
        # Dict↔scalar shape change is recorded as a single replacement at
        # the parent path. Recursing with `{}` would lose the scalar side
        # entirely (admin sets `keboola: {…}` to `keboola: "disabled"` —
        # auditor would see members removed but never the new value).
        # The dict side may itself contain secret-keyed children (e.g.
        # `keboola: {token_env: ${KEBOOLA_TOKEN}}` resolved to cleartext);
        # `_redact` masks those children even when the parent key isn't
        # secret-named, so the audit log doesn't leak ${ENV_VAR}-resolved
        # values when a section is replaced wholesale.
        elif b_is_dict != a_is_dict:
            if _is_secret_key(key):
                changes.append({
                    "path": new_path,
                    "before": _mask(b_val),
                    "after": _mask(a_val),
                })
            else:
                changes.append({
                    "path": new_path,
                    "before": _redact(b_val, key) if b_is_dict else b_val,
                    "after": _redact(a_val, key) if a_is_dict else a_val,
                })
        elif b_val != a_val:
            if _is_secret_key(key):
                changes.append({
                    "path": new_path,
                    "before": _mask(b_val),
                    "after": _mask(a_val),
                })
            else:
                changes.append({"path": new_path, "before": b_val, "after": a_val})
    return changes


def _deep_merge(base: dict, patch: dict) -> dict:
    """Merge `patch` into `base` recursively, returning a new dict.

    Patch values overwrite base values. Dict-into-dict recurses; everything
    else (lists, scalars, None) is replaced wholesale — admin sets
    ``email: {smtp_port: 465}`` and we don't try to re-merge nested ports.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_current_instance_yaml() -> dict:
    """Return the editor's view of instance.yaml — deep-merge of static +
    overlay via ``app.instance_config.load_instance_config``.

    Readers (GET /server-config) hit the cache and trust that writers
    invalidate. Writers must call ``reset_cache()`` explicitly *before*
    the read so they see the latest disk state in the read-modify-write
    sequence. The shared helper is the authoritative source so the editor
    never sees a different view than the rest of the running app.
    """
    from app.instance_config import load_instance_config
    return load_instance_config()


def _public_view(config: dict) -> dict:
    """Return a config dict safe to render in the admin UI form.

    Deep-copies and redacts secret-looking fields so an admin can see
    *which* fields are populated without the cleartext leaking into the
    rendered HTML / browser DevTools.
    """
    import copy
    return _redact(copy.deepcopy(config))


class ServerConfigUpdateRequest(BaseModel):
    """Patch payload for POST /api/admin/server-config.

    Only the sections listed in `_EDITABLE_SECTIONS` are accepted; anything
    else is rejected with 400. `confirm_danger` must be true if the patch
    touches any danger-zone section (auth.*, server.*).
    """
    sections: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-section patch dict (e.g. {'instance': {'name': 'X'}})",
    )
    confirm_danger: bool = Field(
        default=False,
        description="Must be true to apply changes touching auth.* or server.*",
    )


# Optional BQ fields whose runtime defaults are documented but which used to
# be invisible in the editor when YAML omitted them. The data_source.bigquery
# subtree renders as a JSON textarea; a key that's absent from the GET
# payload literally cannot appear in the form for the operator to edit. We
# surface them with their documented defaults so the UI always shows them as
# editable knobs — see Phase J of the admin-tables-cleanup work.
#
#   - billing_project: defaults to data project; explicit value needed when
#     the SA can read the data project but not bill against it.
#   - max_bytes_per_materialize: cost guardrail for `query_mode='materialized'`
#     (default 10 GiB; 0 disables; null falls through to the default).
_BQ_OPTIONAL_FIELD_DEFAULTS: Dict[str, Any] = {
    # `billing_project` intentionally NOT seeded here. The empty-string
    # default would inject `billing_project: ""` into every GET payload,
    # which makes the JS `isUnset = (value === undefined)` check evaluate
    # False — and the `(defaults to <project>)` placeholder feature
    # (#160 §4.7.5) would never render. Leaving it absent keeps the
    # field in the unset rendering path so placeholder_from fires.
    # Devin Review iter #3 on PR #168.
    "max_bytes_per_materialize": 10737418240,
    "bq_max_scan_bytes": 5368709120,
}


def _ensure_bq_optional_fields(sections: Dict[str, Any]) -> None:
    """In-place: add missing BQ optional fields to data_source.bigquery so the
    UI's JSON-textarea renders them as editable keys. Existing values are
    preserved — only absent keys are populated with their documented default.
    """
    ds = sections.get("data_source")
    if not isinstance(ds, dict):
        return
    bq = ds.get("bigquery")
    if not isinstance(bq, dict):
        # No BQ subsection — leave alone. Non-BQ instances don't need these
        # knobs, and creating an empty bigquery dict would be misleading.
        return
    for key, default in _BQ_OPTIONAL_FIELD_DEFAULTS.items():
        bq.setdefault(key, default)


@router.get("/server-config")
async def get_server_config(
    user: dict = Depends(require_admin),
):
    """Return the current instance.yaml with secrets redacted.

    Used by the /admin/server-config UI to prefill its form. The redacted
    payload mirrors the actual file shape, so the UI doesn't need to know
    the schema — it iterates over the editable sections and renders the
    fields it finds. Empty sections still show in the response so the form
    knows to render their headers.
    """
    config = _load_current_instance_yaml()
    redacted = _public_view(config)
    # Surface every editable section so the UI renders them even when the
    # file omits them — operator can populate from scratch without manual
    # JSON edits.
    sections = {section: redacted.get(section, {}) for section in _EDITABLE_SECTIONS}
    # Always surface the optional BQ knobs so the operator sees them in the
    # UI's JSON editor instead of having to know they exist (Phase J).
    _ensure_bq_optional_fields(sections)
    return {
        "sections": sections,
        "editable_sections": list(_EDITABLE_SECTIONS),
        "danger_sections": list(_DANGER_SECTIONS),
        "secret_key_patterns": list(_SECRET_KEY_PATTERNS),
        # Known-but-optional fields per section so the UI can render
        # placeholders for fields the operator hasn't set yet (Phase J).
        # Subagents 2-4 populate the bodies; the renderer ships now so the
        # mechanism is wired end-to-end and adding entries is purely a
        # data-edit in `_KNOWN_FIELDS` above.
        "known_fields": _KNOWN_FIELDS,
    }


@router.post("/server-config")
async def update_server_config(
    request: ServerConfigUpdateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch instance.yaml from the /admin/server-config editor.

    Accepts a partial patch keyed by section. Validates sections, refuses
    danger-zone edits without explicit confirmation, deep-merges into the
    current overlay, writes the file, and emits one audit entry per save
    with a sanitized diff. Returns ``restart_required=true`` so the UI can
    show the restart banner — hot-reload is a separate issue (see #91 Out
    of scope).
    """
    import yaml

    if not request.sections:
        raise HTTPException(status_code=422, detail="sections cannot be empty")

    # Reject unknown sections loudly. Without this, a typo like "thmee"
    # would silently land in the YAML root and the operator wouldn't see
    # their colour change apply.
    unknown = sorted(set(request.sections.keys()) - set(_EDITABLE_SECTIONS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s): {', '.join(unknown)}. "
                   f"Editable: {', '.join(_EDITABLE_SECTIONS)}",
        )

    # Danger-zone gate. The UI shows a confirmation dialog before posting
    # with confirm_danger=true; an API caller (CLI/script) has to pass it
    # explicitly so they can't fat-finger a hostname change.
    danger_touched = sorted(set(request.sections.keys()) & set(_DANGER_SECTIONS))
    if danger_touched and not request.confirm_danger:
        raise HTTPException(
            status_code=400,
            detail=f"section(s) {', '.join(danger_touched)} require confirm_danger=true",
        )

    # SSRF protection — same gate the /configure wizard applies to
    # keboola_url, but here it covers any URL-bearing field reachable via
    # the per-section patch (e.g. data_source.keboola.stack_url).
    _validate_urls_in_patch(request.sections)

    # Field-level constraints for sections whose values have documented ranges.
    _validate_materialize_section(request.sections)

    # Defense-in-depth: scrub redaction sentinels (`***` / `<empty>`) out of
    # secret-keyed leaves in the patch before they reach the deep-merge.
    # The client form does the same scrub, but an API caller round-tripping
    # the GET payload could otherwise overwrite real overlay secrets with
    # the placeholder shown in the form.
    scrubbed_sections: Dict[str, Dict[str, Any]] = {
        section: _strip_redacted_sentinels(patch, section)
        for section, patch in request.sections.items()
    }

    # Serialize read-modify-write across concurrent admin saves. Without the
    # lock, two saves would each read the same overlay snapshot, merge their
    # disjoint patches, and the second os.replace would silently drop the
    # first patch. The lock spans the cache-invalidate → load → merge →
    # atomic-write sequence; the audit log sits outside since it operates on
    # local snapshots.
    from app.instance_config import reset_cache
    from app.secrets import _state_dir
    config_path = _state_dir() / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with _overlay_write_lock:
        # Drop the in-process cache so we read the latest on-disk state,
        # including any update that landed from a concurrent caller before
        # we acquired the lock.
        reset_cache()
        before = _load_current_instance_yaml()

        # Deep merge — section-by-section so we never accidentally delete a
        # sibling section the patch didn't touch. Use the redaction-scrubbed
        # patch so a round-tripped GET payload can't overwrite real secrets
        # with the `***` placeholder.
        after = dict(before)
        for section, patch in scrubbed_sections.items():
            if not isinstance(patch, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"section '{section}' must be an object, got {type(patch).__name__}",
                )
            if isinstance(after.get(section), dict):
                after[section] = _deep_merge(after[section], patch)
            else:
                after[section] = patch

        # Write only the sections the user actually patched in this request.
        # Two reasons:
        #   1. Persisting the full merged config (or every editable section)
        #      would snapshot non-editable static sections into the overlay,
        #      shadowing later operator updates to those sections in the
        #      static file (`_load_current_instance_yaml` merges static + overlay,
        #      overlay wins per leaf).
        #   2. The merged config has `${ENV_VAR}` placeholders RESOLVED to the
        #      runtime values by config.loader. Writing every editable section
        #      back would persist real cleartext secrets where the static file
        #      had only env-var references — turning `smtp_password:
        #      ${SMTP_PASSWORD}` into `smtp_password: hunter2` in the overlay.
        # By writing only the sections in `request.sections` we keep both the
        # static-evolution and the env-var-placeholder properties intact.
        overlay_payload: Dict[str, Any] = {}
        if config_path.exists():
            try:
                overlay_payload = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                # A corrupt overlay used to be silently replaced — that masked
                # disk corruption / partial writes / hand-edits and dropped
                # every previously-saved section on the next save. Refuse and
                # surface so the operator can investigate.
                logger.exception("server-config: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                           "back up and remove the file, or fix it by hand",
                ) from e
        for section, patch in scrubbed_sections.items():
            if section not in _EDITABLE_SECTIONS:
                continue
            # Deep-merge the patch into the existing overlay slot (or static-
            # backed `before` if overlay had nothing for this section). This
            # preserves any unrelated keys the operator didn't touch in this
            # request — e.g. patching `email.smtp_host` doesn't blow away the
            # `email.smtp_password: ${SMTP_PASSWORD}` reference.
            existing = overlay_payload.get(section)
            if not isinstance(existing, dict):
                existing = {}
            overlay_payload[section] = _deep_merge(existing, patch)

        # Atomic via tmp + os.replace so two concurrent admin saves can't
        # interleave bytes and produce corrupt YAML (especially harmful since
        # auth.* is editable here — half-written file → operator lockout).
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False))
        os.replace(tmp_path, config_path)
        logger.info("server-config: wrote %d section(s) to %s",
                    len(request.sections), config_path)

        # Invalidate cached instance config so subsequent reads pick up the
        # change. Hot-reload of running modules (auth providers, SMTP client)
        # is out of scope — the restart banner tells the operator to bounce.
        reset_cache()

    # Audit entry — diff is computed on RAW values then `_diff_dicts`
    # redacts each row whose leaf key matches `_is_secret_key`. Pre-
    # masking the inputs would collapse a secret rotation into "no
    # diff" because both sides redact to ``***``, hiding the most
    # security-relevant changes from the audit log. We log even if no
    # fields changed so the operator's intent (touched the page, hit
    # save) is auditable.
    diff = _diff_dicts(before, after)
    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="instance_config.update",
        resource="instance.yaml",
        params={
            "sections": sorted(request.sections.keys()),
            "danger_sections": danger_touched,
            "diff": diff,
            "diff_count": len(diff),
        },
    )

    return {
        "status": "ok",
        "restart_required": True,
        "sections_updated": sorted(request.sections.keys()),
        "diff_count": len(diff),
    }


# --- End server-config editor -----------------------------------------------


# Source types accepted by /api/admin/register-table. Anything else is
# rejected with 422 — keeps a typo'd source_type from silently landing in
# table_registry (where it would later confuse the orchestrator scan).
_VALID_SOURCE_TYPES: tuple[str, ...] = ("keboola", "bigquery", "jira", "local")

# Explicit allowlist of audit-payload keys whose values are credentials and
# must be masked. Substring-scan + ad-hoc whitelist (the previous shape) is
# fragile in two ways:
#   1. False positive: legit fields like `primary_key` get masked because
#      they contain "key" — we then need a whitelist exception, which has
#      to be kept in sync as new fields are added.
#   2. False negative: a future field like `primary_key_hash` *would* be
#      masked (defensible) but `not_actually_a_token` ALSO matches "token"
#      and gets masked unnecessarily; conversely, a brand-new credential
#      field that doesn't contain one of the patterns (`auth_material`,
#      `bearer`) silently leaks.
# Allowlist puts the burden on the developer adding a new secret-bearing
# field: they must add the literal key name here, which forces a code-
# review touch on the audit path. Audit the current Pydantic models
# (RegisterTableRequest / UpdateTableRequest / ConfigureRequest /
# ServerConfigUpdateRequest) when extending — the registry payloads don't
# currently carry credentials, but ConfigureRequest does (`keboola_token`)
# and could be routed through this sanitizer in the future.
_SECRET_FIELDS: frozenset = frozenset({
    # ConfigureRequest — POST /api/admin/configure carries Keboola creds.
    "keboola_token",
    # Generic names that have appeared in earlier iterations of admin
    # request bodies and could resurface — keep them masked defensively.
    "api_token",
    "auth_token",
    "bot_token",
    "client_secret",
    "google_client_secret",
    "google_oauth_client_secret",
    "password",
    "smtp_password",
    "webapp_secret_key",
    "bot_secret",
    # Marketplace PATs (private repos) — see src/marketplace.py.
    "marketplace_token",
    "marketplace_pat",
})


def _sanitize_for_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mask credential-bearing fields in a request payload before audit_log.

    Uses an explicit `_SECRET_FIELDS` allowlist (case-insensitive) instead
    of substring matching. The trade-off is that adding a new secret field
    requires updating the set — but that's the *point*: the test suite
    asserts `not_actually_a_token` does NOT get masked, so a substring-
    based regression would surface immediately, and a missing entry for a
    real new credential gets caught at code review of the audit path.
    """
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in _SECRET_FIELDS:
            out[k] = "***" if v not in (None, "") else "<empty>"
        else:
            out[k] = v
    return out


# Both the BigQuery and Keboola materialize paths funnel `source_query`
# through DuckDB (BQ via the bigquery extension's COPY translation, Keboola
# via an ATTACH'd extension and a direct COPY). DuckDB uses double quotes
# for quoted identifiers — backticks are a BigQuery-native syntactic form
# DuckDB's parser does not honor, so a backtick-quoted source_query either
# parse-errors at COPY time or silently scans nothing. Surfaced from the
# field validator on RegisterTableRequest AND the merged-record path in
# `update_table` so neither route can persist a backtick query.
_BACKTICK_REJECTION_MESSAGE = (
    "source_query uses BigQuery-native backtick identifiers (e.g. "
    "`project.dataset.table`), but the materialize path runs the SQL "
    "through DuckDB's BigQuery extension which uses DuckDB-flavor "
    "identifiers. Rewrite to DuckDB syntax: bq.\"dataset\".\"table\" "
    "(with the attached catalog alias `bq` plus double-quoted dataset/"
    "table). The instance is configured with the data project, so you "
    "don't need to repeat it in the FROM clause."
)


class RegisterTableRequest(BaseModel):
    name: str
    folder: Optional[str] = None
    sync_strategy: str = Field(
        default="full_refresh",
        description=(
            "Per-table extraction strategy. v26+: drives the Keboola "
            "extractor's dispatcher in connectors/keboola/extractor.py. "
            "Allowed values: 'full_refresh' (default; full table dump on "
            "each sync), 'incremental' (Storage API changedSince + "
            "primary-key dedup merge), 'partitioned' (per-partition "
            "parquet files keyed by partition_by column, per-partition "
            "merge for daily updates, chunked initial load). "
            "Pre-v26 this field was inert; existing rows default to "
            "'full_refresh' so behavior is unchanged unless an admin "
            "opts a table in to incremental/partitioned."
        ),
    )
    # Composite primary keys are real (session-grain MSA tables key on
    # `(session_id, event_date)`, browse rows on more). The frontend sends +
    # reads this as a list; backend stores it JSON-serialized in VARCHAR.
    # A bare string is accepted for backward compat — see _normalize_primary_key.
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    # Backs query_mode='materialized'. Stored verbatim in
    # table_registry.source_query (schema v20); the trigger pass runs it
    # through the DuckDB BQ extension via BqAccess and writes the result
    # to /data/extracts/bigquery/data/<id>.parquet.
    source_query: Optional[str] = None
    query_mode: str = "local"
    sync_schedule: Optional[str] = None
    profile_after_sync: bool = Field(
        default=True,
        deprecated=True,
        description=(
            "DEPRECATED: not consumed by the runtime (Agent 1 finding "
            "2026-05-01). Profiler runs unconditionally on every synced "
            "table; this flag has no effect. Field stays for back-compat."
        ),
    )
    # v26 — Keboola sync-strategy support fields. All optional; meaningful
    # only when paired with the matching sync_strategy. Per-strategy
    # required-field rules + conflict policy enforced in the model_validator
    # below.
    incremental_window_days: Optional[int] = None
    max_history_days: Optional[int] = None
    incremental_column: Optional[str] = None
    where_filters: Optional[List[Dict[str, Any]]] = None
    partition_by: Optional[str] = None
    partition_granularity: Optional[str] = None
    initial_load_chunk_days: Optional[int] = None

    @model_validator(mode="after")
    def _check_mode_query_coherence(self):
        """Enforce query_mode ↔ source_query invariants up front so an admin
        can't persist a remote/local row carrying an orphan source_query.

        For BigQuery materialized rows, an empty source_query is allowed here
        because _validate_bigquery_register_payload generates it from
        bucket+source_table after this validator runs. For all other source
        types (e.g. Keboola), source_query is still required for materialized.
        """
        sq = (self.source_query or "").strip() or None
        if self.query_mode != "materialized" and sq:
            raise ValueError(
                "source_query is only valid when query_mode='materialized'"
            )
        # BigQuery materialized auto-generates a full-table-dump SQL from
        # `bucket`+`source_table` when source_query is omitted (see
        # `register_table` BQ branch). Keboola materialized: a NULL
        # source_query means "full-table export via Storage API
        # export-async" — no SQL needed (the API takes a structured
        # filter, see `connectors/keboola/storage_api.py:ExportFilter`).
        # Other source_types (e.g. jira) don't support materialized mode
        # and require an explicit source_query if the operator opts in.
        if (
            self.query_mode == "materialized"
            and not sq
            and self.source_type not in ("bigquery", "keboola")
        ):
            raise ValueError(
                f"query_mode='materialized' for source_type='{self.source_type}' "
                "requires a non-empty source_query"
            )
        # Backtick guard stays for non-materialized rows (DuckDB-flavor SQL
        # contract); materialized SQL is BigQuery-native and MUST allow
        # backticks for dashed identifiers (e.g. `prj-org.dataset.table`).
        if self.query_mode != "materialized" and sq and "`" in sq:
            raise ValueError(_BACKTICK_REJECTION_MESSAGE)
        # Normalise: stash the trimmed-or-None form so the persisted column
        # never carries surrounding whitespace or empty-string sentinels.
        self.source_query = sq
        return self

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v):
        # Defensive normalization for descriptions arriving via shell-quoting
        # tooling that injects literal backslash escapes (e.g. `Don\'t`, `\n`).
        return _unescape_shell_quoting(v)

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, v):
        # None is tolerated for backward compat with old CLI scripts that
        # didn't set a source_type; the route resolves it later. Anything
        # else must be in the canonical list.
        if v in (None, ""):
            return v
        if v not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}, got {v!r}"
            )
        return v

    @field_validator("sync_schedule", mode="before")
    @classmethod
    def _validate_sync_schedule(cls, v):
        # None / "" → no schedule, accepted.
        # Any non-empty string (including pure whitespace) must parse as a
        # valid schedule — otherwise it would be persisted and silently
        # ignored by the runtime evaluator.
        if v in (None, ""):
            return v
        if not is_valid_schedule(v):
            raise ValueError(
                f"sync_schedule must be 'every Nm' / 'every Nh' / "
                f"'daily HH:MM[,HH:MM,...]', got {v!r}"
            )
        return v

    @field_validator("sync_strategy", mode="before")
    @classmethod
    def _validate_sync_strategy(cls, v):
        """v26: enforce the strategy enum. NULL/empty → 'full_refresh' default.

        Pre-v26 the column accepted any string (catalog/profiler metadata
        only). Now the extractor dispatches off this value, so unknown
        strings would silently fall through to the default branch and
        confuse operators.
        """
        if v in (None, ""):
            return "full_refresh"
        allowed = {"full_refresh", "incremental", "partitioned"}
        if v not in allowed:
            raise ValueError(
                f"sync_strategy must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("partition_granularity", mode="before")
    @classmethod
    def _validate_partition_granularity(cls, v):
        if v in (None, ""):
            return v
        allowed = {"day", "month", "year"}
        if v not in allowed:
            raise ValueError(
                f"partition_granularity must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("where_filters", mode="before")
    @classmethod
    def _validate_where_filters(cls, v):
        """Validate filter shape via parse_filters from the keboola module.

        Accepts None / empty list, a JSON string, or a pre-parsed list.
        Returns the canonical list-of-dicts form for storage. Raises
        ValueError(InvalidFilterError message) on malformed shape so
        FastAPI returns 422 with a useful body. Placeholders are NOT
        resolved here — they're resolved at sync time so a misspelled
        token is caught when the next sync runs (admin can register a
        rolling-window filter today and the sync next month uses the
        same filter shape with a fresh date)."""
        if v in (None, "", []):
            return None
        from connectors.keboola.where_filters import parse_filters, InvalidFilterError
        try:
            return parse_filters(v)
        except InvalidFilterError as e:
            raise ValueError(str(e))

    @model_validator(mode="after")
    def _check_strategy_invariants(self):
        """v27 conflict policy + per-strategy required-field rules.

        Reject combinations that are silently broken at the extractor
        layer rather than letting the row land in the registry and
        confuse operators when the next sync misbehaves.

        - partitioned ⇒ partition_by required, query_mode='local' only.
          partition_granularity defaults to 'month' if omitted.
        - incremental + where_filters → 400. changedSince already does
          temporal filtering; layering server-side row filters on top is
          not supported by the extractor (legacy repo silently drops
          filters in this combination — match the rejection here).
        - partitioned + where_filters → 400. extract_partitioned does
          not thread where_filters through to its chunked downloads;
          accepting the pair would persist a filter that gets silently
          ignored at sync time (Devin Review concern). Reject explicitly
          until threading lands.
        - query_mode='remote' + where_filters → 400. _extract_via_extension
          (the remote/extension path) doesn't take a filters argument;
          accepting would silently drop them.
        """
        if self.sync_strategy == "partitioned":
            if not self.partition_by:
                raise ValueError(
                    "sync_strategy='partitioned' requires partition_by to be set"
                )
            if self.query_mode == "remote":
                raise ValueError(
                    "sync_strategy='partitioned' is incompatible with query_mode='remote' "
                    "— partitioned writes per-partition parquet files locally"
                )
            if self.where_filters:
                raise ValueError(
                    "sync_strategy='partitioned' is incompatible with where_filters "
                    "in v27 — extract_partitioned does not thread where_filters "
                    "through its chunked downloads; the filter would be silently "
                    "ignored. Use 'full_refresh' for filter+full-overwrite, or "
                    "wait for partitioned + where_filters wiring in a future PR."
                )
            if not self.partition_granularity:
                self.partition_granularity = "month"

        if self.sync_strategy == "incremental" and self.where_filters:
            raise ValueError(
                "sync_strategy='incremental' is incompatible with where_filters "
                "— changedSince already filters temporally; layering whereFilters "
                "on top is silently dropped by the extractor (use 'full_refresh' "
                "for filter+full-overwrite)"
            )

        # query_mode='remote' + where_filters: the DuckDB Keboola extension
        # path does not consume whereFilters. Accepting would silently drop
        # them at sync time. Caller must use query_mode='local' (Direct
        # extract) to apply filters.
        if self.query_mode == "remote" and self.where_filters:
            raise ValueError(
                "query_mode='remote' is incompatible with where_filters "
                "— the DuckDB Keboola extension does not expose whereFilters. "
                "Use query_mode='local' (Direct extract) to apply server-side "
                "row filters."
            )

        return self


def _generate_materialized_source_query(
    bucket: str, source_table: str, project_id: str,
) -> str:
    """Build the canonical full-table-dump source_query for a materialized
    BQ row when admin only supplies dataset + table. The result is
    BigQuery-native SQL — wrapped at materialize time into
    bigquery_query(...) by connectors.bigquery.extractor.materialize_query."""
    if not _is_safe_quoted_identifier(bucket):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: dataset {bucket!r} is unsafe",
        )
    if not _is_safe_quoted_identifier(source_table):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: source_table {source_table!r} is unsafe",
        )
    if not _is_safe_project_id(project_id):
        raise HTTPException(
            status_code=400,
            detail=f"bigquery: data_source.bigquery.project {project_id!r} is malformed",
        )
    return f"SELECT * FROM `{project_id}.{bucket}.{source_table}`"


def _validate_bigquery_register_payload(req: "RegisterTableRequest") -> None:
    """Enforce BQ-specific shape on a register/precheck request.

    Two BQ paths:

    - ``query_mode='materialized'`` — admin-registered SQL writes a parquet on
      schedule. Requires ``source_query``; ``bucket`` / ``source_table`` are
      not used (the SQL inlines the references). Doesn't force any field; the
      Pydantic ``model_validator`` already gated the query/mode coherence.

    - ``query_mode='remote'`` (or default) — remote view over a single BQ
      table. Requires ``bucket`` (BQ dataset) + ``source_table``. Mutates
      the model: forces ``query_mode='remote'`` and ``profile_after_sync=False``
      (per Decision 7 in #108) so a caller can't accidentally enqueue a
      parquet profiling pass for a remote view that has no local file.

    Raises HTTPException(422) for missing required fields and
    HTTPException(400) for unsafe identifiers / bogus project_id.
    """
    if req.query_mode == "materialized":
        # Materialized BQ rows: the SQL body replaces dataset+table refs.
        # source_query may be empty if admin supplied bucket+source_table —
        # in that case the server generates a full-table-dump SQL below.
        raw_name = req.name or ""
        if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bigquery: view name {raw_name!r} is unsafe — must match "
                    f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                    "with no leading/trailing whitespace"
                ),
            )
        from app.instance_config import get_value
        project_id = get_value("data_source", "bigquery", "project", default="") or ""
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "bigquery: data_source.bigquery.project is not set in "
                    "instance.yaml; configure it via /admin/server-config or "
                    "/api/admin/configure first"
                ),
            )
        if not _is_safe_project_id(project_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bigquery: data_source.bigquery.project {project_id!r} "
                    "is malformed — must match GCP project_id grammar "
                    "^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
                ),
            )

        if not (req.source_query and req.source_query.strip()):
            # Server-generate from bucket+source_table. Trivial full-table
            # dump path; admin only sets dataset+table and the server
            # builds BQ-native SQL from instance.yaml's configured project.
            if not (req.bucket and req.source_table):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "bigquery materialized requires either source_query "
                        "(custom SQL) or bucket+source_table (server-generates "
                        "the full-table-dump SQL)"
                    ),
                )
            req.source_query = _generate_materialized_source_query(
                req.bucket, req.source_table, project_id,
            )

        # Phase C: profile_after_sync is now inert (Pydantic field marked
        # deprecated; not read by app/api/sync.py:410-438). The runtime
        # profiles every synced table unconditionally, so we no longer
        # force-set this here as a "signal."
        return

    if not req.bucket or not req.bucket.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'bucket' (BQ dataset) is required",
        )
    if not req.source_table or not req.source_table.strip():
        raise HTTPException(
            status_code=422,
            detail="bigquery: 'source_table' is required",
        )
    # No wildcard / sharded BQ tables in M1 (Decision 8).
    if "*" in (req.source_table or "") or "*" in (req.bucket or ""):
        raise HTTPException(
            status_code=400,
            detail="bigquery: wildcard / sharded tables are not supported (see #108 M3+)",
        )
    # Strict identifier on the DuckDB view name. CRITICAL: validate the RAW
    # name (the value that ``register_table`` actually persists to
    # ``table_registry.name`` and which the BQ extractor reads back as the
    # DuckDB view name at next rebuild). Earlier revisions normalized first
    # (``strip().lower().replace(" ", "_")``) and then checked, which let
    # names like ``"my table"`` pass here, get stored verbatim, and then
    # blow up inside ``_init_extract`` at view-create time — defeating the
    # whole point of fast-fail-at-register. We do NOT silently rewrite the
    # operator's name; if they typed ``"my table"``, return 400 with a
    # clear message and let them retype with a corrected name.
    raw_name = req.name or ""
    if raw_name.strip() != raw_name or not _is_safe_identifier(raw_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: view name {raw_name!r} is unsafe — must match "
                f"^[a-zA-Z_][a-zA-Z0-9_]{{0,63}}$ (DuckDB identifier rules) "
                "with no leading/trailing whitespace"
            ),
        )
    # Same fast-fail rule as ``raw_name`` above: validate the RAW value the
    # caller sent, not a stripped form. ``register_table`` persists ``bucket``
    # / ``source_table`` verbatim, and the BQ extractor splices them straight
    # into the ``ATTACH … AS bq_<bucket>`` and view DDL at next rebuild — so a
    # value with leading/trailing whitespace passes validation here, gets
    # stored as-is, and explodes inside DuckDB at view-create time. Surface
    # the offending raw value in the 400 detail and let the operator retype.
    raw_bucket = req.bucket
    if raw_bucket.strip() != raw_bucket or not _is_safe_quoted_identifier(raw_bucket):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: dataset {raw_bucket!r} is unsafe (only [A-Za-z0-9_.-] "
                "allowed, no leading/trailing whitespace)"
            ),
        )
    raw_source_table = req.source_table
    if raw_source_table.strip() != raw_source_table or not _is_safe_quoted_identifier(raw_source_table):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: source_table {raw_source_table!r} is unsafe (only "
                "[A-Za-z0-9_.-] allowed, no leading/trailing whitespace)"
            ),
        )
    # Pull project from instance.yaml — single-project model in M1
    # (Decision: no per-table project field). Validate the format here so
    # we surface a config issue at registration rather than at first
    # rebuild, where the operator no longer has a request to look at.
    from app.instance_config import get_value
    project_id = get_value("data_source", "bigquery", "project", default="")
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "bigquery: data_source.bigquery.project is not set in instance.yaml; "
                "configure it via /admin/server-config or /api/admin/configure first"
            ),
        )
    if not _is_safe_project_id(project_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"bigquery: data_source.bigquery.project {project_id!r} is malformed — "
                "must match GCP project_id grammar ^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
            ),
        )
    # Force the BQ-required mode (Decision 7). The orchestrator and
    # extractor both assume remote; persisting `local` here would later create
    # a profiling job against a non-existent parquet file.
    # Phase C: profile_after_sync is now inert (deprecated, not read by the
    # runtime); no longer force-set here.
    req.query_mode = "remote"


# Source types that don't depend on a `data_source.<name>.*` block — they
# get their data through a different ingestion path (e.g. Jira via
# webhooks). Registrations against these types are allowed regardless of
# the configured primary `data_source.type`.
_SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE: frozenset[str] = frozenset({
    "jira",
    "local",
})


def _validate_source_type_configured(source_type: Optional[str]) -> None:
    """Refuse register-table requests whose ``source_type`` isn't actually
    configured on this instance.

    Pre-fix the route happily persisted e.g. ``source_type='keboola'`` on a
    BQ-only instance — the row landed in the registry but the scheduler had
    no Keboola URL/token to ATTACH against, so it silently never synced.
    No upfront error, no operator-visible signal until they noticed the
    table was missing from `agnes catalog`.

    A source_type is considered configured when:

    - it matches the instance's primary ``data_source.type``, OR
    - a non-empty ``data_source.<source_type>`` block exists in the
      effective `instance.yaml` (multi-source instances), OR
    - it's in the small allowlist of types that don't sit under
      `data_source.*` at all (Jira, local — see
      ``_SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE``).

    Special case: when the configured primary is ``'local'`` (the default
    when an instance is freshly bootstrapped and no `data_source.type` has
    been set yet), the validator stays permissive — refusing registrations
    here would block the first-time-setup workflow where the operator
    registers a few tables against a not-yet-fully-configured instance.
    The misconfiguration that this validator targets is the *explicit
    mismatch*: `type=bigquery` instance + `source_type=keboola` payload
    with no `data_source.keboola.*` block. That case still 422s.

    A bare/None source_type is tolerated for backward compat with legacy
    CLI scripts; the route resolves it later against
    ``get_data_source_type()``.
    """
    if not source_type:
        return
    if source_type in _SOURCE_TYPES_INDEPENDENT_OF_DATA_SOURCE:
        return

    from app.instance_config import get_data_source_type, get_value

    configured_primary = get_data_source_type()
    if source_type == configured_primary:
        return

    # Multi-source: accept if a non-empty `data_source.<source_type>` block
    # exists. Empty dict / None / "" all count as "not configured".
    secondary_block = get_value("data_source", source_type, default=None)
    if secondary_block:
        # Truthy non-empty dict / mapping / scalar — treat as configured.
        return

    # Bootstrap-friendliness: a primary of 'local' means the instance hasn't
    # been pointed at a real source yet (or has been deliberately set to
    # local-only). Don't gate registrations in that state — the operator is
    # likely in the middle of first-time setup and will fill in the config
    # next. The check still fires when primary is an actual source type
    # (bigquery / keboola) and the requested source_type doesn't match
    # AND has no secondary block.
    if configured_primary == "local":
        return

    raise HTTPException(
        status_code=422,
        detail=(
            f"source_type={source_type!r} is not configured on this instance. "
            f"The configured data source is {configured_primary!r}. To enable "
            f"a secondary source, set data_source.{source_type}.* fields in "
            "instance.yaml or via /admin/server-config."
        ),
    )


class UpdateTableRequest(BaseModel):
    name: Optional[str] = None
    sync_strategy: Optional[str] = Field(
        default=None,
        description=(
            "v26+: drives the Keboola extractor dispatcher. PUT-shape "
            "requires a value if sent. See RegisterTableRequest.sync_strategy."
        ),
    )
    primary_key: Optional[List[str]] = None
    description: Optional[str] = None
    source_type: Optional[str] = None
    bucket: Optional[str] = None
    source_table: Optional[str] = None
    source_query: Optional[str] = None
    query_mode: Optional[str] = None
    sync_schedule: Optional[str] = None
    profile_after_sync: Optional[bool] = Field(
        default=None,
        deprecated=True,
        description=(
            "DEPRECATED: not consumed by the runtime. See "
            "RegisterTableRequest.profile_after_sync."
        ),
    )
    # v26 — same fields as RegisterTableRequest, all optional. The PUT
    # handler overlays the body on the existing row and re-runs the
    # synthetic RegisterTableRequest validator on the merged record, so
    # cross-field invariants are checked against the post-update state.
    incremental_window_days: Optional[int] = None
    max_history_days: Optional[int] = None
    incremental_column: Optional[str] = None
    where_filters: Optional[List[Dict[str, Any]]] = None
    partition_by: Optional[str] = None
    partition_granularity: Optional[str] = None
    initial_load_chunk_days: Optional[int] = None

    @field_validator("sync_strategy", mode="before")
    @classmethod
    def _validate_sync_strategy(cls, v):
        if v in (None, ""):
            return v
        allowed = {"full_refresh", "incremental", "partitioned"}
        if v not in allowed:
            raise ValueError(
                f"sync_strategy must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("partition_granularity", mode="before")
    @classmethod
    def _validate_partition_granularity(cls, v):
        if v in (None, ""):
            return v
        allowed = {"day", "month", "year"}
        if v not in allowed:
            raise ValueError(
                f"partition_granularity must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("where_filters", mode="before")
    @classmethod
    def _validate_where_filters(cls, v):
        if v in (None, "", []):
            return None
        from connectors.keboola.where_filters import parse_filters, InvalidFilterError
        try:
            return parse_filters(v)
        except InvalidFilterError as e:
            raise ValueError(str(e))

    @model_validator(mode="after")
    def _check_mode_query_coherence(self):
        """PUT semantics — only the fields explicitly in the body are
        validated. The body is overlaid on the existing row at the handler
        level (see ``update_table``), so omitted fields keep their stored
        values and the synthetic ``RegisterTableRequest`` constructed against
        the merged record runs the strict cross-field check before persist.

        The only invariants enforceable from the PUT body alone:

        - explicit ``source_query='SELECT ...'`` paired with ``query_mode``
          that isn't materialized → coherent reject (the SQL would be dead);
        - explicit ``source_query='SELECT ...'`` without any ``query_mode``
          in the body → reject; the operator must commit to materialized;
        - explicit empty/whitespace ``source_query=''`` paired with
          ``query_mode='materialized'`` → reject (operator clearly
          mistyped — they sent the field).

        Pre-fix this validator also rejected ``{"query_mode": "materialized",
        "sync_schedule": "every 12h"}`` because ``source_query`` was None
        — but that's the canonical "edit the schedule on a materialized
        row" use-case from the Edit modal, which always sends
        ``query_mode`` to indicate intent. Devin BUG_0002 on PR #148
        commit 2219255.
        """
        if self.query_mode is None and self.source_query is None:
            return self

        sq_raw = self.source_query
        sq = (sq_raw or "").strip() or None

        # Operator explicitly sent source_query as empty/whitespace while
        # claiming materialized — typo / bad form data, reject.
        if (
            self.query_mode == "materialized"
            and sq_raw is not None
            and not sq
        ):
            raise ValueError(
                "query_mode='materialized' requires a non-empty source_query"
            )

        # source_query only makes sense with materialized mode. Allow None
        # (omitted) to flow through; only reject when explicitly set with
        # the wrong mode.
        if (
            self.query_mode is not None
            and self.query_mode != "materialized"
            and sq
        ):
            raise ValueError(
                "source_query is only valid when query_mode='materialized'"
            )
        if self.query_mode is None and sq:
            raise ValueError(
                "source_query requires query_mode='materialized' to be set "
                "in the same request"
            )

        # Normalise: drop whitespace-only strings to None so the persisted
        # column is clean. Don't touch when source_query was None to begin
        # with — that signals "PUT didn't touch this field, keep existing".
        if sq_raw is not None:
            self.source_query = sq
        return self

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, v):
        return _normalize_primary_key(v)

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v):
        # Defensive normalization for descriptions arriving via shell-quoting
        # tooling that injects literal backslash escapes (e.g. `Don\'t`, `\n`).
        return _unescape_shell_quoting(v)

    # Duplicated from RegisterTableRequest — Pydantic v2 validators don't
    # inherit cleanly across unrelated BaseModel classes; a shared mixin
    # would be overkill for two fields.
    @field_validator("sync_schedule", mode="before")
    @classmethod
    def _validate_sync_schedule(cls, v):
        # None / "" → no schedule, accepted.
        # Any non-empty string (including pure whitespace) must parse as a
        # valid schedule — otherwise it would be persisted and silently
        # ignored by the runtime evaluator.
        if v in (None, ""):
            return v
        if not is_valid_schedule(v):
            raise ValueError(
                f"sync_schedule must be 'every Nm' / 'every Nh' / "
                f"'daily HH:MM[,HH:MM,...]', got {v!r}"
            )
        return v


class ConfigureRequest(BaseModel):
    data_source: str  # "keboola" | "bigquery" | "local"
    keboola_token: Optional[str] = None
    keboola_url: Optional[str] = None
    bigquery_project: Optional[str] = None
    bigquery_location: Optional[str] = None
    instance_name: Optional[str] = None
    allowed_domain: Optional[str] = None


@router.get("/discover-tables")
async def discover_tables(
    user: dict = Depends(require_admin),
    dataset: Optional[str] = None,
):
    """Discover available tables from the configured data source.

    For ``data_source.type='keboola'`` returns the full Storage API table
    list (single round-trip). For ``data_source.type='bigquery'``:

    - Without ``dataset``: list datasets in the configured project.
    - With ``dataset=name``: list tables (BASE TABLE + VIEW) in that dataset.

    Two-step shape avoids paying the per-dataset list_tables cost up-front
    on projects with hundreds of datasets — the UI populates the dataset
    dropdown first, then fetches tables only for the selected dataset.
    """
    try:
        from app.instance_config import get_data_source_type
        source_type = get_data_source_type()

        if source_type == "keboola":
            from connectors.keboola.client import KeboolaClient
            from app.instance_config import get_value
            url = get_value("data_source", "keboola", "stack_url", default="")
            token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
            token = os.environ.get(token_env, "") if token_env else ""
            if not token:
                token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
            client = KeboolaClient(token=token, url=url)
            tables = client.discover_all_tables()
            return {"tables": tables, "count": len(tables), "source": "keboola"}

        if source_type == "bigquery":
            return _discover_bigquery(dataset=dataset)

        return {
            "tables": [],
            "count": 0,
            "source": source_type,
            "error": f"Discovery not implemented for source_type={source_type!r}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e}")


def _discover_bigquery(dataset: Optional[str]) -> Dict[str, Any]:
    """List BQ datasets (when ``dataset`` is None) or tables-in-dataset.

    Routes through ``BqAccess.client()`` so config / auth / error
    translation matches the rest of the BQ surface (#138 facade). Returns
    the same shape as the Keboola path so the UI doesn't have to branch.
    """
    from connectors.bigquery.access import (
        get_bq_access,
        BqAccessError,
        translate_bq_error,
    )

    try:
        bq = get_bq_access()
        client = bq.client()
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.message, "kind": e.kind, "details": e.details},
        )

    try:
        if dataset is None:
            datasets = []
            for ds in client.list_datasets():
                datasets.append({
                    "dataset_id": ds.dataset_id,
                    "full_id": f"{ds.project}.{ds.dataset_id}",
                })
            return {
                "datasets": sorted(datasets, key=lambda d: d["dataset_id"]),
                "count": len(datasets),
                "source": "bigquery",
            }

        # List tables in the named dataset. `list_tables` returns
        # `TableListItem` with `table_id` + `table_type` ('TABLE', 'VIEW',
        # 'MATERIALIZED_VIEW', 'EXTERNAL', 'SNAPSHOT'). UI maps TABLE → Type
        # selector "table" and VIEW/MATERIALIZED_VIEW → "view"; the rest are
        # passed through with their raw type so the operator can decide.
        tables = []
        for t in client.list_tables(dataset):
            tables.append({
                "table_id": t.table_id,
                "table_type": t.table_type,
                "full_id": f"{t.project}.{t.dataset_id}.{t.table_id}",
            })
        return {
            "tables": sorted(tables, key=lambda t: t["table_id"]),
            "count": len(tables),
            "source": "bigquery",
            "dataset": dataset,
        }
    except Exception as e:
        # `translate_bq_error` re-raises non-Google exceptions unchanged,
        # so wrap in HTTPException to keep the JSON-shape contract.
        try:
            err = translate_bq_error(e, bq.projects, bad_request_status="upstream_error")
        except Exception:
            raise HTTPException(status_code=502, detail=f"BQ discovery failed: {e}")
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(err.kind, 502),
            detail={"error": err.message, "kind": err.kind, "details": err.details},
        )


@router.get("/registry")
async def list_registry(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get full table registry.

    Each table row is enriched with `last_sync_error` from sync_state so
    operators can see WHY a row isn't materializing without trawling
    scheduler logs. None for rows that have never errored or have already
    recovered (status='ok'); the per-row error message string otherwise.
    """
    repo = TableRegistryRepository(conn)
    tables = repo.list_all()

    # Single batched read of sync_state errors — avoid N+1 GETs against
    # `sync_state` for large registries. The sync_state row is keyed on
    # `table_id` which mirrors `table_registry.name` (see comment in
    # _run_materialized_pass / _build_manifest_for_user about name vs id).
    error_by_name: Dict[str, Optional[str]] = {}
    try:
        rows = conn.execute(
            "SELECT table_id, error FROM sync_state "
            "WHERE status = 'error' AND error IS NOT NULL AND error <> ''"
        ).fetchall()
        error_by_name = {r[0]: r[1] for r in rows}
    except Exception:
        # Defensive: if sync_state is unreadable for any reason, the
        # registry response still serializes — operators just lose the
        # last_sync_error column on this call.
        logger.exception("Failed to read sync_state errors for registry")

    for t in tables:
        # Sync_state.table_id == table_registry.name by convention.
        t["last_sync_error"] = error_by_name.get(t.get("name"))

    return {"tables": tables, "count": len(tables)}


# Wall-clock budget for the synchronous BQ materialization that runs after
# a successful BQ register. If the rebuild + view creation exceeds this,
# we hand the rest off to BackgroundTasks and return 202. 5s matches the
# UX contract in #108 ("Queryable as <view> within seconds") — long enough
# to cover a healthy GCE round-trip, short enough that a hung GCE call
# doesn't park the request handler.
_BQ_SYNC_REGISTER_TIMEOUT_S: float = 5.0


def _materialize_bigquery_extract() -> Dict[str, Any]:
    """Re-build the BigQuery extract.duckdb + master views.

    Wrapper used by both the synchronous (in-band) and async (BackgroundTask)
    code paths after a BQ register/update/delete. Imports kept inside the
    function so non-BQ instances don't pay the import cost on app start.

    Opens a FRESH system DB connection rather than reusing the request-scoped
    one. The request handler closes its connection in a `finally` after the
    response, but BackgroundTask + the timeout-fallback daemon thread can
    both outlive that close — they would then operate on a closed handle (or
    one being torn down concurrently). A fresh handle is cheap (DuckDB is an
    embedded engine) and isolates the worker's lifetime from the request's.

    Returns the rebuild result dict (``{"errors": [...], "tables_registered":
    N, ...}``) so the synchronous caller can propagate failures to the
    operator. Background-task callers ignore the return value, but the loud
    log inside ``_run_bigquery_materialize_with_timeout`` covers that path.
    """
    from connectors.bigquery import extractor as _bq_extractor
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator

    fresh_conn = get_system_db()
    try:
        result = _bq_extractor.rebuild_from_registry(conn=fresh_conn)
        SyncOrchestrator().rebuild()
        return result or {}
    finally:
        try:
            fresh_conn.close()
        except Exception:
            pass


def _materialize_bigquery_extract_bg() -> None:
    """BackgroundTask wrapper around `_materialize_bigquery_extract`.

    BackgroundTasks discard return values, but `rebuild_from_registry` can
    surface auth / config / identifier errors via the ``errors`` list. Log
    those at ERROR level so the failure is loud in the operator's logs even
    though the 202 response can't carry the detail (Decision 3 in #108: a
    202 is documented as "accepted, may not be queryable yet" — we don't
    block on it but we shouldn't swallow it either).
    """
    try:
        result = _materialize_bigquery_extract()
    except Exception:
        logger.exception("BQ post-register background materialize crashed")
        return
    errors = (result or {}).get("errors") or []
    if errors:
        logger.error(
            "BQ post-register background materialize completed with %d error(s): %s",
            len(errors), errors,
        )


def _run_bigquery_materialize_with_timeout(
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """Try to materialize synchronously within the wall-clock budget.

    Returns a dict with:
      - ``status`` ∈ {"ok", "errors", "timeout"} — caller maps to HTTP code
      - ``errors``: list of {table, error} surfaced by ``rebuild_from_registry``
        (only present on ``status="errors"``)

    Mapping by caller (`register_table`):
      - "ok"       → 200 (synchronous success)
      - "errors"   → 500 (rebuild ran but reported errors — propagate so
                     the operator knows the registry row exists but the
                     view wasn't created)
      - "timeout"  → 202 (rebuild still running on a BackgroundTask)

    The synchronous worker runs on a daemon thread (so a hung GCE call
    can't park the request) that opens its OWN system DB connection (see
    `_materialize_bigquery_extract`). Even though FastAPI now invokes the
    sync route in a threadpool — and `done.wait()` no longer blocks the
    event loop — we still off-load to a daemon so the wait is bounded
    even if `rebuild_from_registry` ignores its own timeouts.
    """
    import threading

    done = threading.Event()
    err_holder: Dict[str, Any] = {}
    result_holder: Dict[str, Any] = {}

    def _worker():
        try:
            result_holder["result"] = _materialize_bigquery_extract()
        except Exception as e:  # pragma: no cover — logged below
            err_holder["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True, name="bq-register-rebuild")
    t.start()
    finished = done.wait(_BQ_SYNC_REGISTER_TIMEOUT_S)

    if finished:
        if "error" in err_holder:
            # Worker finished within the wall-clock budget but raised. This
            # is a HARD ERROR, not a timeout — surface it as such so the
            # operator gets the actual exception in the 500 body instead
            # of a misleading 202 + "still working in the background".
            # Earlier revisions returned ``{"status": "timeout"}`` here,
            # which the register handler then mapped to 202 + a retry
            # BackgroundTask; that hid the real failure for `_BQ_SYNC_
            # REGISTER_TIMEOUT_S` seconds before the BG retry surfaced
            # the same exception in the logs.
            exc = err_holder["error"]
            logger.error(
                "BQ post-register rebuild raised within budget: %r",
                exc,
            )
            return {
                "status": "errors",
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
        # Synchronous worker finished cleanly — but check whether
        # `rebuild_from_registry` itself surfaced any errors (auth fail,
        # missing project from the overlay, unsafe identifier slipping the
        # validator, etc.). Without this, those errors got silently logged
        # and the API claimed success.
        result = result_holder.get("result") or {}
        errors = result.get("errors") or []
        if errors:
            logger.error(
                "BQ post-register rebuild reported %d error(s): %s",
                len(errors), errors,
            )
            return {"status": "errors", "errors": errors}
        return {"status": "ok"}

    # Timed out — let the worker keep running on its thread (already daemon)
    # and also schedule a BackgroundTask so the orchestrator gets called via
    # the supported FastAPI path. `_INIT_EXTRACT_LOCK` in the BQ extractor
    # serializes the two file-swap calls so the slow daemon thread and the
    # background task can't tear `extract.duckdb`; the orchestrator's own
    # `_rebuild_lock` protects the master-view rebuild step downstream.
    logger.info(
        "BQ post-register rebuild exceeded %ss budget — handing off to BackgroundTask",
        _BQ_SYNC_REGISTER_TIMEOUT_S,
    )
    background.add_task(_materialize_bigquery_extract_bg)
    return {"status": "timeout"}


@router.post(
    "/register-table",
    responses={
        200: {"description": "BigQuery row registered + materialized synchronously"},
        201: {"description": "Non-BigQuery row registered (no post-insert materialize)"},
        202: {"description": "BigQuery row registered; materialize continues in background"},
        409: {"description": "Table id or view name already in use"},
        500: {"description": "BigQuery row registered but post-insert rebuild failed"},
    },
)
def register_table(
    request: RegisterTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new table in the system.

    Behavior by source_type:
    - **bigquery**: validates BQ-specific shape (dataset / source_table /
      identifier safety / project_id format), forces query_mode='remote' and
      profile_after_sync=False, then synchronously rebuilds extract.duckdb +
      master views with a wall-clock budget. Returns 200 with the view name
      on success, 202 on budget overrun (rebuild continues in a
      BackgroundTask), or 500 if the synchronous rebuild ran but reported
      an error (e.g. auth failure, missing project, unsafe identifier).
    - other source types: insert-only, no post-register hook. Returns 201.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the synchronous-materialize path waits on
    ``threading.Event.wait()``, which would otherwise block the asyncio
    event loop and stall every other request for up to ``_BQ_SYNC_REGISTER_
    TIMEOUT_S``. ``Depends(...)``, ``BackgroundTasks``, and
    ``JSONResponse`` all work the same in sync handlers; the rest of the
    admin module mixes both styles already.

    The route does NOT carry a default ``status_code`` — each branch returns
    its own JSONResponse with the right code. A blanket ``status_code=201``
    on the decorator would mislead OpenAPI consumers about the BQ branch.

    Always: 409 on view-name collision against the existing registry, audit
    log entry on success.
    """
    from fastapi.responses import JSONResponse
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")
    repo = TableRegistryRepository(conn)
    table_id = request.name.strip().lower().replace(" ", "_")

    if repo.get(table_id):
        raise HTTPException(status_code=409, detail=f"Table '{table_id}' already registered")

    # View-name collision pre-check — distinct from id collision above.
    # `id` is derived from `name`, but two callers could legally pick
    # different display names that lower-case + slugify to the same view
    # (e.g. "Orders v2" + "orders_v2"); the strict view-name uniqueness
    # check stops that here, before the orchestrator surfaces it as a
    # silent overwrite at next rebuild.
    existing_by_name = next(
        (r for r in repo.list_all() if (r.get("name") or "") == request.name),
        None,
    )
    if existing_by_name is not None:
        raise HTTPException(
            status_code=409,
            detail=f"View name '{request.name}' is already in use by table id '{existing_by_name.get('id')}'",
        )

    # Refuse rows whose source_type isn't actually configured — pre-fix the
    # row landed in the registry but never synced because there was no
    # Keboola URL/token (or BQ project) to ATTACH against. Surfaces the
    # misconfig at registration time so the operator sees the gap before
    # they wonder why `agnes catalog` is missing the table.
    _validate_source_type_configured(request.source_type)

    # BQ rows go through the extra validation + post-insert materialization
    # contract from issue #108. Other source types keep the legacy insert-only
    # flow — Keboola materialization happens via the scheduled sync, Jira via
    # webhook, local via a manual extractor run.
    is_bigquery = request.source_type == "bigquery"
    if is_bigquery:
        _validate_bigquery_register_payload(request)

    # Phase C: profile_after_sync is no longer passed — the field is
    # deprecated and inert at the runtime layer. The DB column keeps its
    # schema default; the registry response no longer reflects request
    # values for this flag.
    repo.register(
        id=table_id,
        name=request.name,
        folder=request.folder,
        sync_strategy=request.sync_strategy,
        primary_key=request.primary_key,
        description=request.description,
        registered_by=user.get("email"),
        source_type=request.source_type,
        bucket=request.bucket,
        source_table=request.source_table,
        source_query=request.source_query,
        query_mode=request.query_mode,
        sync_schedule=request.sync_schedule,
        # v26 sync-strategy support fields. None for non-Keboola or
        # full_refresh tables; persisted as NULL.
        incremental_window_days=request.incremental_window_days,
        max_history_days=request.max_history_days,
        incremental_column=request.incremental_column,
        where_filters=request.where_filters,
        partition_by=request.partition_by,
        partition_granularity=request.partition_granularity,
        initial_load_chunk_days=request.initial_load_chunk_days,
    )

    # Audit entry — masked params; description kept raw (it's documentation).
    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="register_table",
        resource=table_id,
        params=_sanitize_for_audit(request.model_dump()),
    )

    from app.api.v2_catalog import invalidate_for_table
    invalidate_for_table(table_id)

    if not is_bigquery:
        # Keboola / Jira / local rows are insert-only here. 201 Created — the
        # decorator no longer carries a default status, so each branch is
        # explicit about its code (BQ branch overrides via JSONResponse).
        return JSONResponse(
            status_code=201,
            content={"id": table_id, "name": request.name, "status": "registered"},
        )

    if request.query_mode == "materialized":
        # Materialized BQ rows are picked up by the trigger pass on the next
        # scheduled tick (or via POST /api/sync/trigger). No synchronous
        # rebuild — the COPY can scan multi-GB and would block the request.
        return JSONResponse(
            status_code=201,
            content={
                "id": table_id,
                "name": request.name,
                "status": "registered",
                "view_name": table_id,
                "message": (
                    "Materialized — parquet will be written on the next sync "
                    "tick. Trigger now via POST /api/sync/trigger."
                ),
            },
        )

    # BQ post-register: rebuild extract + master views, with timeout fallback.
    # Decision 1: 200 on synchronous success, 202 on timeout, 500 if the
    # synchronous rebuild surfaced errors. Distinct from the 201 Keboola
    # path above, so the BQ branch builds its own response.
    outcome = _run_bigquery_materialize_with_timeout(background)
    status = outcome.get("status")
    if status == "ok":
        return JSONResponse(
            status_code=200,
            content={
                "id": table_id,
                "name": request.name,
                "status": "ok",
                "view_name": table_id,
            },
        )
    if status == "errors":
        # Registry insert succeeded but the post-insert rebuild reported
        # errors — the row is in the registry but the master view was NOT
        # created. Surface the failure verbatim so the operator can fix
        # the underlying config (typically a missing
        # `data_source.bigquery.project` in the overlay or auth that lacks
        # bigquery.metadata.get on the dataset). The row stays in the
        # registry; a re-run after fixing the config picks up the existing
        # row and creates the view on the next register/update or
        # scheduler tick.
        return JSONResponse(
            status_code=500,
            content={
                "id": table_id,
                "name": request.name,
                "status": "rebuild_failed",
                "view_name": table_id,
                "errors": outcome.get("errors") or [],
                "message": (
                    "Registry row created but post-insert rebuild failed; "
                    "view is not queryable. See `errors` for details."
                ),
            },
        )
    # Default: timeout — rebuild continues on a BackgroundTask.
    return JSONResponse(
        status_code=202,
        content={
            "id": table_id,
            "name": request.name,
            "status": "accepted",
            "view_name": table_id,
            "message": "Registration accepted; materializing in background",
        },
    )


class PrecheckResponse(BaseModel):
    """Response model for /api/admin/register-table/precheck.

    Documented here so OpenAPI consumers know what to expect; the route
    returns a plain dict for backwards compatibility with the rest of the
    admin API which doesn't use response_model.
    """
    ok: bool
    table: Dict[str, Any]


@router.post("/register-table/precheck")
def register_table_precheck(
    request: RegisterTableRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate a register-table payload + (BQ only) confirm the source table exists.

    No DB write. Used by the UI to surface row count + size + column count
    in the modal before the operator clicks Register, and by the CLI's
    ``--dry-run`` to print what *would* be registered without touching
    state. Identical Pydantic validation to register-table; for BQ rows we
    additionally make a ``bigquery.Client(project).get_table(...)`` call
    and surface the GCP error verbatim.

    Defined as a plain ``def`` (not ``async def``) so FastAPI runs it in a
    threadpool — the BQ branch makes synchronous ``bigquery.Client(...)``
    /``client.get_table(...)`` calls, which would otherwise block the
    asyncio event loop and stall every other request for the duration of
    the GCE round-trip. Mirrors the same conversion done for
    ``register_table`` (see comment on that route). ``Depends(...)`` works
    identically in sync handlers.
    """
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail="Table name cannot be empty")

    if request.source_type != "bigquery":
        # M1 only adds BQ-specific precheck. Other source types get a
        # validation-only response so the CLI / UI can rely on the same
        # endpoint shape across types.
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": request.source_type,
                "bucket": request.bucket,
                "source_table": request.source_table,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": "precheck for non-bigquery sources is validation-only in M1",
            },
        }

    # BQ-specific shape validation (forces query_mode/profile_after_sync,
    # checks identifier safety, validates project_id from instance.yaml).
    _validate_bigquery_register_payload(request)

    # Materialized BQ rows have no `dataset.source_table` to round-trip —
    # the SQL body is the contract. Skip the BQ-jobs-API call and return a
    # validation-only precheck so the CLI's `--dry-run --query-mode
    # materialized` path doesn't crash on an empty fully-qualified name.
    if request.query_mode == "materialized":
        return {
            "ok": True,
            "table": {
                "name": request.name,
                "source_type": "bigquery",
                "query_mode": "materialized",
                "source_query": request.source_query,
                "rows": None,
                "size_bytes": None,
                "columns": [],
                "note": (
                    "materialized precheck is validation-only — the SQL is "
                    "evaluated for cost on each scheduled materialize tick"
                ),
            },
        }

    # Round-trip the BQ jobs API to confirm the table exists and the SA can
    # see it. Imports kept local to avoid pulling google-cloud-bigquery into
    # the import chain on non-BQ instances.
    try:
        from google.cloud import bigquery  # noqa: PLC0415
        from google.api_core import exceptions as google_exc  # noqa: PLC0415
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "google-cloud-bigquery not installed; install the bigquery "
                f"extras to use BQ precheck ({e})"
            ),
        ) from e

    from app.instance_config import get_value
    project_id = get_value("data_source", "bigquery", "project", default="")
    dataset = (request.bucket or "").strip()
    source_table = (request.source_table or "").strip()
    fq = f"{project_id}.{dataset}.{source_table}"

    try:
        client = bigquery.Client(project=project_id)
        bq_table = client.get_table(fq)
    except google_exc.NotFound as e:
        raise HTTPException(status_code=404, detail=f"BigQuery table not found: {fq} ({e})") from e
    except google_exc.Forbidden as e:
        raise HTTPException(
            status_code=403,
            detail=(
                f"BigQuery access denied for {fq}: {e}. "
                "Service account needs bigquery.metadata.get on the dataset."
            ),
        ) from e
    except Exception as e:
        # Auth errors, transient 5xx, malformed table refs — surface as 400
        # so the operator gets the GCP error verbatim and can fix their
        # config without us guessing the right HTTP code.
        raise HTTPException(status_code=400, detail=f"BigQuery precheck failed for {fq}: {e}") from e

    columns = [
        {"name": f.name, "type": f.field_type}
        for f in (bq_table.schema or [])
    ]
    return {
        "ok": True,
        "table": {
            "name": request.name,
            "source_type": "bigquery",
            "bucket": dataset,
            "source_table": source_table,
            "project_id": project_id,
            "rows": int(bq_table.num_rows or 0),
            "size_bytes": int(bq_table.num_bytes or 0),
            "columns": columns,
            "column_count": len(columns),
        },
    }


@router.put("/registry/{table_id}")
async def update_table(
    table_id: str,
    request: UpdateTableRequest,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update a registered table's configuration.

    For BQ rows, schedules a background rebuild so the master view picks
    up changes (e.g. a renamed dataset) without waiting for the next
    scheduled sync.
    """
    repo = TableRegistryRepository(conn)
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    # `exclude_unset=True` honors the PUT-shape distinction between
    # "field omitted from body" (keep existing) vs "field sent as null"
    # (clear to NULL). Pre-v26 the handler used `model_dump()` filtered by
    # `if v is not None`, which collapsed both cases to "omitted" — meaning
    # an admin couldn't clear a field via PUT. v26 needs the clear path so
    # the Edit modal can switch a partitioned row back to full_refresh and
    # have the stale partition_by / partition_granularity / max_history_days
    # actually go away (without this fix, those fields linger and either
    # confuse the dispatcher or trip the v26 conflict-policy validator on
    # the next edit).
    #
    # Contract change (Devin Review finding 0001): callers that previously
    # sent explicit `null` to mean "no-op, keep existing" will now have the
    # field cleared. In practice this is fine — the only known caller is
    # the Edit modal, which pre-populates form fields from the existing row
    # and JSON-encodes the populated (non-null) value back. CLI register-table
    # only POSTs new rows, never PUTs nulls. If a future client needs the
    # old "null = no-op" semantics for some field, it should omit the field
    # from the body instead of sending null — that's the canonical PUT shape.
    updates = request.model_dump(exclude_unset=True)
    # Run BQ-shape validation BEFORE persisting whenever the merged record
    # would be a bigquery row (existing was BQ, or the patch flips it to BQ,
    # or the patch touches BQ-relevant fields on an already-BQ row). Without
    # this gate, an admin could PUT `bucket="evil\"; DROP --"` onto a BQ
    # row and the next rebuild would silently fail at view-create time —
    # surface the bad shape at PUT time instead.
    if updates:
        # Preserve the original `registered_at` across PUTs — `repo.register`
        # now accepts it as an optional kwarg; without this the upsert would
        # stamp a fresh `now()` on every edit (issue #130).
        merged = dict(existing)
        merged.update(updates)
        merged.pop("id", None)  # avoid duplicate id kwarg

        # When switching the merged record away from materialized mode, drop
        # the stale source_query — the request validator can't clear it via
        # the `if v is not None` filter above. Without this, a remote/local
        # row would carry an orphan source_query in the registry.
        if merged.get("query_mode") != "materialized":
            merged["source_query"] = None

        # Cross-source coherence: query_mode='materialized' requires a
        # non-empty source_query for ALL source types, not just BigQuery.
        # BQ rows without source_query can be server-generated from
        # bucket+source_table (handled by _validate_bigquery_register_payload
        # via the synthetic RegisterTableRequest below). Non-BQ rows (e.g.
        # Keboola) still require an explicit source_query at PUT time.
        if merged.get("query_mode") == "materialized":
            sq = merged.get("source_query")
            if not sq or not str(sq).strip():
                # BQ rows: let _validate_bigquery_register_payload generate
                # source_query from bucket+source_table (falls through below).
                # Non-BQ rows: no server-generate fallback; raise 422.
                if merged.get("source_type") != "bigquery":
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "query_mode='materialized' requires a non-empty "
                            "source_query. To revert to a non-materialized mode, "
                            "PATCH query_mode='local' (Keboola) or 'remote' "
                            "(BigQuery) and the stale source_query is cleared "
                            "automatically."
                        ),
                    )
            # Backtick guard removed for materialized rows: the Task 2 wrapping
            # path (connectors.bigquery.extractor.materialize_query) now runs
            # admin SQL through the BQ jobs API using BQ-native syntax, which
            # requires backticks for dashed project/dataset identifiers.
            # Non-materialized rows still reject backticks in the model validator.

        if merged.get("source_type") == "bigquery":
            # Reuse the register-time validator. It mutates the request to
            # force query_mode='remote' / profile_after_sync=False (or to
            # leave a materialized row alone) — apply the same coercion to
            # `merged` so the persisted row matches.
            synthetic = RegisterTableRequest(
                name=merged.get("name") or table_id,
                bucket=merged.get("bucket"),
                source_table=merged.get("source_table"),
                source_query=merged.get("source_query"),
                source_type="bigquery",
                query_mode=merged.get("query_mode") or "remote",
                profile_after_sync=bool(merged.get("profile_after_sync") or False),
                primary_key=merged.get("primary_key"),
                description=merged.get("description"),
                folder=merged.get("folder"),
                sync_strategy=merged.get("sync_strategy") or "full_refresh",
                sync_schedule=merged.get("sync_schedule"),
            )
            _validate_bigquery_register_payload(synthetic)
            merged["query_mode"] = synthetic.query_mode
            merged["profile_after_sync"] = synthetic.profile_after_sync
            merged["source_query"] = synthetic.source_query

        repo.register(id=table_id, **merged)

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="update_table",
        resource=table_id,
        params=_sanitize_for_audit({"updated_fields": sorted(updates.keys()), **updates}),
    )

    # If we updated a BQ row (or one that's now BQ), refresh the extract in
    # the background so the view picks up renames / column-list changes.
    # Use the BG wrapper so any rebuild errors are logged at ERROR level
    # instead of being silently dropped by BackgroundTasks (which discards
    # return values).
    after = repo.get(table_id) or {}
    if after.get("source_type") == "bigquery":
        background.add_task(_materialize_bigquery_extract_bg)

    from app.api.v2_catalog import invalidate_for_table
    invalidate_for_table(table_id)

    return {"id": table_id, "updated": list(updates.keys())}


@router.delete("/registry/{table_id}", status_code=204)
async def unregister_table(
    table_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Unregister a table from the system.

    For BQ rows, schedules a background rebuild so the dropped row's
    master view is removed from analytics.duckdb (rather than hanging
    around until the next scheduled sync).

    For materialized rows, also removes the canonical parquet at
    `${DATA_DIR}/extracts/<source_type>/data/<id>.parquet` and clears
    the matching `sync_state` row. Without these two cleanups, the
    manifest endpoint kept advertising the dropped table to `agnes pull`
    (sync_state-driven) and the orchestrator's next rebuild could
    resurrect a master view from the leftover parquet (E2E sub-agent
    finding 2026-05-01).
    """
    repo = TableRegistryRepository(conn)
    existing = repo.get(table_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Table not found")

    was_bigquery = existing.get("source_type") == "bigquery"
    was_materialized = existing.get("query_mode") == "materialized"
    source_type = existing.get("source_type") or ""
    name = existing.get("name") or table_id

    repo.unregister(table_id)

    # Drop the canonical parquet for materialized rows. Path layout:
    # `${DATA_DIR}/extracts/<source_type>/data/<name>.parquet` — the
    # filename is keyed by `table_registry.name` (matches sync_state
    # bookkeeping convention; see _run_materialized_pass + the manifest
    # builder for the same name-keyed lookup). Defensively remove the
    # `.parquet.tmp` sibling too in case a prior materialize crashed
    # mid-COPY. Failure to remove (file missing, permission error) is
    # logged but doesn't fail the DELETE — the registry row is already
    # gone, and the orphan parquet will not produce a master view at
    # next rebuild because the orchestrator's _meta-driven scan never
    # picks up bare parquet files.
    if was_materialized and source_type in ("bigquery", "keboola"):
        try:
            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            base = data_dir / "extracts" / source_type / "data"
            for candidate in (
                base / f"{name}.parquet",
                base / f"{name}.parquet.tmp",
            ):
                if candidate.exists():
                    candidate.unlink()
                    logger.info(
                        "Removed materialized parquet for unregistered table %s: %s",
                        table_id, candidate,
                    )
        except Exception as e:
            logger.warning(
                "Failed to remove materialized parquet for %s: %s — registry row is "
                "still dropped; clean up the file manually if it lingers",
                table_id, e,
            )

    # Clear sync_state for any source/mode (a row that was synced at any
    # point — local/materialized — has a sync_state entry that the manifest
    # serves regardless of registry state). Pre-fix, the manifest still
    # advertised the dropped table to `agnes pull` because sync_state was
    # never cleaned up, and analysts kept getting it through the manifest.
    try:
        conn.execute("DELETE FROM sync_state WHERE table_id = ?", [name])
        conn.execute("DELETE FROM sync_history WHERE table_id = ?", [name])
    except Exception as e:
        logger.warning(
            "Failed to clear sync_state for unregistered table %s: %s — "
            "manifest may still advertise the dropped row to agnes pull",
            table_id, e,
        )

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="unregister_table",
        resource=table_id,
        params=_sanitize_for_audit({
            "name": existing.get("name"),
            "source_type": existing.get("source_type"),
            "bucket": existing.get("bucket"),
            "source_table": existing.get("source_table"),
        }),
    )

    from app.api.v2_catalog import invalidate_for_table
    invalidate_for_table(table_id)

    if was_bigquery:
        background.add_task(_materialize_bigquery_extract_bg)


@router.post("/configure")
async def configure_instance(
    request: ConfigureRequest,
    user: dict = Depends(require_admin),
):
    """Configure data source and instance settings via API.

    Writes config to instance.yaml and persists secrets to .env_overlay.
    AI agents and the /setup wizard use this instead of manual file editing.
    """
    import yaml

    if request.data_source not in ("keboola", "bigquery", "local"):
        raise HTTPException(status_code=400, detail="data_source must be 'keboola', 'bigquery', or 'local'")

    # Validate credentials if provided
    if request.data_source == "keboola":
        if not request.keboola_token or not request.keboola_url:
            raise HTTPException(status_code=400, detail="keboola_token and keboola_url are required for Keboola data source")
        _validate_url_not_private(request.keboola_url, field_name="keboola_url")
        try:
            from connectors.keboola.client import KeboolaClient
            client = KeboolaClient(token=request.keboola_token, url=request.keboola_url)
            client.test_connection()
        except Exception as e:
            logger.error("Keboola connection validation failed: %s", e)
            raise HTTPException(status_code=400, detail="Keboola connection failed. Check your token and URL.")

    elif request.data_source == "bigquery":
        if not request.bigquery_project:
            raise HTTPException(status_code=400, detail="bigquery_project is required for BigQuery data source")

    # Write instance.yaml to DATA_DIR/state/ (writable Docker volume),
    # NOT to CONFIG_DIR which is mounted read-only in Docker.
    #
    # Narrow-overlay write strategy — must match `/api/admin/server-config`:
    # 1. Read overlay verbatim (do NOT fall back to static). Falling back
    #    would copy env-resolved cleartext secrets from the merged static
    #    file back into the overlay (e.g. `smtp_password: ${SMTP_PASSWORD}`
    #    → `smtp_password: hunter2`). The wizard only ever sets
    #    `instance`, `auth`, `data_source` here, so other sections must
    #    flow from the static file via `load_instance_config`'s deep-merge
    #    — they don't belong in the overlay at all.
    # 2. Patch only the sections this endpoint touches.
    # 3. Write the narrow overlay back atomically (tmp + os.replace).
    from app.secrets import _state_dir
    config_path = _state_dir() / "instance.yaml"

    # Same serialization + corrupt-overlay handling as POST /server-config.
    with _overlay_write_lock:
        overlay: dict = {}
        if config_path.exists():
            try:
                overlay = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                logger.exception("configure: refusing to overwrite corrupt overlay at %s", config_path)
                raise HTTPException(
                    status_code=500,
                    detail=f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                           "back up and remove the file, or fix it by hand",
                ) from e

        # Merge instance settings into the overlay only — never seed from the
        # env-resolved merged config.
        if request.instance_name:
            overlay.setdefault("instance", {})["name"] = request.instance_name

        if request.allowed_domain:
            overlay.setdefault("auth", {})["allowed_domain"] = request.allowed_domain

        # data_source is fully owned by this endpoint — replace wholesale.
        overlay["data_source"] = {"type": request.data_source}
        if request.data_source == "keboola":
            overlay["data_source"]["keboola"] = {
                "stack_url": request.keboola_url,
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            }
        elif request.data_source == "bigquery":
            overlay["data_source"]["bigquery"] = {
                "project": request.bigquery_project,
                "location": request.bigquery_location or "us",
            }

        # Seed an ai: block on first-time setup so LLM-driven services
        # (corporate_memory, verification_detector) can boot without manual
        # YAML editing. Only inserts when the overlay has no ai: yet AND an
        # appropriate env var is present — never overwrites operator config,
        # never writes a placeholder block (#176).
        if "ai" not in overlay:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            llm_key = os.environ.get("LLM_API_KEY", "").strip()
            if anthropic_key:
                overlay["ai"] = {
                    "provider": "anthropic",
                    "api_key": "${ANTHROPIC_API_KEY}",
                    "model": "claude-haiku-4-5-20251001",
                    "structured_output": "auto",
                }
            elif llm_key:
                overlay["ai"] = {
                    "provider": "anthropic",
                    "api_key": "${LLM_API_KEY}",
                    "model": "claude-haiku-4-5-20251001",
                    "structured_output": "auto",
                }

        # Atomic write to writable data volume — same tmp + os.replace pattern
        # as the server-config editor so a concurrent save can't tear the file.
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay, default_flow_style=False, sort_keys=False))
        os.replace(tmp_path, config_path)
        logger.info("Wrote instance config to %s", config_path)

    # Persist secrets to .env_overlay (in data volume, never in git)
    secrets_to_persist = {}
    if request.keboola_token:
        secrets_to_persist["KEBOOLA_STORAGE_TOKEN"] = request.keboola_token
    if request.keboola_url:
        secrets_to_persist["KEBOOLA_STACK_URL"] = request.keboola_url

    if secrets_to_persist:
        # Resolve via _state_dir() so the path matches app/main.py's
        # startup-time read of the same overlay. Without this, an operator
        # on the flat-mount layout (STATE_DIR=/data-state) would write
        # secrets to /data/state/.env_overlay here while the app reads
        # from /data-state/.env_overlay — silent loss on next restart.
        from app.secrets import _state_dir
        overlay_path = _state_dir() / ".env_overlay"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing overlay
        existing_overlay = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing_overlay[k.strip()] = v.strip()
        existing_overlay.update(secrets_to_persist)

        overlay_path.write_text(
            "\n".join(f"{k}={v}" for k, v in existing_overlay.items()) + "\n"
        )
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Persisted %d secrets to .env_overlay", len(secrets_to_persist))

        # Inject into current process environment
        for k, v in secrets_to_persist.items():
            os.environ[k] = v

    # Invalidate cached instance config so next read picks up changes.
    # Use the public helper (matches `/api/admin/server-config`); reaching
    # into the private global silently breaks if the cache layout changes.
    from app.instance_config import reset_cache
    reset_cache()

    return {
        "status": "ok",
        "data_source": request.data_source,
        "connection": "verified" if request.data_source != "local" else "local",
    }


def _split_keboola_table_id(full_id: str, fallback_name: str = "") -> tuple[str, str]:
    """Split a Keboola table id into ``(bucket, source_table)``.

    Keboola convention: ``<stage>.<bucket-id>.<table>`` where stage ∈
    ``{in, out, sys}`` and bucket-id typically starts with ``c-``
    (e.g. ``in.c-finance.orders``). Storage API export-async needs the
    FULL ``<stage>.<bucket-id>`` as the bucket arg — a stripped
    ``c-finance`` 404s. The 2-segment fallback covers id strings
    without the stage prefix; the 0/1-segment path returns empty
    bucket and uses ``fallback_name`` as the table name so the row
    fails loud at sync time rather than silently registering with
    no source coordinates.
    """
    parts = (full_id or "").strip().split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", fallback_name or full_id


def _build_keboola_discovery_plan(
    conn: duckdb.DuckDBPyConnection, discovered: list[dict],
) -> dict:
    """Inspect ``discovered`` (output of ``KeboolaClient.discover_all_tables``)
    against the live registry and bucket every entry into one of:

      - ``new``: not in registry, will be inserted.
      - ``existing_match``: row already in registry under the same id
        AND its ``(bucket, source_table)`` matches what discovery would
        write — no-op, nothing to do.
      - ``existing_drift``: a row in the registry conflicts with what
        discovery would write. Two flavours, both surfaced for operator
        visibility but **never overwritten**:

          1. Same registry id, different ``(bucket, source_table)`` —
             admin corrected the coordinates inline (rarer).
          2. Different registry id but the discovered ``name`` clashes
             with an existing row's ``name`` (case-insensitive). Real
             example: registry has ``id='kbc_job', name='kbc_job',
             bucket='in.c-kbc_telemetry'``; Keboola exposes the same
             logical table at id ``in.c-keboola-storage.job`` (which
             slugs to a different ``table_id``). Without this
             check, auto-discovery would insert a duplicate ``kbc_job``
             whose Storage API export-async 404s.

      - ``invalid``: id couldn't produce a usable ``table_id`` slug.

    Each bucket carries the exact rows; the API endpoint composes a
    summary + (optionally) executes. Pre-fix, this logic was inlined
    in ``_discover_and_register_tables`` and there was no way to see
    what would change without writing.
    """
    repo = TableRegistryRepository(conn)
    # Pre-load all keboola rows once so the name-collision lookup
    # below is O(1) per discovered entry. Falls back to per-id
    # `repo.get(...)` calls when list_all isn't available — keeps
    # the single-row test stubs working without forcing them to
    # implement list_all.
    try:
        all_rows = [r for r in repo.list_all() if r.get("source_type") == "keboola"]
    except AttributeError:
        all_rows = []
    by_name: dict[str, dict] = {
        (r.get("name") or "").strip().lower(): r for r in all_rows
    }

    plan = {"new": [], "existing_match": [], "existing_drift": [], "invalid": []}
    for table in discovered:
        full_id = (table.get("id") or "").strip()
        # Slug used as the registry primary key. Lowercase, dots/spaces
        # → underscores. Stable across discovery runs.
        table_id = full_id.lower().replace(".", "_").replace(" ", "_")
        if not table_id:
            plan["invalid"].append({
                "table_id": "",
                "full_id": full_id,
                "reason": "empty id from discovery payload",
            })
            continue

        # Prefer Keboola's authoritative `bucket_id` (separate field in
        # the API response, normalised by `discover_all_tables`) over
        # parsing the full id string. Fall back to the parser when
        # the API didn't return bucket_id (older fallback path inside
        # discover_all_tables).
        bucket = (table.get("bucket_id") or "").strip()
        name = (table.get("name") or "").strip()
        source_table = name
        if not bucket or not source_table:
            bucket, source_table = _split_keboola_table_id(full_id, source_table)

        entry = {
            "table_id": table_id,
            "name": table.get("name", table_id),
            "full_id": full_id,
            "bucket": bucket,
            "source_table": source_table,
        }

        existing = repo.get(table_id)
        if existing is not None:
            ex_bucket = existing.get("bucket") or ""
            ex_source_table = existing.get("source_table") or ""
            if ex_bucket == bucket and ex_source_table == source_table:
                plan["existing_match"].append(entry)
            else:
                plan["existing_drift"].append({
                    **entry,
                    "registry_bucket": ex_bucket,
                    "registry_source_table": ex_source_table,
                    "registry_id": existing.get("id"),
                    "drift_kind": "same_id_diff_coords",
                })
            continue

        # No row at this id. Look for a name collision (admin
        # registered the same logical table under a different id).
        name_match = by_name.get(name.lower()) if name else None
        if name_match is not None:
            plan["existing_drift"].append({
                **entry,
                "registry_bucket": name_match.get("bucket") or "",
                "registry_source_table": name_match.get("source_table") or "",
                "registry_id": name_match.get("id"),
                "drift_kind": "name_collision",
            })
            continue

        plan["new"].append(entry)
    return plan


def _discover_and_register_tables(
    conn: duckdb.DuckDBPyConnection,
    user_email: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Discover tables from configured source and register them.

    Behavior:
      - Only the configured source type ``keboola`` is supported here
        (BigQuery uses a different discovery endpoint).
      - Already-registered rows are NEVER overwritten. The plan
        classifies them as ``existing_match`` (no-op, registry agrees
        with discovery) or ``existing_drift`` (admin edited the
        coordinates; left alone, surfaced in the response so the
        operator sees the divergence).
      - ``dry_run=True`` returns the plan without writing anything —
        useful for auditing before a re-discovery on a registry that
        already has admin overrides.
    """
    from app.instance_config import get_data_source_type, get_value

    source_type = get_data_source_type()
    if source_type != "keboola":
        return {
            "registered": 0,
            "skipped": 0,
            "errors": 0,
            "drifted": 0,
            "tables": [],
            "source": source_type,
            "dry_run": dry_run,
        }

    from connectors.keboola.client import KeboolaClient
    # Read from data_source.keboola (matches what /api/admin/configure writes)
    url = get_value("data_source", "keboola", "stack_url", default="")
    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    token = os.environ.get(token_env, "") if token_env else ""
    if not token:
        token = os.environ.get("KEBOOLA_STORAGE_TOKEN", "")

    client = KeboolaClient(token=token, url=url)
    discovered = client.discover_all_tables()

    plan = _build_keboola_discovery_plan(conn, discovered)
    drift_summary = [
        {
            "table_id": e["table_id"],
            "discovery": {"bucket": e["bucket"], "source_table": e["source_table"]},
            "registry":  {"bucket": e["registry_bucket"],
                           "source_table": e["registry_source_table"]},
        }
        for e in plan["existing_drift"]
    ]

    if dry_run:
        return {
            "registered": 0,
            "skipped": len(plan["existing_match"]),
            "errors": len(plan["invalid"]),
            "drifted": len(plan["existing_drift"]),
            "tables": [e["table_id"] for e in plan["new"]],
            "would_register": [e["table_id"] for e in plan["new"]],
            "drift": drift_summary,
            "invalid": plan["invalid"],
            "source": "keboola",
            "dry_run": True,
        }

    repo = TableRegistryRepository(conn)
    registered = 0
    errors = 0
    table_names = []

    for entry in plan["new"]:
        try:
            repo.register(
                id=entry["table_id"],
                name=entry["name"],
                source_type="keboola",
                bucket=entry["bucket"],
                source_table=entry["source_table"],
                # Keboola goes through Storage API export-async via the
                # materialized path (NULL source_query = full table). The
                # legacy `local` mode for Keboola was retired in v26 and
                # would no-op here anyway.
                query_mode="materialized",
                registered_by=user_email,
                description=f"Auto-discovered from Keboola: {entry['full_id']}",
            )
            registered += 1
            table_names.append(entry["table_id"])
        except Exception as e:
            logger.warning("Failed to register %s: %s", entry["table_id"], e)
            errors += 1

    if plan["existing_drift"]:
        logger.warning(
            "Auto-discover skipped %d row(s) where the admin-edited "
            "bucket/source_table differs from discovery — preserving "
            "the admin values. Run with dry_run=True to see the deltas.",
            len(plan["existing_drift"]),
        )

    return {
        "registered": registered,
        "skipped": len(plan["existing_match"]),
        "errors": errors + len(plan["invalid"]),
        "drifted": len(plan["existing_drift"]),
        "tables": table_names,
        "drift": drift_summary,
        "invalid": plan["invalid"],
        "source": "keboola",
        "dry_run": False,
    }


@router.post("/discover-and-register")
async def discover_and_register(
    dry_run: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Discover tables from configured source and auto-register them.

    Combines discover-tables + register-table into one call. Already-
    registered rows are NEVER overwritten — admin edits to bucket /
    source_table win. The response surfaces a ``drift`` array listing
    any rows where discovery would have written different coordinates
    than what's in the registry, so operators can audit divergence
    after a Keboola-side bucket rename / table move.

    Query params:
      - ``dry_run=true`` returns the plan without writing anything.
        Lists ``would_register``, ``drift``, and ``invalid`` so an
        operator can decide whether to proceed (or, in the drift case,
        which side they want to fix).

    Used by /setup wizard and AI agents.
    """
    try:
        result = _discover_and_register_tables(
            conn, user.get("email", "admin"), dry_run=dry_run,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery and registration failed: {e}")


# ---------------------------------------------------------------------------
# Scheduler-driven LLM pipeline endpoints (#176)
#
# The scheduler container drives these via HTTP rather than running them
# in-process — same reasoning as the existing /api/marketplaces/sync-all
# job: DuckDB allows only one writer per file across processes, and the
# app keeps a long-lived handle on system.duckdb. Routing through the app
# inherits the existing connection without contention.
#
# Each endpoint is `def` (sync), so FastAPI runs it in a thread pool —
# the underlying jobs do blocking I/O (LLM calls, DuckDB writes,
# filesystem scans). Running on the asyncio thread would block health
# checks for the duration of a job.
# ---------------------------------------------------------------------------


@router.post("/run-session-collector")
def run_session_collector(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the session-collector job from the scheduler.

    Walks /home/*/user/sessions/*.jsonl and copies new files into
    /data/user_sessions/<user>/. Idempotent — already-collected files
    are skipped.
    """
    from services.session_collector import collector

    # Call run() not main(): main() does argparse.parse_args() which would
    # try to parse uvicorn's sys.argv and SystemExit(2) the worker.
    rc: int = 1
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        rc, stats = collector.run(dry_run=False, verbose=False)
    except Exception as e:
        # Mirror run_verification_detector / run_corporate_memory
        # (#179 review): capture any unhandled error so audit_log +
        # /admin/scheduler-runs reflect the failure. Re-raised below
        # after audit. Filesystem permission, OSError on /home walking,
        # etc. are realistic failure modes worth surfacing.
        job_error = e

    audit_params: dict = {"rc": rc, **stats}
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="run_session_collector",
        resource="job:session-collector",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": rc == 0, "details": {"rc": rc, **stats}}


@router.post("/run-session-processor")
def run_session_processor(
    processor: str = Query(..., description="Processor name (e.g. 'verification', 'usage')"),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger one session-pipeline processor against /data/user_sessions/*.

    Replaces the per-processor /run-* endpoints with a single parametrized
    entry. The scheduler invokes this once per registered processor on its
    own cadence; processors are independent (one slow / failing processor
    can't block any other).

    Returns 400 if `processor` is unknown. The verification processor
    requires an LLM extractor — if the instance has no ai: config and no
    ANTHROPIC_API_KEY / LLM_API_KEY, it won't appear in the registry and
    the call returns 400 the same as a misspelled name.
    """
    from services.session_pipeline.runner import run_processor as _run_processor
    from services.session_processors import get_processor, list_processor_names
    from src.db import get_system_db

    proc = get_processor(processor)
    if proc is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown processor '{processor}'. "
                f"Known: {', '.join(list_processor_names())}"
            ),
        )

    # Reject overlapping invocations of the same processor (PR #232 review).
    # See `_get_processor_run_lock` docstring for why this matters
    # (verification_evidence row duplication on race).
    proc_lock = _get_processor_run_lock(processor)
    if not proc_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Processor '{processor}' is already running",
        )

    job_conn = get_system_db()
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        stats = _run_processor(job_conn, proc)
        # Rebuild daily rollups after a successful usage run so the
        # marketplace / admin dashboards see fresh aggregates. Runs on the
        # same connection while it's still open; incremental (last-7-days)
        # so it's cheap. Kept here (not in runner.py) to stay
        # processor-agnostic at the framework level.
        if processor == "usage" and stats.get("errors", 0) == 0:
            from services.session_processors.usage_lib import rebuild_rollups
            try:
                rebuild_rollups(job_conn)
            except Exception as rollup_exc:
                logger.warning("usage rollup rebuild failed: %s", rollup_exc)
    except Exception as e:
        # Capture and re-raise after audit so an unhandled runner error
        # (DuckDB lock, network blip, unexpected SDK type) still leaves a
        # row in audit_log — the /admin/scheduler-runs page is the
        # operator's only signal beyond docker logs.
        job_error = e
    finally:
        try:
            job_conn.close()
        except Exception:
            pass
        # Always release, even if the runner raised. A leaked lock would
        # wedge the processor permanently until process restart.
        proc_lock.release()

    audit_params: dict = {
        "processor": processor,
        "scanned": stats.get("scanned", 0),
        "processed": stats.get("processed", 0),
        "skipped": stats.get("skipped", 0),
        "errors": stats.get("errors", 0),
        "items_extracted": stats.get("items_extracted", 0),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action=f"run_session_processor:{processor}",
        resource=f"job:session-processor:{processor}",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": stats.get("errors", 0) == 0, "details": stats}


@router.post("/run-corporate-memory")
def run_corporate_memory(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the corporate-memory catalog refresh from the scheduler.

    Reads all CLAUDE.local.md files, sends them through the LLM with the
    existing catalog, and writes an updated catalog to knowledge.json.
    """
    from services.corporate_memory.collector import collect_all

    # Fail-fast (#176): collect_all raises ValueError when no ai: block AND
    # no env keys are present. Surface the actionable factory message in a
    # 500 instead of letting it crash the request anonymously.
    stats: dict = {}
    job_error: Optional[Exception] = None
    try:
        stats = collect_all(dry_run=False)
    except ValueError as e:
        # Already-translated misconfiguration → 500 with actionable message
        # but no audit row (the request never reached the LLM stage).
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        # Mirror run_verification_detector (#179 review): capture any other
        # unhandled error so audit_log + /admin/scheduler-runs reflect the
        # failure. Re-raised below after audit.
        job_error = e

    audit_params: dict = {
        "items_new": stats.get("items_new", 0),
        "items_filtered": stats.get("items_filtered", 0),
        "errors": len(stats.get("errors", [])),
        "skipped": stats.get("skipped", False),
    }
    if job_error is not None:
        audit_params["unhandled_error"] = f"{type(job_error).__name__}: {job_error}"

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="run_corporate_memory",
        resource="job:corporate-memory",
        params=audit_params,
    )

    if job_error is not None:
        raise HTTPException(status_code=500, detail=audit_params["unhandled_error"])

    return {"ok": not stats.get("errors"), "details": stats}


# ---------------------------------------------------------------------------
# Flea-market guardrails — admin endpoints
#
# Backs /admin/store/submissions (the human triage page) and the override /
# retry / delete-submission action buttons. Every action here writes an
# audit_log row so the trail of "who force-published what, and why" is
# permanent — same governance posture as the corporate-memory + scheduler
# runs surfaces.
# ---------------------------------------------------------------------------

import shutil as _shutil


@router.get("/store/submissions")
async def admin_list_store_submissions(
    status: Optional[str] = None,
    submitter: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 — FastAPI query-param name
    name: Optional[str] = None,
    version: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List flea-market guardrail submissions newest-first.

    All filters AND together. ``status`` is comma-separated
    (e.g. ``blocked_inline,blocked_llm``). ``submitter`` matches
    ``submitter_id`` exactly. ``type`` is one of ``skill`` / ``agent`` /
    ``plugin``. ``name`` and ``version`` are case-insensitive substrings.
    ``limit`` clamped to [1, 500].
    """
    from src.repositories.store_submissions import StoreSubmissionsRepository

    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
    if type and type not in {"skill", "agent", "plugin"}:
        raise HTTPException(status_code=400, detail="invalid_type")
    limit = max(1, min(int(limit), 500))
    skip = max(0, int(skip))

    # v36+ chip routing: 'archived' / 'deleted' tokens in ?status=
    # are LIFECYCLE filters, not verdict filters. The repo handles the
    # JOIN-on-entity logic for archived; submission terminal marker
    # for deleted. Verdict tokens (approved, blocked_*, pending_*,
    # overridden, review_error) pass through unchanged.
    lifecycle = None
    if statuses == ["archived"]:
        lifecycle = "archived"
        statuses = None
    elif statuses == ["deleted"]:
        lifecycle = "deleted"
        statuses = None

    try:
        items, total = StoreSubmissionsRepository(conn).list_for_admin(
            status=statuses,
            submitter_id=submitter or None,
            type_=type or None,
            name_substr=name or None,
            version_substr=version or None,
            sort_by=sort or None,
            sort_order=order or None,
            lifecycle=lifecycle,
            limit=limit, skip=skip,
        )
    except ValueError as e:
        # Sort key whitelist rejection (#23) — surface as 400 so the UI
        # can show the operator a meaningful message instead of 500.
        msg = str(e)
        if msg.startswith("invalid_sort_key"):
            raise HTTPException(status_code=400, detail="invalid_sort_key")
        raise
    return {"items": items, "total": total, "limit": limit, "skip": skip}


@router.get("/store/submissions/{submission_id}")
async def admin_get_store_submission(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.repositories.store_submissions import StoreSubmissionsRepository

    sub = StoreSubmissionsRepository(conn).get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    return sub


class _OverrideRequest(BaseModel):
    reason: str = Field(..., min_length=4, max_length=2000)


@router.post("/store/submissions/{submission_id}/override")
async def admin_override_store_submission(
    submission_id: str,
    body: _OverrideRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Force-publish a previously-blocked submission.

    Flips the submission to ``status='overridden'`` and the linked
    store_entities row to ``visibility_status='approved'``. Audit row
    captures who, why, and the verdict that was overridden so the next
    time this submission shows up, the trail is intact.
    """
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository

    subs = StoreSubmissionsRepository(conn)
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    if sub["status"] not in {"blocked_inline", "blocked_llm", "review_error", "pending_llm"}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot_override_status:{sub['status']}",
        )

    entity_id = sub.get("entity_id")
    if not entity_id:
        # v30+ ought to always carry entity_id. Legacy rows from the
        # pre-v30 inline-rollback design land here — refuse with a
        # message that points at the only path forward (Delete +
        # ask submitter to re-upload).
        raise HTTPException(
            status_code=409,
            detail="cannot_override_legacy_without_entity",
        )

    subs.set_override(submission_id, admin_user_id=user["id"], reason=body.reason)
    ents_repo = StoreEntitiesRepository(conn)
    ents_repo.set_visibility(entity_id, "approved")

    # Mirror the runner's deferred-promotion path. An override on a
    # v2+ edit/restore must promote the overridden version + swap the
    # on-disk live bundle, otherwise the entity stays at the prior
    # approved version and installers keep receiving stale bytes the
    # admin just told us to replace. For an initial v1 submission
    # (no prior approved) the version_no already matches — the loop
    # just no-ops and we skip promotion harmlessly.
    entity_row = ents_repo.get(entity_id) or {}
    promoted_to: Optional[int] = None
    # Look up THIS submission's version entry by submission_id, NOT
    # by hash. Hash-based lookup breaks when the user re-uploads
    # byte-identical bundles (e.g. v2 same content as v1): the loop
    # picks the FIRST history entry with that hash (always v1, n=1),
    # so target_version_no lands at 1 instead of the actual new
    # entry's n. The forward-only `target > current` guard then
    # skips the promote, leaving the entity stuck at v1. Surfaced
    # live on agnes-development.
    from app.api.store import _version_no_for_submission
    target_version_no: Optional[int] = _version_no_for_submission(
        entity_row, submission_id,
    )
    # Forward-only: refuse to promote backwards. An admin overriding a
    # stale v2 submission when v3 is already approved + live must NOT
    # demote the live bundle back to v2's bytes. Override flips the
    # row's status + visibility regardless; only the version-promote
    # is gated. Forward (target > current) is the only motion the
    # publish-gate model is designed to express.
    if (target_version_no is not None
            and target_version_no > int(entity_row.get("version_no") or 0)):
        # Atomic helper: swap live bundle first, then update the DB.
        # Eliminates the "DB promoted but live still on prior bytes"
        # window. If the helper returns None (source missing / swap
        # failed) the row's status + visibility are still flipped
        # above — admin can re-trigger via /rescan once the bundle
        # is recovered.
        from app.api.store import promote_to_version
        promoted_to = promote_to_version(
            entity_id, target_version_no, ents_repo,
        )
        if promoted_to is not None:
            # Re-read after promotion so attribution picks up the
            # new version's name/type if a rename was bundled in.
            entity_row = ents_repo.get(entity_id) or entity_row

    # Update usage-attribution rows now that the entity is live.
    update_flea_attribution(
        conn, entity_id,
        entity_row.get("type", ""),
        entity_row.get("name", ""),
    )

    AuditRepository(conn).log(
        user_id=user["id"],
        action="store.submission.overridden",
        resource=f"store_submission:{submission_id}",
        params={
            "entity_id": entity_id,
            "reason": body.reason,
            "prior_status": sub["status"],
            "prior_findings": sub.get("llm_findings"),
            "prior_inline": sub.get("inline_checks"),
            "promoted_to_version_no": promoted_to,
        },
        result="ok",
    )
    return {"ok": True, "submission_id": submission_id, "entity_id": entity_id}


@router.post("/store/submissions/{submission_id}/rescan")
async def admin_rescan_store_submission(
    submission_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-run **all** guardrail checks (inline + LLM) against the current
    bundle.

    Different from ``/retry``: rescan starts from scratch (re-runs the
    deterministic inline checks too) and is allowed regardless of
    current status. Use when check rules have changed and a previously-
    approved entity might now fail (or vice versa).

    Effects:
      * inline checks run sync; verdict written to ``inline_checks``
      * on inline fail → ``status='blocked_inline'``, entity hidden
      * on inline pass → ``status='pending_llm'``, LLM call scheduled,
        entity visibility flipped to ``pending`` until verdict lands
      * audit_log entry recorded for both outcomes — admin sees the
        rescan in the detail-page activity timeline
      * audit row recorded

    Requires the bundle to still be on disk. Inline-blocked submissions
    whose bundle was rolled back (no ``entity_id``) cannot be rescanned —
    nothing to scan.
    """
    from app.api.store import (
        _plugin_dir,
        _submission_plugin_dir,
        _version_no_for_submission,
    )
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.store_guardrails import run_inline_checks
    from src.store_guardrails.runner import (
        default_api_key_loader,
        default_model_loader,
        run_llm_review,
    )
    from app.instance_config import (
        get_guardrails_enabled,
        get_guardrails_llm_provider_ready,
    )

    subs = StoreSubmissionsRepository(conn)
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(status_code=409, detail="cannot_rescan_without_entity")

    ents = StoreEntitiesRepository(conn)
    entity = ents.get(entity_id)
    # Rescan the bundle this submission represents — not live. See the
    # equivalent fix in /retry for the full reasoning. Same fall-back
    # to live for legacy rows that never seeded a versions/v<N>/plugin/.
    target_n = _version_no_for_submission(entity or {}, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        if not plugin_dir.exists():
            plugin_dir = _plugin_dir(entity_id)
    else:
        plugin_dir = _plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    description = (entity or {}).get("description")

    inline = run_inline_checks(
        plugin_dir, type_=sub["type"], description=description,
    )

    if not inline.passed:
        # Re-failed inline. Hide the entity (was approved or pending);
        # admin can either fix the bundle (PUT to recreate) or override.
        subs.conn.execute(
            "UPDATE store_submissions SET inline_checks = ?, llm_findings = NULL, "
            "status = 'blocked_inline', updated_at = current_timestamp "
            "WHERE id = ?",
            [__import__("json").dumps(inline.to_response_dict()), submission_id],
        )
        ents.set_visibility(entity_id, "hidden")
        AuditRepository(conn).log(
            user_id=user["id"],
            action="store.submission.rescan",
            resource=f"store_submission:{submission_id}",
            params={"entity_id": entity_id, "outcome": "blocked_inline"},
        )
        return {"ok": True, "submission_id": submission_id, "status": "blocked_inline"}

    # Inline passes. Three-state matrix:
    #   - intent False           → auto-approve (operator opt-out)
    #   - intent True + ready    → pending_llm, schedule LLM
    #   - intent True + not-ready → pending_llm, DO NOT schedule (admin
    #     retries from the same endpoint after providing credentials)
    guardrails_enabled = get_guardrails_enabled()
    provider_ready = get_guardrails_llm_provider_ready()
    hold_for_review = guardrails_enabled
    schedule_async_llm = guardrails_enabled and provider_ready
    guardrails_on = hold_for_review  # retained for audit-log compat
    new_status = "pending_llm" if hold_for_review else "approved"
    subs.conn.execute(
        "UPDATE store_submissions SET inline_checks = ?, llm_findings = NULL, "
        "status = ?, updated_at = current_timestamp "
        "WHERE id = ?",
        [__import__("json").dumps(inline.to_response_dict()), new_status, submission_id],
    )
    if hold_for_review:
        ents.set_visibility(entity_id, "pending")
    else:
        ents.set_visibility(entity_id, "approved")
        # Guardrails explicitly disabled — immediately live. Promote
        # the rescanned submission's version forward (same atomic
        # helper the create / update / restore inline-promote paths
        # use). Pre-fix this branch flipped visibility but never
        # called promote_to_version, so a rescan that re-approved a
        # non-current v2+ left the entity stuck at the prior version.
        # Surfaced by adversarial review of PR #330.
        from app.api.store import promote_to_version
        entity_row = ents.get(entity_id) or {}
        if target_n is not None and target_n > int(entity_row.get("version_no") or 0):
            promote_to_version(entity_id, target_n, ents)
            entity_row = ents.get(entity_id) or entity_row
        update_flea_attribution(
            conn, entity_id,
            entity_row.get("type", ""),
            entity_row.get("name", ""),
        )
    AuditRepository(conn).log(
        user_id=user["id"],
        action="store.submission.rescan",
        resource=f"store_submission:{submission_id}",
        params={"entity_id": entity_id, "outcome": new_status,
                "guardrails_enabled": guardrails_on,
                "provider_ready": provider_ready},
    )
    if schedule_async_llm:
        background.add_task(
            run_llm_review,
            submission_id,
            plugin_dir=plugin_dir,
            conn_factory=get_system_db,
            api_key_loader=default_api_key_loader,
            model_loader=default_model_loader,
        )
    return {"ok": True, "submission_id": submission_id, "status": new_status}


@router.post("/store/submissions/{submission_id}/retry")
async def admin_retry_store_submission(
    submission_id: str,
    background: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-queue the LLM review for a submission.

    Eligible statuses:
      * ``review_error`` — LLM call failed, admin retrying after the
        underlying issue (rate limit, timeout, transient outage) clears.
      * ``blocked_llm`` — admin disagrees with the prior verdict; rerun
        from a clean slate (review rules may have shifted since).
      * ``pending_llm`` — submission was held when the LLM provider had
        no credentials in env (fail-CLOSED matrix: intent True + not
        ready). Admin sets the key and re-fires from here.

    Only valid when the original submission's plugin tree is still on
    disk — for inline-blocked rows the bundle was deleted at POST time.
    """
    from app.api.store import (
        _plugin_dir,
        _submission_plugin_dir,
        _version_no_for_submission,
    )
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.store_guardrails.runner import (
        default_api_key_loader,
        default_model_loader,
        run_llm_review,
    )

    subs = StoreSubmissionsRepository(conn)
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    if sub["status"] not in {"review_error", "blocked_llm", "pending_llm"}:
        raise HTTPException(
            status_code=409, detail=f"cannot_retry_status:{sub['status']}",
        )
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(
            status_code=409, detail="cannot_retry_without_entity",
        )

    # Review the STAGED version's bytes — not live. For a v2+ edit
    # held at pending_llm or blocked_llm, live `plugin/` still holds
    # the prior approved version. Reviewing live would produce a
    # verdict against the wrong bytes; the runner's hash-match
    # promotion would then advance the entity to staged bytes that
    # were never actually reviewed.
    ent = StoreEntitiesRepository(conn).get(entity_id) or {}
    target_n = _version_no_for_submission(ent, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        # Fall back to live for legacy pre-v37 rows where the version
        # dir was never seeded.
        if not plugin_dir.exists():
            plugin_dir = _plugin_dir(entity_id)
    else:
        plugin_dir = _plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    subs.update_status(submission_id, status="pending_llm")
    AuditRepository(conn).log(
        user_id=user["id"],
        action="store.submission.retry",
        resource=f"store_submission:{submission_id}",
        params={"entity_id": entity_id},
    )
    background.add_task(
        run_llm_review,
        submission_id,
        plugin_dir=plugin_dir,
        conn_factory=get_system_db,
        api_key_loader=default_api_key_loader,
        model_loader=default_model_loader,
    )
    return {"ok": True, "submission_id": submission_id, "status": "pending_llm"}


@router.delete("/store/submissions/{submission_id}")
async def admin_delete_store_submission(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard-delete a submission record + its linked bundle (if any).

    Use this for spam / accidental uploads after override-publish is the
    wrong call. The audit_log row preserves what was deleted in case
    triage needs the evidence trail later.
    """
    from app.api.store import _entity_dir
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    subs = StoreSubmissionsRepository(conn)
    sub = subs.get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")

    entity_id = sub.get("entity_id")
    if entity_id:
        UserStoreInstallsRepository(conn).delete_all_for_entity(entity_id)
        StoreEntitiesRepository(conn).delete(entity_id)
        _shutil.rmtree(_entity_dir(entity_id), ignore_errors=True)
    conn.execute("DELETE FROM store_submissions WHERE id = ?", [submission_id])

    AuditRepository(conn).log(
        user_id=user["id"],
        action="store.submission.deleted",
        resource=f"store_submission:{submission_id}",
        params={
            "entity_id": entity_id,
            "submitter_id": sub.get("submitter_id"),
            "name": sub.get("name"),
            "status": sub.get("status"),
        },
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# v30: download blocked bundle for forensic inspection
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse


@router.get("/store/submissions/{submission_id}/bundle.zip")
async def admin_download_store_submission_bundle(
    submission_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream the on-disk bundle as a fresh ZIP for admin inspection.

    Required by the forensic use case: admin needs to inspect what a
    submitter actually tried to upload (not just the verdict). Bundle
    must still be on disk — TTL purge nulls ``entity_id`` and removes
    the directory, in which case this returns 410.
    """
    import io as _io
    import zipfile as _zipfile
    from pathlib import Path as _P
    from app.api.store import (
        _plugin_dir as _sp_plugin_dir,
        _submission_plugin_dir,
        _version_no_for_submission,
    )

    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository

    sub = StoreSubmissionsRepository(conn).get(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission_not_found")
    entity_id = sub.get("entity_id")
    if not entity_id:
        raise HTTPException(status_code=410, detail="bundle_purged_or_missing")

    # Resolve the STAGED bundle this submission represents, not live.
    # Under deferred promotion, live `plugin/` holds the prior approved
    # version — so for a blocked v2 row, live shows v1's safe bytes
    # while the staged v2 bytes (the actual risky upload the admin is
    # reviewing) sit in `versions/v2/plugin/`. Falls back to live for
    # legacy rows that never seeded a versions/ dir.
    ent = StoreEntitiesRepository(conn).get(entity_id) or {}
    target_n = _version_no_for_submission(ent, submission_id)
    if target_n is not None:
        plugin_dir = _submission_plugin_dir(entity_id, target_n)
        if not plugin_dir.exists():
            plugin_dir = _sp_plugin_dir(entity_id)
    else:
        plugin_dir = _sp_plugin_dir(entity_id)
    if not plugin_dir.exists():
        raise HTTPException(status_code=410, detail="bundle_missing")

    AuditRepository(conn).log(
        user_id=user["id"],
        action="store.submission.bundle_downloaded",
        resource=f"store_submission:{submission_id}",
        params={"entity_id": entity_id, "name": sub.get("name")},
    )

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(_P(plugin_dir).rglob("*")):
            if not f.is_file():
                continue
            arcname = f.relative_to(plugin_dir).as_posix()
            zf.write(f, arcname)
    buf.seek(0)

    safe_name = (sub.get("name") or "bundle").replace("/", "_")
    filename = f"{safe_name}-{submission_id[:8]}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# v30: scheduled TTL purge of blocked bundle bytes
# ---------------------------------------------------------------------------


@router.post("/run-blocked-purge")
async def run_blocked_purge(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the TTL purge of blocked bundle bytes.

    Wraps :func:`src.store_guardrails.purge.purge_blocked_bundles`. The
    scheduler service hits this endpoint daily (under
    ``SCHEDULER_API_TOKEN`` like the corporate-memory + verification
    jobs); admins can also run it on demand from the UI.
    """
    from app.instance_config import get_guardrails_blocked_bundle_ttl_days
    from src.store_guardrails.purge import purge_blocked_bundles

    ttl = get_guardrails_blocked_bundle_ttl_days()
    result = purge_blocked_bundles(conn, ttl_days=ttl)

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="run_blocked_purge",
        resource="job:store-blocked-purge",
        params={"ttl_days": ttl, "purged": result.get("purged", 0),
                "skipped": result.get("skipped", False)},
    )
    return {"ok": True, "details": result}


@router.post("/run-reap-stuck-reviews")
async def run_reap_stuck_reviews(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Trigger the stuck-review reaper.

    Wraps :func:`src.store_guardrails.reaper.reap_stuck_llm_reviews`.
    The scheduler hits this every 15 minutes; admins can run it on
    demand if a worker crash is suspected. Flips any
    ``status='pending_llm'`` row older than the configured grace to
    ``review_error`` so the queue stops growing indefinitely.
    """
    from app.instance_config import get_guardrails_stuck_review_grace_seconds
    from src.store_guardrails.reaper import reap_stuck_llm_reviews

    grace = get_guardrails_stuck_review_grace_seconds()
    result = reap_stuck_llm_reviews(conn, grace_seconds=grace)

    AuditRepository(conn).log(
        user_id=user.get("id"),
        action="run_reap_stuck_reviews",
        resource="job:store-reap-stuck-reviews",
        params={"grace_seconds": grace,
                "reaped": result.get("reaped", 0),
                "skipped": result.get("skipped", False)},
    )
    return {"ok": True, "details": result}
