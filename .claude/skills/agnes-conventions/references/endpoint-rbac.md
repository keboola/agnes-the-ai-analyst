# Playbook: new REST endpoint + RBAC gate

## Files

1. `app/api/<feature>.py` — define `router = APIRouter(prefix="/api/<feature>", tags=["<feature>"])`.
2. `app/main.py` — import + `app.include_router(<feature>_router)` (imports block
   ~`app/main.py:211`, include block ~`app/main.py:1130`).

## Choose the gate (both in `app/auth/access.py`)

- **`require_admin`** (`app/auth/access.py:235`) — app-level mutations only admins
  may do (create/delete, run sync, configure). Denies session-principal tokens.
  Use: `_user: dict = Depends(require_admin)`. Example: `app/api/admin_bigquery_test.py:33`.
- **`require_resource_access(ResourceType.X, "{path_param}")`**
  (`app/auth/access.py:262`) — entity-scoped access. It's a FACTORY returning a
  dependency; the 2nd arg is a format string resolved against the route's
  path params at request time; admins short-circuit. Examples:
  `app/api/marketplace.py:1603` (interpolated), `app/api/chat.py:27` (fixed id).

```python
from app.auth.access import require_resource_access
from app.resource_types import ResourceType

@router.get("/{plugin_id}")
async def detail(plugin_id: str,
    _u: dict = Depends(require_resource_access(ResourceType.MARKETPLACE_PLUGIN, "{plugin_id}"))):
    ...
```

## New resource type? (one file: `app/resource_types.py`)

1. Add a `ResourceType` StrEnum member (`app/resource_types.py:36`) — value is
   persisted in `resource_grants.resource_type`; never rename an existing member.
2. Write a `list_blocks` delegate `(conn) -> list[Block]` returning
   `[{id, name, items:[{resource_id, name, ...}]}]` where `resource_id` matches
   what's stored in grants.
3. Register a `ResourceTypeSpec` in `RESOURCE_TYPES` (`app/resource_types.py:424`)
   with `key`, `display_name`, `description`, `id_format`, `list_blocks`.
   No DB migration — the admin `/access` page picks it up automatically.

## Coverage — CLI + MCP (required)

A new `/api/*` endpoint is not done when it returns 200. It must also be:

- **CLI:** add a subcommand under `cli/commands/` calling it via
  `api_get/post/put/delete` from `cli/client.py` (1:1 mapping, see
  `cli/commands/admin_data_package.py:1`). State-changing? add a parity case to
  `tests/test_cli_api_parity.py`.
- **MCP:** a static `@mcp.tool()` in `cli/mcp/server.py`, or a `tool_registry`
  passthrough row (`app/api/mcp/tools_generator.py:132`).
- Refresh `tests/snapshots/openapi.json`: `make update-openapi-snapshot`.

Exempt: health checks, webhooks, OAuth callbacks, internal/SSE. Two structural
gates enforce this: `tests/test_documentation_api_triple_surface.py` (new endpoints
must be classified triple-surface or exempt) and `tests/test_api_docs_coverage.py`
(docs). See CONTRIBUTING.md → API coverage.

## Steps

1. TDD: write an API test (auth'd + unauth'd) asserting the gate (403 without
   access, 200 with).
2. Create the router, register it in `app/main.py`, add the gate, (new resource
   type if needed).
3. Green the test.

## Anchors

- gates: `app/auth/access.py:235`, `app/auth/access.py:262`
- gated examples: `app/api/admin_bigquery_test.py:33`, `app/api/marketplace.py:1603`, `app/api/chat.py:27`
- resource types: `app/resource_types.py:36`, `app/resource_types.py:424`
- router registration: `app/main.py:211` (imports), `app/main.py:1130` (includes)
