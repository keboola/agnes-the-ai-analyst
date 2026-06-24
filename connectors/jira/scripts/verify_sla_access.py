#!/usr/bin/env python3
"""
Jira SLA access preflight — prove (against the live API) that the single
primary token can read SLA custom fields, and discover the instance's SLA
field ids.

This is the "run it, don't guess" check for the single-token SLA setup.
SLA fields are ordinary issue custom fields, readable via the regular issue
REST API by an account holding a JSM Agent licence — the same primary
credentials used everywhere else (JIRA_EMAIL / JIRA_API_TOKEN). Field ids are
instance-specific and have NO defaults; discover them with --list-fields, set
the two you want, then verify against a real ticket.

Usage:
    # Discover the SLA custom fields on your instance (id + name):
    python -m connectors.jira.scripts.verify_sla_access --list-fields

    # Verify the configured fields are readable on a real ticket:
    python -m connectors.jira.scripts.verify_sla_access --issue SUPPORT-123

Environment variables (loaded from .env):
    JIRA_DOMAIN - Atlassian site host (e.g. your-org.atlassian.net)
    JIRA_EMAIL - Email for API authentication
    JIRA_API_TOKEN - Primary API token (account needs a JSM Agent licence)
    JIRA_CLOUD_ID - Optional; set only for a scoped token (gateway base URL)
    JIRA_REFRESH_FIELDS - field ids to refresh (field_id or field_id:column, comma-separated)

Secret hygiene: token/email values are never printed — only field ids,
classifications, URLs and HTTP status appear in the output.

Exit code: 0 when at least one URL form returns valid SLA for a configured
field; 1 otherwise (or when unconfigured).
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Add project root to sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.jira.service import refresh_fields  # noqa: E402


def classify_field(value: object) -> str:
    """Classify a fetched field value: present / permission-error / null."""
    if isinstance(value, dict) and "errorMessage" in value:
        return "permission-error"
    if value is None:
        return "null"
    return "present"


def build_base_urls(domain: str, cloud_id: str) -> list[tuple[str, str]]:
    """Return the REST base URLs to try, labelled.

    Domain URL always (classic token); the api.atlassian.com gateway too when
    a cloud id is set (required for a scoped token). Both use the same auth.
    """
    urls = [("domain", f"https://{domain}/rest/api/3")]
    if cloud_id:
        urls.append(("gateway", f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"))
    return urls


def list_custom_fields(base_url: str, auth: tuple[str, str]) -> list[dict]:
    """Return [{id, name, type}] for the instance's custom fields."""
    url = f"{base_url}/field"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, auth=auth, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        return []
    out = []
    for field in resp.json():
        schema = field.get("schema") or {}
        custom_type = schema.get("custom")
        if custom_type:
            out.append({"id": field["id"], "name": field.get("name"), "type": custom_type})
    return out


def check_issue(base_url: str, auth: tuple[str, str], issue_key: str, field_ids: list[str]) -> dict:
    """Fetch the configured SLA fields for one issue and classify each.

    Returns {"status": int, "fields": {field_id: classification}}; on a
    non-200 the fields map is empty and "error" is True.
    """
    url = f"{base_url}/issue/{issue_key}"
    params = {"fields": ",".join(field_ids)}
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, auth=auth, params=params, headers={"Accept": "application/json"})
    except httpx.RequestError as e:
        return {"status": None, "fields": {}, "error": f"{type(e).__name__}"}
    if resp.status_code != 200:
        return {"status": resp.status_code, "fields": {}, "error": True}
    fields = resp.json().get("fields", {})
    return {
        "status": resp.status_code,
        "fields": {fid: classify_field(fields.get(fid)) for fid in field_ids},
    }


def run(issue_key: str | None = None, list_fields: bool = False) -> dict:
    """Run the preflight. Reads env at call time, prints a human report, and
    returns a structured result dict. Never prints token/email values."""
    domain = os.environ.get("JIRA_DOMAIN", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    cloud_id = os.environ.get("JIRA_CLOUD_ID", "")

    if not all([domain, email, token]):
        print("✗ Jira not configured — set JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN.")
        return {"ok": False, "reason": "jira_not_configured"}

    auth = (email, token)
    base_urls = build_base_urls(domain, cloud_id)

    if list_fields:
        print(f"Discovering custom fields on {domain} ...")
        discovered: list[dict] = []
        for label, base in base_urls:
            try:
                fields = list_custom_fields(base, auth)
            except httpx.RequestError as e:
                print(f"  [{label}] request error: {type(e).__name__}")
                continue
            if fields:
                for f in fields:
                    print(f"  {f['id']}  {f['name']}  [{f['type']}]  (via {label})")
                discovered = fields
                break
        if not discovered:
            print("  No custom fields found (token may lack access, or none are defined).")
        else:
            print("\nSet the ones you want in JIRA_REFRESH_FIELDS (field_id or field_id:column).")
        return {"ok": bool(discovered), "mode": "list", "fields": discovered}

    field_ids = [fid for fid, _ in refresh_fields()]
    if not field_ids:
        print("✗ No refresh fields configured. Run with --list-fields to discover them.")
        return {"ok": False, "reason": "no_refresh_fields_configured"}
    if not issue_key:
        print("✗ Provide --issue KEY (a ticket that has these fields) to verify.")
        return {"ok": False, "reason": "no_issue"}

    print(f"Verifying field access for {issue_key} (fields: {', '.join(field_ids)})")
    results = []
    any_present = False
    for label, base in base_urls:
        res = check_issue(base, auth, issue_key, field_ids)
        results.append({"url_label": label, **res})
        print(f"  [{label}] HTTP {res.get('status')}")
        for fid, verdict in res.get("fields", {}).items():
            print(f"      {fid}: {verdict}")
        if any(c == "present" for c in res.get("fields", {}).values()):
            any_present = True

    if any_present:
        print("\n✓ PASS — the token reads the configured fields through the API.")
    else:
        print(
            "\n✗ FAIL — no field value returned. Check the account's read permission "
            "(e.g. a JSM Agent licence for SLA fields), the field ids (--list-fields), "
            "or set JIRA_CLOUD_ID for a scoped token."
        )
    return {"ok": any_present, "mode": "verify", "results": results}


def _load_env() -> None:
    """Load .env from the usual locations (mirrors the other Jira scripts)."""
    env_paths = [
        Path(os.environ["AGNES_ENV_FILE"]) if os.environ.get("AGNES_ENV_FILE") else None,
        Path.cwd() / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for env_path in [p for p in env_paths if p is not None]:
        if env_path.exists():
            load_dotenv(env_path)
            break


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(
        description="Verify single-token Jira SLA access and discover SLA field ids",
    )
    parser.add_argument("--issue", help="Issue key to verify (e.g. SUPPORT-123)")
    parser.add_argument(
        "--list-fields",
        action="store_true",
        help="List the instance's SLA custom fields (id + name) instead of verifying",
    )
    args = parser.parse_args()

    report = run(issue_key=args.issue, list_fields=args.list_fields)
    sys.exit(0 if report.get("ok") else 1)


if __name__ == "__main__":
    main()
