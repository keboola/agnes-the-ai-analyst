/**
 * Thin client for the Agnes REST API, in the shape of the upstream
 * `kbcQuery.ts` helper KAI's baked scaffold ships: reads AGNES_URL/AGNES_TOKEN
 * from the environment (injected by the Agnes control plane as an
 * owner-scoped service token, rotated on every deploy — see
 * docs/superpowers/specs/2026-07-21-data-apps-design.md §8) and calls the
 * normal Agnes REST endpoints. No SDK, no special sandbox wiring — this is
 * literally the same API the `agnes` CLI and MCP tools call.
 */

const AGNES_URL = process.env.AGNES_URL ?? "http://app:8000";
const AGNES_TOKEN = process.env.AGNES_TOKEN ?? "";

function authHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${AGNES_TOKEN}`,
  };
}

async function agnesFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(`${AGNES_URL}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Agnes API ${path} -> ${res.status}: ${body}`);
  }
  return res;
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
}

/**
 * Run a read-only SQL query against Agnes, scoped to the app owner's RBAC
 * grants — exactly `POST /api/query` (the same endpoint the CLI/MCP use).
 */
export async function runQuery(sql: string, limit = 1000): Promise<QueryResult> {
  const res = await agnesFetch("/api/query", {
    method: "POST",
    body: JSON.stringify({ sql, limit }),
  });
  return (await res.json()) as QueryResult;
}

export interface CatalogTable {
  id: string;
  name: string;
  description?: string | null;
  source_type?: string | null;
  sync_strategy?: string | null;
  query_mode: string;
}

/** List the tables this app's owner can see — `GET /api/catalog/tables`. */
export async function listTables(): Promise<CatalogTable[]> {
  const res = await agnesFetch("/api/catalog/tables");
  const body = (await res.json()) as { tables: CatalogTable[]; count: number };
  return body.tables;
}

/** Look up a single table's profile (row counts, column stats) by name. */
export async function getTableProfile(tableName: string): Promise<Record<string, unknown>> {
  const res = await agnesFetch(`/api/catalog/profile/${encodeURIComponent(tableName)}`);
  return (await res.json()) as Record<string, unknown>;
}
