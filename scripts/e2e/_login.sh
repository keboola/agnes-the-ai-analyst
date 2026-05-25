#!/usr/bin/env bash
# Sourced by every scripts/e2e/smoke_*.sh after $SESSION is set, before any
# agent-browser open against a protected URL. Logs the agent-browser session
# in via the email/password form so the next page load carries the cookie.
#
# Counterpart: scripts/seed_e2e_user.py must have been run against the same
# stack first (workflow handles this; locally see scripts/e2e/README.md).

set -euo pipefail

if [[ -z "${SESSION:-}" ]]; then
  echo "::error::_login.sh requires \$SESSION to be set by the caller" >&2
  exit 2
fi
if [[ -z "${BASE_URL:-}" ]]; then
  echo "::error::_login.sh requires \$BASE_URL to be set by the caller" >&2
  exit 2
fi

E2E_USER_EMAIL='e2e@example.com'
E2E_USER_PASSWORD='E2eSmokePass!'
# Scope every selector by the unique form action — disambiguates Sign In
# from the sibling Forgot Password and Sign Up forms in login_email.html.
LOGIN_FORM='form[action="/auth/password/login/web"]'

echo "→ sign in as ${E2E_USER_EMAIL}"
agent-browser --session "$SESSION" open "${BASE_URL}/login/password"
agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=email]"    "$E2E_USER_EMAIL"
agent-browser --session "$SESSION" fill "${LOGIN_FORM} input[name=password]" "$E2E_USER_PASSWORD"
agent-browser --session "$SESSION" click "${LOGIN_FORM} button[type=submit]"
agent-browser --session "$SESSION" wait --load networkidle
