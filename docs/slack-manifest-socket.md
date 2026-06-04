# Slack App manifest — Socket Mode transport (optional)

Use this when your Agnes instance has no publicly reachable webhook URL.
Slack delivers events over an outbound WebSocket instead of an HTTPS
webhook, so there is **no `request_url`** — that's the whole point of the
two-stanza split (a stale `request_url` is a common foot-gun).

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
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

After creating the app, generate an **app-level token** (`xapp-…`) with the
`connections:write` scope under "Basic Information → App-Level Tokens".

## Required environment

- `SLACK_BOT_TOKEN` (`xoxb-…`)
- `SLACK_APP_TOKEN` (`xapp-…`, with `connections:write`)
- `SLACK_SIGNING_SECRET`
- `chat.slack.transport: socket` in `instance.yaml` (or `SLACK_TRANSPORT=socket`)
- Install the optional dependency: `pip install '.[slack-socket]'`

## Constraints

- **Single worker only.** Socket Mode requires `UVICORN_WORKERS=1` — multiple
  workers each open a WS and fracture event dedup. Agnes refuses to start the
  WS otherwise (logs the reason, disables Slack, never crashes).
- All gates are fail-closed: a missing/mis-prefixed token pair or a missing
  `slack-socket` extra logs the reason and leaves Slack HTTP-only.
