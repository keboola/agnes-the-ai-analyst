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
