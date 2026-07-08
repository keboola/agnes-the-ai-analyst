> New: [docs/PLATFORM_SETUP.md](./PLATFORM_SETUP.md) is the consolidated operator playbook. This doc covers a focused subset; check the playbook first.

# Headless / CI usage

For unattended clients (CI, cron, Claude Code), authenticate with a Personal Access Token (PAT) rather than an interactive session.

## Create a PAT

**Via UI:** sign in, open `/me/profile`, create a token. Copy the raw value — it is shown exactly once.

**Via CLI (requires an interactive session):**

```bash
agnes auth token create --name "github-actions" --ttl 365d --raw
```

The `--raw` flag prints only the token, suitable for piping into a secret store.

## Use the PAT

Set the `AGNES_TOKEN` env var:

```bash
export AGNES_TOKEN=<your-token>
agnes query "SELECT 1"
```

### GitHub Actions example

```yaml
- name: Sync data
  env:
    AGNES_TOKEN: ${{ secrets.AGNES_TOKEN }}
    AGNES_SERVER: https://agnes.example.com
  run: |
    uv tool install "$AGNES_SERVER/cli/wheel/agnes.whl"
    agnes pull
```

## Revoke

```bash
agnes auth token list
agnes auth token revoke <id|prefix|name>
```

Or from `/me/profile` → Revoke.

## Renewal (interactive analysts)

`agnes auth login` (the browser loopback flow, not this doc's headless
`--ttl` path) mints a 90-day PAT. Rather than a refresh-token grant, the CLI
proactively reminds analysts to re-mint before that PAT expires: any
non-quiet command prints a one-line stderr nudge once the stored token is
within `AGNES_TOKEN_RENEW_DAYS` (default 7; `0` disables) of `exp`, at most
once per day. `agnes auth whoami` always shows the current status
(`Token: valid until <date> (<N> days)`). Renew by simply re-running:

```bash
agnes auth login
```

which overwrites the stored token in place. See [`docs/RBAC.md`](./RBAC.md#pat-lifetime--renewal)
for the rationale behind this model over a refresh-token grant.

For unattended/headless clients using `--ttl`-minted PATs (this doc's main
path), there is no nudge — rotate on your own schedule (CI secret rotation,
cron) since there's no interactive terminal to print a warning to.
