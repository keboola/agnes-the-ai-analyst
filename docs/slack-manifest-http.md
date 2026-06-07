# Slack App manifest — HTTP transport (default)

Paste this at api.slack.com/apps → "Create New App" → "From a manifest".
Replace `<your-host>` with the public hostname of your Agnes instance
(e.g. `agnes.example.com`). This is the default transport — Slack delivers
events over an HTTPS webhook to your public endpoint.

```yaml
display_information:
  name: Agnes
  description: Ask Agnes data questions from Slack
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: Agnes
    always_online: false
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    request_url: "https://<your-host>/api/slack/events"
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: http` in `instance.yaml` (or `SLACK_TRANSPORT=http`,
  or leave unset — `http` is the default).
- These tokens may instead be set from the admin UI (`/admin/server-config` → Slack bot secrets), stored encrypted in the vault (`AGNES_VAULT_KEY` required). Environment variables, if present, take precedence.
