# Agnes data workspace

You are an analyst assistant working in this Agnes data workspace. The data you
can access is **not** stored as files in this directory — it lives behind the
`agnes` CLI (served from the Agnes server, filtered to what your account is
allowed to see). Reach for `agnes` for any question about the data: never
answer a data question by listing or reading local files, and never claim there
is no data without first running `agnes catalog`.

## Querying data

1. `agnes catalog` — list the tables you can query (run this first). Add
   `--metrics` to list canonical business-metric definitions.
2. `agnes schema <table>` — column names and types.
3. `agnes describe <table> -n 5` — a few sample rows, to see real values.
4. Run a query:
   - `agnes query "<SQL>"` — runs against your local synced copy.
   - `agnes query --remote "<SQL>"` — runs server-side and returns rows with no
     download. Use this when nothing has been pulled locally yet, or for large
     tables — it queries the same RBAC-filtered views without copying data down.

Each table's `query_mode` (shown by `agnes catalog`) tells you whether it is
local (synced) or remote. Before computing a business metric, look up its
canonical definition with `agnes catalog --metrics` and adapt that SQL rather
than inventing your own.
