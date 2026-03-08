# Security Audit Report: Data Broker Server

**Date:** 2026-01-30
**Server:** `data-broker-for-claude` (YOUR_SERVER_IP), Debian 12 (bookworm), GCP e2-medium
**Auditors:** Claude Opus 4.5 (primary) + Perplexity Sonar (validation) + OpenAI Codex (second opinion)
**Scope:** Linux server security, user isolation, CI/CD pipeline, notification system, desktop app attack surface
**Status:** Read-only audit -- no changes were made to the server

---

## Table of Contents

- [Executive Summary](#executive-summary)
- [Server Overview](#server-overview)
- [Findings Summary](#findings-summary)
- [Critical Findings](#critical-findings)
- [High Findings](#high-findings)
- [Medium Findings](#medium-findings)
- [Low Findings](#low-findings)
- [Additional Findings from Second Opinion](#additional-findings-from-second-opinion)
- [Auditor Consensus Matrix](#auditor-consensus-matrix)
- [Prioritized Remediation Plan](#prioritized-remediation-plan)
- [Appendix: Methodology](#appendix-methodology)

---

## Executive Summary

The server has a solid foundational architecture: SSH key-only authentication, POSIX ACLs for data isolation, per-user home directories with 750 permissions, setgid directories for shared data, and systemd service hardening. However, the audit identified **3 critical, 5 high, and 10 medium/low** findings that undermine these controls.

The most severe issues are:

1. **All analysts can read private data** due to an ACL misconfiguration (C2)
2. **Any local user can send fake notifications** to any other user via a world-writable Unix socket (C3)
3. **CI/CD pipeline provides unrestricted root access** through sudoers wildcard rules (C1)

These findings were independently validated by Perplexity (CVE references, Unix socket security research) and confirmed by OpenAI Codex second opinion review.

---

## Server Overview

### Infrastructure

| Parameter | Value |
|-----------|-------|
| Hostname | data-broker-for-claude |
| GCP Project | kids-ai-data-analysis |
| Zone | europe-north1-a |
| OS | Debian 12 (bookworm) |
| External IP | YOUR_SERVER_IP |
| Domain | your-instance.example.com |

### Users and Groups

| Group | Members | Purpose |
|-------|---------|---------|
| `dataread` | padak, matejkys, dasa, petr, fisa, dasa.damaskova, martin.lepka, pavel.dolezal, martin.matejka, jiri.manas | Public data read access |
| `data-private` | padak, matejkys, dasa | Private/sensitive data access |
| `data-ops` | deploy, padak, matejkys, dasa, www-data | Application deployment and operations |

### Services

| Service | User | Port/Socket | Purpose |
|---------|------|-------------|---------|
| `webapp` | www-data | gunicorn -> nginx :443 | Self-service portal (Google SSO) |
| `notify-bot` | deploy | `/run/notify-bot/bot.sock` | Telegram notification bot |
| `ws-gateway` | deploy | 127.0.0.1:8765 + `/run/ws-gateway/ws.sock` | WebSocket gateway for desktop app |
| nginx | www-data | :80, :443 | HTTPS reverse proxy |
| Postfix | root | :25 | SMTP (outbound mail) |
| sshd | root | :22 | SSH access |

### CI/CD Pipeline

```
GitHub repo (main branch)
    |
    v (push trigger)
GitHub Actions (appleboy/ssh-action@v1.0.3)
    |
    v (SSH as deploy user)
deploy.sh on server:
    1. git fetch + git reset --hard origin/main
    2. sudo cp server/bin/* -> /usr/local/bin/
    3. sudo cp server/sudoers-* -> /etc/sudoers.d/
    4. sudo cp *.service -> /etc/systemd/system/
    5. sudo systemctl restart webapp, notify-bot, ws-gateway
```

### Notification Pipeline

```
User Python scripts (~/user/notifications/*.py)
    |
    v (crontab -> notify-runner)
bot.sock (/run/notify-bot/bot.sock, mode 0666)
    |
    +-> Telegram Bot API (sendMessage, sendPhoto)
    |
    +-> ws.sock (/run/ws-gateway/ws.sock, mode 0770)
            |
            v (WebSocket over wss://)
        macOS Desktop App (DataAnalyst.app)
```

---

## Findings Summary

| Severity | Count | IDs |
|----------|-------|-----|
| **CRITICAL** | 3 | C1, C2, C3 |
| **HIGH** | 5 | H1, H2, H3, H4, H5 |
| **MEDIUM** | 8 | M1-M8 |
| **LOW** | 4 | L1-L4 |

---

## Critical Findings

### C1: CI/CD Pipeline Provides Unrestricted Root Access via Sudoers Wildcards

**Severity:** CRITICAL
**CVSS estimate:** 9.1 (Network / High impact / No user interaction if GitHub compromised)
**Validated by:** Perplexity (CVE-2026-22536 pattern), Codex (confirmed CRITICAL)

#### Description

The deploy user's sudoers configuration (`server/sudoers-deploy`) uses wildcard glob patterns that allow copying **any file** matching the source pattern into privileged system directories:

```
# /etc/sudoers.d/deploy (lines 8-9, 13)
deploy ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/data-analyst/repo/server/bin/* /usr/local/bin/*
deploy ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/data-analyst/repo/server/sudoers-* /etc/sudoers.d/*
deploy ALL=(ALL) NOPASSWD: /usr/bin/chmod 755 /usr/local/bin/*
deploy ALL=(ALL) NOPASSWD: /usr/bin/chmod 440 /etc/sudoers.d/*
```

The deploy user has write access to the source directory (`/opt/data-analyst/repo/`) because `deploy.sh` runs `git reset --hard origin/main`, pulling whatever code exists on the main branch.

#### Attack Chain

1. Attacker gains write access to the GitHub repository (compromised personal access token, stolen SSH deploy key, social engineering on a maintainer, or missing branch protection rules)
2. Attacker creates `server/sudoers-backdoor` with content: `ALL ALL=(ALL) NOPASSWD: ALL`
3. Attacker creates `server/bin/backdoor` with a reverse shell or privilege escalation payload
4. Push to `main` triggers GitHub Actions deploy workflow
5. `deploy.sh` executes `git reset --hard origin/main` -- attacker's code is now on disk
6. Script runs `sudo /usr/bin/cp /opt/data-analyst/repo/server/sudoers-backdoor /etc/sudoers.d/backdoor` (matches wildcard)
7. Script runs `sudo /usr/bin/chmod 440 /etc/sudoers.d/backdoor` (valid sudoers file)
8. **Full root access achieved** -- any user on the system can now run any command as root

Note: `deploy.sh` does validate sudoers files with `visudo -cf` before copying (line 64), which is a good control. However, the attacker controls the file content, so they can craft a syntactically valid but malicious sudoers file.

#### Perplexity Validation

Perplexity confirmed this as a known vulnerability pattern, referencing CVE-2026-22536:

> _"A common exploitation involves sudoers entries granting a user permission to execute /usr/bin/cp with wildcards, where the attacker controls the source directory. The wildcard expands to copy the malicious file as root."_

Source: SentinelOne vulnerability database, Compass Security research on dangerous sudoers entries.

#### Codex Second Opinion

> _"git reset --hard origin/main does NOT mitigate a compromised origin. The attacker updates origin/main and you faithfully reset to it and then copy sudoers into /etc/sudoers.d. Critical -- full root compromise via CI/CD."_

#### Current Mitigating Controls

- GitHub deploy key is read-only (cannot push, but attacker could use a maintainer's token)
- `visudo -cf` validates syntax before copy (prevents broken sudoers, not malicious ones)
- Deploy user is not in `sudo` group (can only run explicitly allowed commands)

#### Recommendations

1. **Replace wildcards with explicit file paths** in `sudoers-deploy`:
   ```
   # Instead of:
   deploy ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/data-analyst/repo/server/sudoers-* /etc/sudoers.d/*
   # Use:
   deploy ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/data-analyst/repo/server/sudoers-deploy /etc/sudoers.d/deploy
   deploy ALL=(ALL) NOPASSWD: /usr/bin/cp /opt/data-analyst/repo/server/sudoers-webapp /etc/sudoers.d/webapp
   ```
   Same for `server/bin/*` -- list each script explicitly.

2. **GitHub branch protection** on `main`: require pull request reviews, no force push, require signed commits, restrict who can push.

3. **Pin GitHub Actions** to SHA instead of tag:
   ```yaml
   # Instead of:
   uses: appleboy/ssh-action@v1.0.3
   # Use:
   uses: appleboy/ssh-action@<full-sha-hash>
   ```

4. **Consider**: manage sudoers files outside the git repo (e.g., Ansible vault, manual root-only deployment).

---

### C2: Private Data ACL Grants Read Access to All Analysts

**Severity:** CRITICAL
**CVSS estimate:** 7.5 (Local / High confidentiality impact)
**Validated by:** Perplexity (POSIX ACL mechanism confirmed), live server test (exit code 0)

#### Description

The directory `/data/src_data/parquet/private/` is intended to be accessible only to members of the `data-private` group (3 privileged users: padak, matejkys, dasa). However, its POSIX ACL also grants access to the `dataread` group, which contains **all 10 analysts**:

```
# getfacl /data/src_data/parquet/private/
user::rwx
group::rwx
group:dataread:r-x      # <-- ALL analysts have read+execute
group:data-private:r-x   # <-- intended: only 3 privileged users
mask::rwx                # <-- mask does NOT restrict (rwx & r-x = r-x)
other::---

# Default ACL (inherited by new files):
default:group:dataread:r-x      # <-- new files also readable by all
default:group:data-private:r-x
default:mask::rwx
```

The POSIX ACL mask (`mask::rwx`) does not restrict the `dataread` entry because `rwx AND r-x = r-x` (mask only limits, it doesn't deny). This means every member of `dataread` has effective `r-x` on the private directory and all its contents.

#### Proof of Exploitation

Tested directly on server with user `fisa` (standard analyst, member of `dataread` only, NOT `data-private`):

```bash
$ sudo -u fisa ls -la /data/src_data/parquet/private/
total 16
drwxrws---+ 2 padak data-ops 4096 Jan 21 14:29 .
drwxrws---+ 7 padak data-ops 4096 Jan 23 18:29 ..
# Exit code: 0 (access granted)
```

The directory is currently empty, but the ACL permits access. When private data files are added, all analysts will be able to read them.

#### Documentation Contradiction

`server.md` (line 347) claims:
```
$ ls ~/server/parquet/private/
ls: cannot open directory 'private/': Permission denied
```

This is incorrect given the current ACL configuration.

#### Perplexity Validation

Perplexity confirmed the POSIX ACL mechanism:

> _"Effective permissions for any user matching a named group ACL are the logical AND of that entry's permissions and the mask. If mask is rwx, a group:dataread:r-x entry yields effective r-x."_

#### Recommendations

```bash
# Remove dataread from private/ and its default ACL
sudo setfacl -R -x g:dataread /data/src_data/parquet/private/
sudo setfacl -R -d -m g:dataread:--- /data/src_data/parquet/private/

# Or remove and re-set defaults without dataread:
sudo setfacl -R -b /data/src_data/parquet/private/
sudo setfacl -R -m u::rwx,g::rwx,g:data-private:r-x,g:data-ops:rwx,o::--- /data/src_data/parquet/private/
sudo setfacl -R -d -m u::rwx,g::rwx,g:data-private:r-x,g:data-ops:rwx,o::--- /data/src_data/parquet/private/

# Verify:
sudo -u fisa ls /data/src_data/parquet/private/
# Expected: "ls: cannot open directory 'private/': Permission denied"
```

Also fix the `add-analyst` script or whatever sets up ACLs to not include `dataread` on `private/`.

---

### C3: World-Writable Notification Socket Enables Cross-User Spoofing

**Severity:** CRITICAL (upgraded from HIGH based on combined impact with H1)
**CVSS estimate:** 7.8 (Local / High integrity impact / social engineering amplifier)
**Validated by:** Perplexity (dirty_sock CVE-2019-7304 pattern), Codex (HIGH, CRITICAL with H1)

#### Description

The Telegram bot's Unix socket at `/run/notify-bot/bot.sock` has permissions `0666` (world-readable, world-writable):

```
srw-rw-rw- 1 deploy data-ops 0 Jan 30 19:20 /run/notify-bot/bot.sock
```

The socket accepts HTTP requests without any caller authentication. The `POST /send` endpoint takes a JSON body with:
- `user`: target username (any analyst)
- `text`: message content (arbitrary Markdown)
- `parse_mode`: formatting mode

Any local user on the server can send a fake Telegram notification to **any other user** by specifying their username.

#### Proof of Concept

Any analyst can execute:
```bash
curl --unix-socket /run/notify-bot/bot.sock \
  -X POST http://localhost/send \
  -H 'Content-Type: application/json' \
  -d '{
    "user": "ceo",
    "text": "*URGENT: Security incident detected*\nYour account may be compromised.\nReset credentials immediately: https://attacker-controlled.example.com/reset",
    "parse_mode": "Markdown"
  }'
```

This sends a convincing-looking urgent security alert to the CEO's Telegram, appearing to come from the official notification bot.

#### Combined Impact with H1 (Notification Content Injection)

The same mechanism works for the WebSocket dispatch socket (`/run/ws-gateway/ws.sock`), which is correctly restricted to `0770`. However, bot.sock notifications are also dispatched to the WebSocket gateway by `notify-runner` (line 261 of `server/bin/notify-runner`), so **direct bot.sock access bypasses the ws.sock restriction** for Telegram delivery.

Attack scenarios:
- **Phishing**: fake urgent alerts with malicious URLs
- **Social engineering**: impersonate system alerts to manipulate user behavior
- **Prompt injection**: if user copies notification text into Claude Code, attacker can inject LLM instructions
- **Spam/DoS**: flood another user's Telegram with notifications

#### Comparison with ws.sock

The WebSocket gateway socket is correctly configured:
```
srwxrwx--- 1 deploy data-ops 0 Jan 30 19:20 /run/ws-gateway/ws.sock
```
Only `deploy` and `data-ops` group members can access it. The bot socket should follow the same pattern.

#### Perplexity Validation

Perplexity referenced the snapd `dirty_sock` vulnerability (CVE-2019-7304) as a precedent:

> _"SocketMode=0666 in systemd units enabled connections from unprivileged processes. Best practice: Use 0600 (owner-only) or 0660 (owner/group) combined with proper ownership. Always verify peer credentials with SO_PEERCRED after connection."_

#### Codex Second Opinion

> _"I'd rate High, not Critical, unless Telegram/macOS notifications are used for security-sensitive actions. With H1 (no sanitization) it becomes Critical in practice."_

Given that notifications go to both Telegram and the desktop app, and there is no sender verification, we rate this CRITICAL.

#### Recommendations

1. **Immediate**: Change socket permissions to `0660` or `0770` in the bot code (`server/telegram_bot/bot.py`) or systemd service file. The socket is currently set to `0666` by an `ExecStartPost` or in code -- update to restrict to `data-ops` group.

2. **Better**: Add `SO_PEERCRED` validation in the bot's HTTP handler to verify the caller's UID and ensure they can only send notifications for their own username.

3. **Best**: Implement a sender authentication mechanism where the `/send` endpoint validates that the `user` field matches the calling process's system username (obtained via `SO_PEERCRED` or `getpeereid()`).

---

## High Findings

### H1: No Content Sanitization in Notification Pipeline

**Severity:** HIGH (CRITICAL when combined with C3)
**Validated by:** Codex confirmed

#### Description

User notification scripts output JSON with `title` and `message` fields containing arbitrary text. This content flows through the entire notification pipeline without any sanitization:

1. **User script** (`~/user/notifications/*.py`) outputs JSON to stdout
2. **notify-runner** (`/usr/local/bin/notify-runner`) parses JSON, sends to bot.sock
3. **Telegram bot** forwards to Telegram API with Markdown formatting
4. **notify-runner** also dispatches to ws.sock for desktop app delivery
5. **WebSocket gateway** forwards to connected macOS app clients
6. **macOS app** renders in SwiftUI views and macOS notification center

No component in this chain validates, sanitizes, or escapes the content. Combined with C3 (world-writable socket), any local user can inject arbitrary content into another user's notifications.

#### Attack Vectors

- **Phishing via Telegram**: Markdown links `[Click here](https://evil.com)` render as clickable
- **macOS notification spoofing**: banner notifications show title/message as-is
- **Prompt injection**: if user pastes notification content into Claude Code or another LLM tool, the attacker can embed hidden instructions
- **UI confusion**: SwiftUI Text views may render certain characters in unexpected ways

#### Recommendations

1. Strip or escape URLs in notification content at the bot.sock handler level
2. Limit message length (e.g., 500 characters for title, 2000 for message)
3. Desktop app: escape special characters, render as plain text by default
4. Consider: content-type field (text/plain vs text/markdown) with appropriate rendering

---

### H2: notify-scripts Sudo Rule Allows Execution as Any User Including Root

**Severity:** HIGH
**Validated by:** Codex (HIGH, potentially CRITICAL)

#### Description

The sudoers rules for `www-data` and `deploy` allow running `notify-scripts` as **any user** on the system:

```
# /etc/sudoers.d/webapp
www-data ALL=(ALL) NOPASSWD: /usr/local/bin/notify-scripts

# /etc/sudoers.d/deploy (line 69)
deploy ALL=(ALL) NOPASSWD: /usr/local/bin/notify-scripts
```

The `(ALL)` target means these service users can execute:
```bash
sudo -u root /usr/local/bin/notify-scripts run some_script.py
sudo -u deploy /usr/local/bin/notify-scripts list
```

The `username` parameter comes from webapp request data or Telegram bot callback data without validation against a list of valid analyst usernames.

#### Code Analysis

In `server/telegram_bot/runner.py` (line 30):
```python
result = subprocess.run(
    ["/usr/bin/sudo", "-u", username, NOTIFY_SCRIPTS_BIN, "run", script_name],
    ...
)
```

The `username` is passed directly from the Telegram callback data or webapp API request. While `notify-scripts` itself validates that the script file exists in `~/user/notifications/`, the `sudo -u` target is not validated.

#### Mitigating Controls

- `notify-scripts` validates script path exists in `~/user/notifications/` (non-existent for root)
- Scripts must end in `.py`
- 60-second timeout enforced

#### Recommendations

1. Restrict sudoers to analyst group only:
   ```
   www-data ALL=(dataread) NOPASSWD: /usr/local/bin/notify-scripts
   deploy ALL=(dataread) NOPASSWD: /usr/local/bin/notify-scripts
   ```

2. Add username validation in `runner.py` and webapp API: check that username exists in the `dataread` group before executing.

---

### H3: SMTP Port 25 Open on All Interfaces

**Severity:** HIGH
**Validated by:** Codex (MEDIUM-HIGH)

#### Description

Postfix is configured with `inet_interfaces = all`, listening on port 25 on all network interfaces. Combined with:
- No iptables rules (empty chains, ACCEPT policy)
- No GCP firewall rule explicitly blocking port 25

The server may be reachable on port 25 from the internet, potentially allowing:
- Open relay abuse (if Postfix relay restrictions are misconfigured)
- Spam origination
- Information disclosure via SMTP banner

#### Current State

```
# /etc/postfix/main.cf
myhostname = data-broker-for-claude.c.kids-ai-data-analysis.internal
mydestination = $myhostname, localhost
inet_interfaces = all

# ss -tlnp (port 25 listening on 0.0.0.0)
LISTEN 0 100 0.0.0.0:25
```

#### Recommendations

1. Change to `inet_interfaces = loopback-only` if external mail is not needed
2. Or add iptables rule: `iptables -A INPUT -p tcp --dport 25 -j DROP`
3. Verify GCP firewall does not allow port 25 inbound

---

### H4: No Rate Limiting on API Endpoints (Verification Code Brute-Force)

**Severity:** HIGH (upgraded from MEDIUM based on Codex feedback)
**Validated by:** Codex (HIGH if auth factor and online)

#### Description

The Telegram verification code is a 6-digit number (1,000,000 possible combinations) with a 10-minute expiry. The endpoint `POST /api/telegram/verify` has no rate limiting. An attacker could brute-force the code at approximately 1,700 requests/second to exhaust all combinations within the 10-minute window.

A successful brute-force would allow the attacker to link their own Telegram account to another user's analyst account, receiving all that user's notifications.

#### Recommendations

1. Add rate limiting with `flask-limiter` (e.g., 5 attempts per minute per session)
2. Increase code length to 8 digits
3. Lock account after N failed attempts
4. Add CAPTCHA after 3 failed attempts

---

### H5: Empty iptables + fail2ban Inactive

**Severity:** HIGH (combined), individually MEDIUM
**Validated by:** Codex (MEDIUM each, HIGH combined)

#### Description

**iptables:**
```
Chain INPUT (policy ACCEPT)
Chain FORWARD (policy ACCEPT)
Chain OUTPUT (policy ACCEPT)
# No rules in any chain
```

**fail2ban:** inactive/not installed (`systemctl is-active fail2ban` returns inactive).

The server relies exclusively on GCP firewall rules for network protection. If a GCP firewall rule is accidentally broadened, all server ports become exposed with no host-level defense.

SSH is key-only (mitigates password brute-force), but without fail2ban there is no rate limiting on failed key auth attempts, which could be used for user enumeration or resource exhaustion.

#### Recommendations

1. Install and activate fail2ban with sshd and nginx jails
2. Configure iptables/nftables as defense-in-depth:
   ```bash
   # Allow established, SSH, HTTP, HTTPS only
   iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
   iptables -A INPUT -p tcp --dport 22 -j ACCEPT
   iptables -A INPUT -p tcp --dport 80 -j ACCEPT
   iptables -A INPUT -p tcp --dport 443 -j ACCEPT
   iptables -A INPUT -i lo -j ACCEPT
   iptables -P INPUT DROP
   iptables -P FORWARD DROP
   ```

---

## Medium Findings

### M1: CI/CD Trust Boundary -- Push to Main Equals Full Server Control

**Severity:** MEDIUM (de facto part of C1, treated separately for organizational clarity)
**Codex rating:** CRITICAL (if branch protections absent)

#### Description

The GitHub Actions workflow (`.github/workflows/deploy.yml`) triggers on every push to `main` branch. It connects via SSH as the `deploy` user and runs `deploy.sh`, which has root-level effects through sudoers rules.

Anyone with push access to the `main` branch controls:
- All scripts in `/usr/local/bin/`
- All sudoers files in `/etc/sudoers.d/`
- All systemd service files
- The `.env` file containing secrets (WEBAPP_SECRET_KEY, GOOGLE_CLIENT_SECRET, JWT secret, Telegram bot token)
- Python code running as www-data (webapp) and deploy (bot, gateway)

#### Recommendations

See C1 recommendations. Additionally:
- Audit GitHub collaborator list and permissions
- Enable GitHub audit log monitoring
- Consider separate deployment approval step (manual gate in Actions)

### M2: JWT Secret Without Rotation

**Severity:** MEDIUM

Desktop app JWT tokens have 30-day expiry with a 7-day grace period for refresh (`DESKTOP_JWT_REFRESH_GRACE_DAYS`). The `DESKTOP_JWT_SECRET` is set once via GitHub Actions and never rotated. A compromised token provides 37 days of access.

**Recommendation:** Implement secret rotation mechanism. Consider shorter token expiry (7 days) with automatic refresh.

### M3: Notification Image Endpoint May Allow Path Traversal

**Severity:** MEDIUM-HIGH (needs code review of `webapp/notification_images.py`)

The endpoint `GET /api/notifications/images/<filename>` serves files from `/tmp/`. If the `filename` parameter is not properly sanitized, a request like `GET /api/notifications/images/../../etc/passwd` could read arbitrary files.

**Recommendation:** Review `notification_images.py` implementation. Ensure `os.path.basename()` is applied to filename and result is validated to be within `/tmp/`.

### M4: GitHub Actions Uses Tag-Pinned Action (Not SHA)

**Severity:** MEDIUM

```yaml
uses: appleboy/ssh-action@v1.0.3  # tag, not SHA
```

If the `appleboy/ssh-action` repository is compromised, the tag could be moved to point to malicious code (supply chain attack).

**Recommendation:** Pin to full commit SHA:
```yaml
uses: appleboy/ssh-action@<full-40-char-sha>
```

### M5: Telegram Bot Token on Disk

**Severity:** MEDIUM

The Telegram bot token is stored in two files:
- `/opt/data-analyst/.env` (mode 640, root:data-ops) -- readable by data-ops group
- `/opt/data-analyst/repo/.env` (mode 640, root:data-ops) -- same

If compromised, the token allows sending arbitrary Telegram messages to any linked user via the Telegram Bot API directly (bypassing the bot socket entirely).

**Recommendation:** Ensure `.env` files are not readable by analysts (currently correct: 640 root:data-ops). Monitor for unauthorized access.

### M6: Systemd Service Hardening Gaps

**Severity:** MEDIUM
**Source:** Codex second opinion

The `notify-bot.service` has:
```ini
NoNewPrivileges=false  # required for sudo -u
PrivateTmp=false       # required to read user image files from /tmp
```

Missing hardening directives:
- `CapabilityBoundingSet=` (drop unnecessary capabilities)
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`
- `SystemCallFilter=@system-service`

**Recommendation:** Add hardening directives where possible. The `NoNewPrivileges=false` is necessary for the current architecture but should be addressed by moving to a queue-based approach (see GitHub issue #51).

### M7: Deploy Home Directory is 755

**Severity:** MEDIUM

```
drwxr-xr-x 4 deploy deploy 4096 Jan 30 19:20 /home/deploy/
```

All analyst home directories are `750`, but deploy's home is `755` (world-readable). While deploy's home does not contain sensitive data in obvious locations, `.ssh/` is `700` (correct).

**Recommendation:** `chmod 750 /home/deploy`

### M8: WebSocket Gateway Lacks Origin Check and Replay Protection

**Severity:** MEDIUM
**Source:** Codex second opinion

The WebSocket gateway (`server/ws_gateway/gateway.py`) validates JWT tokens but does not:
- Check the `Origin` header of WebSocket connections
- Implement replay protection (a captured auth message could be replayed within token validity)
- Bind tokens to specific connections or IP addresses

**Recommendation:** Add Origin header validation. Consider adding a nonce to the auth flow.

---

## Low Findings

### L1: /data/downloads World-Readable

```
drwxr-xr-x 2 root root 4096 Jan 30 13:39 /data/downloads/
-rw-r--r-- 1 root data-ops 131219 Jan 30 13:39 DataAnalyst.zip
```

The desktop app download is accessible to all users. Low risk as this is a distributable application.

### L2: Missing auditd

No audit daemon configured. Sudo operations are logged in `auth.log` but detailed file access auditing is absent.

**Recommendation:** Install and configure `auditd` for monitoring privileged operations and sensitive file access.

### L3: MaxSessions 20

SSH allows 20 concurrent sessions per user, which is higher than typical (default 10). Increases session hijacking surface.

### L4: Cron Error Reporting via SMTP

Cron jobs use `MAILTO=admin@your-domain.com`, which depends on the Postfix configuration (see H3). If SMTP is misconfigured, cron errors may be silently lost.

---

## Additional Findings from Second Opinion

The following findings were identified by the OpenAI Codex second opinion review and were not in the original audit:

| ID | Finding | Severity | Description |
|----|---------|----------|-------------|
| NEW-1 | Symlink/path-traversal in sudo-executed scripts | MEDIUM | `notify-scripts` and `deploy.sh` may follow symlinks when operating on user directories. If an analyst creates a symlink in `~/user/notifications/` pointing to a sensitive file, execution via `sudo -u` could have unintended effects. |
| NEW-2 | Lateral movement via shared directory enumeration | LOW | Analysts can enumerate shared configs and other users via symlinks in `~/server/`. While data is read-only, directory listings may reveal information about other users or system configuration. |
| NEW-3 | Environment variable leakage in sudo | MEDIUM | `notify-scripts` runs via `sudo -u <user>`. Depending on sudoers `env_reset` configuration, environment variables from the calling process (www-data, deploy) may leak to the user process. |

---

## Auditor Consensus Matrix

| Finding | Claude (primary) | Perplexity | Codex | Consensus |
|---------|-----------------|------------|-------|-----------|
| C1 (sudoers wildcards) | CRITICAL | CRITICAL (CVE ref) | CRITICAL | **CRITICAL** |
| C2 (private data ACL) | CRITICAL | Confirmed mechanism | HIGH-CRITICAL | **CRITICAL** |
| C3 (bot.sock 0666) | CRITICAL | CRITICAL (dirty_sock ref) | HIGH, CRITICAL w/ H1 | **CRITICAL** |
| H1 (no sanitization) | HIGH | -- | HIGH (CRITICAL w/ C3) | **HIGH** |
| H2 (sudo ALL target) | HIGH | -- | HIGH-CRITICAL | **HIGH** |
| H3 (SMTP open) | HIGH | -- | MEDIUM-HIGH | **HIGH** |
| H4 (rate limiting) | MEDIUM | -- | HIGH | **HIGH** |
| H5 (iptables+fail2ban) | HIGH | -- | MEDIUM combined | **HIGH** (combined) |
| M1 (CI/CD trust) | MEDIUM | -- | CRITICAL | **HIGH** (merged w/ C1) |

**Key disagreement:** Codex rated network hardening findings (H3, H5) lower than Claude, arguing SSH key-only auth and GCP firewall provide adequate baseline. Claude maintains HIGH rating based on defense-in-depth principle. Codex elevated M4 (rate limiting) and M1 (CI/CD trust) higher than Claude's original rating.

---

## Prioritized Remediation Plan

### Immediate (same day)

| # | Action | Finding | Risk | Effort |
|---|--------|---------|------|--------|
| 1 | Fix private/ ACL: remove `g:dataread` | C2 | Data exposure | 5 min |
| 2 | Change bot.sock to 0660 | C3 | Spoofing | 5 min |
| 3 | Restrict notify-scripts sudo to `(dataread)` | H2 | Privilege escalation | 5 min |

### Short-term (this week)

| # | Action | Finding | Risk | Effort |
|---|--------|---------|------|--------|
| 4 | Replace sudoers wildcards with explicit paths | C1 | Root compromise | 1 hour |
| 5 | Add rate limiting to API endpoints | H4 | Account takeover | 2 hours |
| 6 | Change Postfix to loopback-only | H3 | Open relay | 5 min |
| 7 | Install and configure fail2ban | H5 | Brute force | 30 min |
| 8 | Configure basic iptables rules | H5 | Network exposure | 30 min |

### Medium-term (this month)

| # | Action | Finding | Risk | Effort |
|---|--------|---------|------|--------|
| 9 | GitHub branch protection on main | C1/M1 | Supply chain | 30 min |
| 10 | Pin GitHub Actions to SHA | M4 | Supply chain | 15 min |
| 11 | Add content sanitization to notification pipeline | H1 | Phishing/injection | 4 hours |
| 12 | Review notification_images.py for path traversal | M3 | File disclosure | 1 hour |
| 13 | Add systemd hardening directives | M6 | Lateral movement | 2 hours |
| 14 | Deploy home dir to 750 | M7 | Info disclosure | 1 min |
| 15 | Install auditd | L2 | Forensics | 1 hour |

---

## Appendix: Methodology

### Tools and Approach

1. **Repository analysis**: Full source code review of all 124 files in the repository, focusing on:
   - `server/sudoers-deploy`, `server/sudoers-webapp` (privilege escalation)
   - `server/deploy.sh` (CI/CD pipeline)
   - `server/bin/notify-runner`, `server/bin/notify-scripts` (notification pipeline)
   - `server/telegram_bot/` (bot service, dispatch, runner)
   - `server/ws_gateway/` (WebSocket gateway, JWT auth)
   - `webapp/desktop_auth.py`, `webapp/user_service.py` (auth flows)
   - `.github/workflows/deploy.yml` (CI/CD configuration)

2. **Live server inspection** (read-only, via SSH as padak):
   - File permissions: `ls -la`, `stat`, `getfacl` on all critical paths
   - Socket permissions: `/run/notify-bot/`, `/run/ws-gateway/`
   - Group memberships: `getent group` for dataread, data-private, data-ops
   - Service status: `systemctl list-units`
   - Network: `ss -tlnp`, iptables, SSH config, nginx config
   - Crontabs: all users checked
   - Access control test: `sudo -u fisa ls` on private directory

3. **Validation**: Perplexity Sonar search for CVE references and best practices on:
   - Unix socket 0666 security (dirty_sock CVE-2019-7304)
   - POSIX ACL mask interaction
   - Sudoers wildcard exploitation (CVE-2026-22536 pattern)

4. **Second opinion**: OpenAI Codex CLI review of all findings with severity validation and identification of additional attack vectors.

### Out of Scope

- Penetration testing (no active exploitation attempted)
- macOS app binary analysis (source code review only)
- Keboola API security
- Google OAuth implementation details
- GCP project-level IAM configuration
- Network-level traffic analysis
