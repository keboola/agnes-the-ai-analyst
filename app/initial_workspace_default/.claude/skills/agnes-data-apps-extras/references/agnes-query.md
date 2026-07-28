# Reading Agnes data from a hosted app — `agnesQuery.ts`

The baked scaffold (`scaffolds/nodejs-dashboard/server/agnesQuery.ts`) is the
**only** sanctioned way a hosted app reads Agnes data. Never hand-roll a
fetch to `/api/query` elsewhere in the app, and never hardcode a token —
both env vars below are injected by the Agnes apps-runner at container start,
scoped to the app's owner.

## Environment

- `AGNES_URL` — the Agnes server's internal base URL.
- `AGNES_TOKEN` — an owner-scoped bearer token. The app's REST calls run
  under the token of whoever owns the app (see `docs/DEPLOYMENT.md` → *Data
  apps* → "Data access is owner-inherited") — never under the viewer's own
  identity, and never with elevated rights the owner doesn't already have.

## Helper shape

```typescript
// server/agnesQuery.ts
export async function runQuery(sql: string): Promise<Record<string, unknown>[]> {
  const res = await fetch(`${process.env.AGNES_URL}/api/query`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.AGNES_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ sql }),
  });
  if (!res.ok) {
    throw new Error(`agnes query failed: ${res.status}`);
  }
  const { rows } = await res.json();
  return rows;
}
```

Catalog/table lookups (what tables/columns exist, before writing a query)
go against the same base URL's catalog endpoints, with the same bearer
token — mirror `runQuery`'s error handling.

## Rules for the query itself

- Every query the app runs is subject to the owner's own RBAC grants — the
  app can only ever see what its owner is allowed to see. If a query 403s,
  that's a real access boundary, not a bug to work around.
- Never embed secrets, other users' data, or PII in client-side code —
  `agnesQuery.ts` runs server-side (Express), and the SPA only ever talks to
  the app's own `/api/data`-style routes, never directly to Agnes.
- Prefer aggregate/paginated queries over `SELECT *` for anything backing a
  dashboard chart — the same discovery-first discipline the root workspace
  `CLAUDE.md` teaches for `agnes query` applies here too.
