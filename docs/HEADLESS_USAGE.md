# Headless / CI usage

For unattended clients (CI, cron, Claude Code), authenticate with a Personal Access Token (PAT) rather than an interactive session.

## Create a PAT

**Via UI:** sign in, open `/tokens`, create a token. Copy the raw value — it is shown exactly once.

**Via CLI (requires an interactive session):**

```bash
da auth token create --name "github-actions" --ttl 365d --raw
```

The `--raw` flag prints only the token, suitable for piping into a secret store.

## Use the PAT

Set the `DA_TOKEN` env var:

```bash
export DA_TOKEN=<your-token>
da query "SELECT 1"
```

### GitHub Actions example

```yaml
- name: Sync data
  env:
    DA_TOKEN: ${{ secrets.AGNES_TOKEN }}
    DA_SERVER: https://agnes.example.com
  run: |
    pip install data-analyst
    da sync --all
```

## Revoke

```bash
da auth token list
da auth token revoke <id|prefix|name>
```

Or from `/tokens` → Revoke.
