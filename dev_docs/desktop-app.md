# Data Analyst - macOS Desktop App

Native macOS menu bar application for receiving real-time notifications from the data broker server.

## Architecture

```
notify-runner (cron, every 5min)
      |
      v
  bot.sock (existing)          ws.sock (new)
      |                            |
      v                            v
  Telegram API              WebSocket Gateway
  (existing)                  (ws-gateway.service)
                                   |
                            nginx wss:// proxy
                            /ws/notifications
                                   |
                              macOS App (Swift)
```

Notifications are delivered in parallel to both Telegram and the desktop app.
The WebSocket gateway (`services/ws_gateway/`) runs as a separate systemd service alongside the existing Telegram bot.

## Requirements

- macOS 14.0+ (Sonoma or later)
- Xcode 16+ (for building from source)
- Active team member account (@your-domain.com Google SSO)

## Building

```bash
cd macos-app/DataAnalyst
xcodebuild -scheme DataAnalyst -configuration Debug build
```

The built app is at:
```
~/Library/Developer/Xcode/DerivedData/DataAnalyst-*/Build/Products/Debug/DataAnalyst.app
```

To run:
```bash
open ~/Library/Developer/Xcode/DerivedData/DataAnalyst-*/Build/Products/Debug/DataAnalyst.app
```

## Authentication Flow

1. User clicks **Sign In** in the menu bar popover
2. Browser opens `https://your-instance.example.com/desktop/link`
3. User authenticates via Google SSO (if not already logged in)
4. User clicks **Authorize Desktop App**
5. Webapp generates a JWT token (HS256, 30-day expiry) and redirects to `data-analyst://auth?token=eyJ...`
6. macOS app catches the custom URL scheme, stores the JWT in Keychain
7. App connects to WebSocket gateway, sends `{"type":"auth","token":"..."}`
8. Gateway validates JWT and confirms with `{"type":"auth_ok","username":"..."}`

Token refresh: the app can call `POST /api/desktop/refresh` with `Authorization: Bearer <token>` before expiry. Tokens expired within 7 days are still refreshable.

## WebSocket Protocol

Server: `wss://your-instance.example.com/ws/notifications`

```
Client -> Server: {"type":"auth","token":"eyJ..."}
Server -> Client: {"type":"auth_ok","username":"john"}
Server -> Client: {"type":"notification","id":"uuid","title":"Revenue Drop","message":"...","image_url":"/api/notifications/images/abc.png","script":"revenue_check","timestamp":"2026-01-30T10:00:00Z"}
Server -> Client: {"type":"ping"}
Client -> Server: {"type":"pong"}
```

- Heartbeat: server sends ping every 30s, disconnects after 3 missed pongs
- Auto-reconnect: exponential backoff from 1s to 30s on disconnect
- Client pong resets the missed counter by restarting the heartbeat task server-side

## App Features

- **Menu bar icon**: bell icon with badge when unread notifications exist
- **Notification list**: last 50 notifications, newest first
- **Detail view**: full message text, chart image (if available), script name
- **Native notifications**: macOS notification center banners
- **Open in Claude Code**: button to launch terminal with Claude Code for investigation
- **Settings**: connection status, account info, sign out
- **Persistence**: notifications stored in UserDefaults between launches
- **Keychain**: JWT token stored securely in macOS Keychain
- **Run scripts**: execute notification scripts on-demand via webapp API, results arrive as WS notifications
- **Logging**: `os.log` with subsystem `com.dataanalyst`, category `WebSocket` -- view with `log show --predicate 'subsystem == "com.dataanalyst"' --last 5m --info`

## Server Components

### WebSocket Gateway (`services/ws_gateway/`)

- `gateway.py` - main asyncio+aiohttp server with two listeners:
  - TCP WebSocket on `127.0.0.1:8765` (proxied by nginx)
  - Unix socket HTTP on `/run/ws-gateway/ws.sock` (internal dispatch, mode `0770`)
- `auth.py` - JWT validation (HS256, same secret as webapp)
- `config.py` - configuration from environment variables

Systemd service: `ws-gateway.service` (User=deploy, Group=data-ops)

The dispatch socket is set to `0770` after creation, allowing group members (`data-ops`: www-data, deploy) to connect. The `RuntimeDirectoryMode=0755` controls the parent directory `/run/ws-gateway/`.

### Desktop Auth (`webapp/desktop_auth.py`)

- `GET /desktop/link` - authorization page (requires Google SSO login)
- `POST /api/desktop/authorize` - generates JWT, returns redirect URL, marks user as linked
- `POST /api/desktop/refresh` - refreshes existing JWT
- `POST /api/desktop/unlink` - unlinks desktop app (requires SSO login)

**Link state tracking:** Desktop app link status is persisted in `/data/notifications/desktop_users.json` (analogous to `telegram_users.json`). The dashboard shows Linked/Not linked badge and an Unlink button. Link state is recorded:
- Explicitly when user authorizes via `/api/desktop/authorize`
- Automatically when the app makes any authenticated API call (scripts, refresh) via `require_desktop_auth()`

Unlinking removes the entry from `desktop_users.json` but does not invalidate the existing JWT token — the app continues to work until the token expires.

### Notification Images (`webapp/notification_images.py`)

- `GET /api/notifications/images/<filename>` - serves chart PNGs from /tmp/

### Notification Dispatch

Both `server/bin/notify-runner` and `services/telegram_bot/bot.py` dispatch notifications to the WebSocket gateway alongside Telegram delivery. The webapp API (`POST /api/desktop/scripts/run`) also dispatches via `services/telegram_bot/dispatch.py`. The dispatch is fire-and-forget - if the gateway is not running, it silently skips.

Script execution (from webapp API and Telegram bot) uses the `notify-scripts` helper:
```bash
sudo -u <username> /usr/local/bin/notify-scripts run <script.py>
```
This avoids direct filesystem access to user home directories (which are `750`).

## Configuration

### Server Environment Variables

| Variable | Required by | Description |
|----------|------------|-------------|
| `DESKTOP_JWT_SECRET` | ws-gateway, webapp | Shared secret for JWT signing (HS256) |

Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`

Must be added to GitHub Actions secrets and present in both:
- `/opt/data-analyst/.env` (webapp reads this)
- `/opt/data-analyst/repo/.env` (ws-gateway reads this)

### Nginx

WebSocket proxy location in `server/webapp-nginx.conf`:

```nginx
location /ws/notifications {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
```

## Project Structure

```
macos-app/DataAnalyst/
  DataAnalyst.xcodeproj/
  DataAnalyst/
    App/
      DataAnalystApp.swift       # @main, MenuBarExtra
      AppDelegate.swift              # URL scheme handler (data-analyst://)
    Core/
      Config.swift                   # URLs, timeouts, keychain names
      KeychainService.swift          # JWT storage in Keychain
      WebSocketManager.swift         # URLSessionWebSocketTask + reconnect
      NotificationManager.swift      # UNUserNotificationCenter
      NotificationStore.swift        # In-memory + UserDefaults persistence
    Models/
      AnalystNotification.swift      # Codable notification model
      AuthState.swift                # Auth state manager with JWT decoding
    Views/
      MenuBarPopover.swift           # Main popover: notification list
      NotificationRow.swift          # List row: icon, title, time
      NotificationDetail.swift       # Full view with chart image
      SettingsView.swift             # Connection status, sign out
    Info.plist                       # URL scheme registration
    DataAnalyst.entitlements      # Network client permission
```

## Troubleshooting

### "No auth token"
Token was deleted (e.g. after auth failure). Sign out and sign in again via the browser.

### "Reconnecting in Xs..."
WebSocket connection lost. Check:
- `sudo systemctl status ws-gateway` on server
- `sudo journalctl -u ws-gateway -f` for live logs
- nginx config has the `/ws/notifications` location block

### "Auth failed"
JWT signature mismatch. Verify `DESKTOP_JWT_SECRET` is identical in both `.env` files on the server. Restart both webapp and ws-gateway after changes.

### App shows green dot but notifications don't arrive
The green dot reflects the app's local state, which can become stale after a server-side WS gateway restart (e.g. during deploy). Check the actual connection:
```bash
# On server:
sudo -u deploy curl -s --unix-socket /run/ws-gateway/ws.sock http://localhost/health
# Should show {"connections": 1, "users": {"username": 1}}
```
If connections is 0, restart the app. Check app logs:
```bash
/usr/bin/log show --predicate 'subsystem == "com.dataanalyst"' --last 5m --info
```

### Script runs but no notification appears
Check dispatch socket permissions and webapp logs:
```bash
# Socket must be 0770 and owned by deploy:data-ops
stat -c '%A %U:%G' /run/ws-gateway/ws.sock

# Check webapp logs for dispatch errors
sudo journalctl -u webapp --since '5 min ago' | grep -i 'dispatch\|error'
```

### Testing notifications manually
```bash
# On server, as deploy user:
sudo -u deploy curl -s --unix-socket /run/ws-gateway/ws.sock \
  -X POST http://localhost/dispatch \
  -H 'Content-Type: application/json' \
  -d '{"user":"USERNAME","notification":{"id":"test-1","title":"Test Alert","message":"This is a test notification.","timestamp":"2026-01-30T12:00:00Z"}}'
```

## Future Plans

- Ad-hoc code signing and notarization (Apple Developer ID)
- Sparkle framework for auto-updates
- Host `.app` download on `https://your-instance.example.com/download/`
- Download link on webapp dashboard
- Launch at login via SMAppService
