# Cloudflare Access Authentication

Agnes can be deployed behind a Cloudflare Zero Trust tunnel with Access
protecting it as an SSO gate. When configured, users who pass CF's
identity check are automatically signed into Agnes — no second login.

This works **alongside** the built-in password and Google OAuth flows:
direct connections (e.g. local dev, CLI with PAT) still use those. Only
the CF-gated path auto-logs-in.

## Prerequisites

- A Cloudflare Zero Trust team (Free tier works for up to 50 users)
- A domain routed to Agnes via Cloudflare Tunnel (`cloudflared`) or CF proxy
- An Access Application configured in front of that domain

## Configure the Access Application

1. In the Cloudflare Zero Trust dashboard → **Access** → **Applications**
   → **Add an application** → **Self-hosted**
2. Application domain: the hostname routed to your Agnes instance
   (e.g. `agnes.yourco.com`)
3. Identity providers: enable your IdP (Google Workspace, Okta, etc.)
4. Policies: add at least one Allow policy (e.g. email ending in `@yourco.com`)
5. After creation, open the app → **Overview** tab and copy the **Application
   Audience (AUD) Tag**

## Configure Agnes

Set two environment variables in your deployment (`.env` or Secret Manager):

~~~bash
CF_ACCESS_TEAM=yourteam          # from https://yourteam.cloudflareaccess.com
CF_ACCESS_AUD=abc123...          # AUD Tag from the Application → Overview page
~~~

Optionally restrict which email domains can auto-provision:

~~~bash
CF_ACCESS_DOMAIN_ALLOW=yourco.com,partner.com
~~~

If unset, falls back to `allowed_domains` in `config/instance.yaml` (same
allowlist used by the Google OAuth provider).

Restart Agnes. That's it — requests arriving with a valid
`Cf-Access-Jwt-Assertion` header will auto-provision a new `analyst` user
and issue a session cookie.

## Security Model

- **Both env vars required**: if either `CF_ACCESS_TEAM` or `CF_ACCESS_AUD`
  is unset, the middleware is completely inert and the header is ignored.
  This prevents header spoofing on deployments that don't actually sit
  behind Cloudflare.
- **JWT verification**: signature checked against the team's JWKS
  (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, cached 5 min);
  `aud` and `iss` both validated; expired tokens rejected.
- **Never overwrites an existing session**: if the user already has an
  `access_token` cookie, the middleware passes through — you can always
  sign in explicitly with password/Google on a CF-protected deployment.
- **Never 401s from middleware**: if verification fails for any reason, the
  request continues to the normal auth layer — users see the normal login
  page rather than a confusing middleware error.
- **PAT/API (Bearer) clients are skipped**: requests carrying an
  `Authorization: Bearer <token>` header bypass the middleware entirely —
  no cookie is set. This preserves the clean stateless contract for
  CLI tools, CI, and scripts.

## Logout Semantics

Clicking "log out" in Agnes clears the local `access_token` cookie.
**However, if the user is still behind Cloudflare Access**, the next
request will carry a fresh `Cf-Access-Jwt-Assertion` header and the
middleware will immediately re-issue a session cookie — logout appears
to have no effect.

To fully sign out on a CF-gated deployment, the user must also sign out
of their Cloudflare Access session by visiting:

~~~
https://<your-agnes-domain>/cdn-cgi/access/logout
~~~

Consider linking to this URL from Agnes's logout UI on CF-gated
deployments, or document it in your internal user guide.

## Troubleshooting

**Auto-login doesn't happen:**
- Check `CF_ACCESS_TEAM` matches the exact subdomain (no protocol, no path):
  `keboola`, not `https://keboola.cloudflareaccess.com`
- Check `CF_ACCESS_AUD` is the **Application AUD Tag**, not the Access
  Team ID
- Verify the request actually has the header:
  `curl -I https://agnes.yourco.com/dashboard` behind CF should show
  `Cf-Access-Jwt-Assertion` in the request (use `cloudflared access curl`
  or browser dev tools)
- Check Agnes logs for `CF Access JWT invalid: ...` or
  `CF Access JWT verification error: ...`

**"User deactivated" redirect:**
- Someone deactivated this user in Agnes's admin panel. CF Access passes
  identity, but Agnes enforces the `active` flag.

**New users arrive with `analyst` role — how do I get admin access?**
- Same as Google OAuth: bootstrap the first admin manually
  (`POST /auth/bootstrap`) or have an existing admin promote via the web UI.
