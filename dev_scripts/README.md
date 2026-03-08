# Development Scripts

Development utilities for local testing without full production setup.

## dev_run.py

Flask development server with authentication bypass for local testing.

**Usage:**
```bash
python3 dev_scripts/dev_run.py
```

**Features:**
- Bypasses Google OAuth (no client ID/secret needed)
- Direct catalog access: http://127.0.0.1:5000/dev-catalog
- Uses local `docs/metrics/` instead of `/data/docs/metrics`
- Debug mode enabled
- Hot reload on code changes

**Quick Access:**
- Dashboard: http://127.0.0.1:5000/dev-login
- Direct to Catalog: http://127.0.0.1:5000/dev-catalog (recommended)

**Note:** Only works in DEBUG mode (automatically enabled by script).
