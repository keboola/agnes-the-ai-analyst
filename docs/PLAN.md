# Modular Architecture Refactor Plan

## Goal

Transform the project from a monolithic structure into a modular, extensible platform where:
- **Auth providers** are pluggable (Google, password, Okta, SAML, custom)
- **Services** are standalone, self-contained modules (telegram bot, WS gateway, etc.)
- **server/** contains only deployment infrastructure
- New features = new directory, zero changes to core

## Target Structure

```
ai-data-analyst/
├── src/                           # Core sync engine (done)
├── connectors/                    # Data source connectors (done)
│   ├── keboola/
│   └── jira/
│
├── auth/                          # Pluggable auth providers
│   ├── __init__.py                # AuthProvider ABC + discover_providers()
│   ├── google/                    # Google OAuth
│   │   ├── __init__.py
│   │   └── provider.py           # Blueprint + GoogleAuthProvider
│   ├── password/                  # Email/password (requires SendGrid)
│   │   ├── __init__.py
│   │   └── provider.py           # Blueprint + PasswordAuthProvider
│   └── desktop/                   # JWT for desktop/API clients
│       ├── __init__.py
│       └── provider.py           # Blueprint + DesktopAuthProvider
│
├── services/                      # Standalone optional services
│   ├── __init__.py                # discover_services() for deploy
│   ├── telegram_bot/              # Telegram notification bot
│   │   ├── __init__.py
│   │   ├── __main__.py           # python -m services.telegram_bot
│   │   ├── bot.py, sender.py, dispatch.py, runner.py
│   │   ├── config.py, storage.py, status.py, test_report.py
│   │   ├── systemd/
│   │   │   └── notify-bot.service
│   │   └── README.md
│   ├── ws_gateway/                # WebSocket notification gateway
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── gateway.py, auth.py, config.py
│   │   ├── systemd/
│   │   │   └── ws-gateway.service
│   │   └── README.md
│   ├── corporate_memory/          # AI knowledge extraction
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── collector.py, prompts.py
│   │   ├── systemd/
│   │   │   ├── corporate-memory.service
│   │   │   └── corporate-memory.timer
│   │   └── README.md
│   └── session_collector/         # User session log collection
│       ├── __init__.py
│       ├── __main__.py
│       ├── collector.py
│       ├── systemd/
│       │   ├── session-collector.service
│       │   └── session-collector.timer
│       └── README.md
│
├── webapp/                        # Flask web portal (slim core)
│   ├── app.py                    # Core routing + auto-discovery
│   ├── auth.py                   # login_required + provider loading
│   ├── config.py                 # Config from instance.yaml
│   ├── user_service.py, account_service.py
│   ├── health_service.py, sync_settings_service.py
│   ├── email_service.py
│   ├── telegram_service.py       # Webapp-side Telegram integration
│   ├── corporate_memory_service.py  # Webapp-side knowledge browser
│   ├── notification_images.py
│   ├── templates/, static/, utils/
│   └── __init__.py
│
├── server/                        # Deployment infrastructure ONLY
│   ├── deploy.sh                 # Auto-discovers services/*/systemd/*
│   ├── setup.sh, webapp-setup.sh
│   ├── bin/                      # add-analyst, list-analysts, etc.
│   ├── sudoers-*, limits-*.conf
│   ├── webapp.service, webapp-nginx.conf
│   └── migrate-*.sh
│
├── scripts/                       # Analyst-facing helpers (merged dev_scripts/)
├── config/                        # Instance configuration
├── docs/                          # User documentation
├── dev_docs/                      # Developer docs (sanitized)
├── examples/                      # Example scripts
└── tests/                         # Test suite
```

## Auth Provider Interface

```python
# auth/__init__.py

class AuthProvider(ABC):
    """Base class for authentication providers."""

    @abstractmethod
    def get_name(self) -> str:
        """Internal name (e.g., 'google', 'password')."""

    @abstractmethod
    def get_blueprint(self) -> Blueprint:
        """Flask blueprint with auth routes."""

    @abstractmethod
    def get_login_button(self) -> dict:
        """Login button definition for the login page.
        Returns: {
            "text": "Sign in with Google",
            "url": "/login/google",
            "icon": "google",       # CSS class or SVG name
            "subtitle": "For @acme.com email addresses.",
            "order": 10,            # Sort order on login page
        }
        """

    def is_available(self) -> bool:
        """Check if provider is configured and ready.
        Override to check env vars, API keys, etc."""
        return True

    def get_display_name(self) -> str:
        """Human-readable name for UI."""
        return self.get_name().title()
```

### Discovery

```python
def discover_providers() -> list[AuthProvider]:
    """Auto-discover auth providers from auth/*/provider.py.
    Each provider module must export `provider` instance."""
    providers = []
    auth_dir = Path(__file__).parent
    for subdir in sorted(auth_dir.iterdir()):
        if subdir.is_dir() and (subdir / "provider.py").exists():
            mod = importlib.import_module(f"auth.{subdir.name}.provider")
            provider = getattr(mod, "provider", None)
            if provider and isinstance(provider, AuthProvider) and provider.is_available():
                providers.append(provider)
    return providers
```

### Login Template

```html
{# webapp/templates/login.html - dynamic login buttons #}
{% for provider in auth_providers %}
<a href="{{ provider.login_button.url }}" class="btn btn-auth btn-{{ provider.login_button.icon }}">
    {{ provider.login_button.text }}
</a>
{% if provider.login_button.subtitle %}
<p class="auth-subtitle">{{ provider.login_button.subtitle }}</p>
{% endif %}
{% endfor %}
```

### Session Contract

All auth providers MUST set the same session structure:
```python
session["user"] = {
    "email": "user@acme.com",   # Required - unique identifier
    "name": "John Doe",          # Optional - display name
    "picture": "https://...",     # Optional - avatar URL
}
```

## Implementation Phases

### Phase 1: Move services to services/ (git mv + fix imports)

**Files moved:**
- `server/telegram_bot/` -> `services/telegram_bot/`
- `server/ws_gateway/` -> `services/ws_gateway/`
- `server/corporate_memory/` -> `services/corporate_memory/`
- `server/session_collector.py` -> `services/session_collector/collector.py`
- Service files from `server/*.service` -> `services/*/systemd/`
- Timer files from `server/*.timer` -> `services/*/systemd/`

**Import fixes:**
- `from server.telegram_bot.X` -> `from services.telegram_bot.X` (in webapp/app.py)
- `python -m server.X` -> `python -m services.X` (in systemd files, bin/ scripts)
- Internal imports within services stay as relative imports

**Config updates:**
- `server/deploy.sh` - discover services from `services/*/systemd/`
- `server/bin/collect-knowledge` - update module path
- `server/bin/collect-sessions` - update module path

### Phase 2: Extract auth providers to auth/

**Files moved:**
- `webapp/auth.py` -> `auth/google/provider.py` (OAuth logic)
- `webapp/password_auth.py` -> `auth/password/provider.py`
- `webapp/desktop_auth.py` -> `auth/desktop/provider.py`

**What stays in webapp/auth.py:**
- `login_required` decorator (used everywhere)
- `/logout` route
- Session management utils

**New files:**
- `auth/__init__.py` - AuthProvider ABC + discover_providers()
- `auth/google/__init__.py`
- `auth/password/__init__.py`
- `auth/desktop/__init__.py`

**webapp/app.py changes:**
- Replace hardcoded blueprint imports with `discover_providers()`
- Pass `auth_providers` to login template context
- Remove try/except blocks for individual auth modules

### Phase 3: Update deploy.sh service discovery

**deploy.sh changes:**
- Auto-discover and install `services/*/systemd/*.service` and `*.timer`
- Remove hardcoded service file paths
- Add enable/disable per instance.yaml config

### Phase 4: Cleanup

- Merge `dev_scripts/` into `scripts/`
- Sanitize `dev_docs/` (replace real IPs, hostnames, usernames with placeholders)
- Update CLAUDE.md, README.md, ARCHITECTURE.md
- Update MEMORY.md

## Verification

```bash
# 1. All tests pass
pytest tests/ connectors/ -v

# 2. No server.telegram_bot imports remain
grep -rn "from server\.\(telegram_bot\|ws_gateway\|corporate_memory\)" .

# 3. No hardcoded auth imports in app.py
grep -n "from.*auth import\|from.*password_auth" webapp/app.py

# 4. Import smoke tests
python -c "from auth import discover_providers; print(f'{len(discover_providers())} providers')"
python -c "from services.telegram_bot.bot import TelegramBot; print('OK')"

# 5. Service files discoverable
ls services/*/systemd/*.service services/*/systemd/*.timer
```
