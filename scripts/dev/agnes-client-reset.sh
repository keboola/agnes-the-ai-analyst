#!/usr/bin/env bash
# Wipe every trace of an Agnes *client* install from this machine, so a
# developer can re-run the onboarding prompt (see app/web/setup_instructions.py)
# from a clean slate. Mirror image of that prompt — keep them in sync.
#
# Touches only the current user's HOME (no admin/root needed) except the Linux
# system trust-store path, which falls back to a warning if sudo is missing.
#
# Usage:
#   scripts/dev/agnes-client-reset.sh           # interactive confirm
#   scripts/dev/agnes-client-reset.sh --yes     # non-interactive
#   scripts/dev/agnes-client-reset.sh --dry-run # print actions only
#
# Cross-platform: Git Bash on Windows, macOS, Linux. Detected via uname.

set -u  # not -e: every step is best-effort and may legitimately no-op.

YES=0
DRY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)     YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

case "$(uname -s)" in
    Darwin)               PLATFORM=macos ;;
    Linux)                PLATFORM=linux ;;
    MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
    *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

run() {
    echo "  \$ $*"
    if [ "$DRY" -eq 0 ]; then
        eval "$@"
    fi
}

step() { echo; echo "==> $*"; }

if [ "$YES" -eq 0 ] && [ "$DRY" -eq 0 ]; then
    cat <<EOF
This will remove the Agnes client install from this machine:
  - 'agnes' CLI (uv tool uninstall + uv cache clean)
  - ~/.config/agnes (token, server URL, sync state)
  - ~/.agnes/ca.pem, ~/.agnes/ca-bundle.pem (TLS bootstrap)
  - ~/.agnes/marketplace (local clone of the per-user marketplace)
  - ~/.agnes/refresh.log, ~/.agnes/refresh.status (refresh audit + statusline state)
  - ~/.claude/skills/agnes (skills cached on disk)
  - ~/.claude/plugins/marketplaces/agnes (Claude's marketplace registration)
  - ~/.claude/plugins/cache/agnes (Claude's per-plugin install cache)
  - Claude Code marketplace 'agnes' + its plugins (best-effort via claude CLI)
  - 'AGNES_CA_PEM_TRUST' block from your shell rc
  - Agnes CA from the OS trust store (certutil / keychain / ca-certificates)
  - /tmp/agnes*.whl

NOT removed (workspace-specific, can't enumerate from here):
  - SessionStart / SessionEnd hooks in any <workspace>/.claude/settings.json
    you ran 'agnes init' in. Those reference 'agnes pull' /
    'agnes refresh-marketplace' / 'agnes push' and stay until you either
    re-init that workspace or delete the file. They're harmless when the
    CLI is uninstalled (the hook command becomes a no-op via '|| true').
  - <workspace>/.claude/agnes-statusline.sh and the matching
    `statusLine` block in <workspace>/.claude/settings.json. The script
    silent-noops when ~/.agnes/refresh.status is missing (which it is
    after this reset), so the statusline just renders nothing.

Platform: $PLATFORM
EOF
    printf "Continue? [y/N] "
    read -r REPLY
    case "$REPLY" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ---------------------------------------------------------------------------
# 1. Remove the Agnes CA from the OS trust store BEFORE we delete ~/.agnes —
#    Windows certutil and macOS `security` need the cert PEM (or its hash) to
#    locate the right entry.
# ---------------------------------------------------------------------------
step "Remove Agnes CA from OS trust store"
CA_PEM="$HOME/.agnes/ca.pem"
if [ -f "$CA_PEM" ]; then
    case "$PLATFORM" in
        windows)
            # certutil accepts the SHA1 thumbprint with colons stripped.
            HASH="$(openssl x509 -in "$CA_PEM" -noout -fingerprint -sha1 2>/dev/null \
                    | sed 's/^.*=//;s/://g')"
            if [ -n "$HASH" ]; then
                run "certutil.exe -user -delstore \"Root\" \"$HASH\""
            else
                echo "  (could not compute SHA1 fingerprint — openssl missing?)"
            fi
            ;;
        macos)
            # Match by SHA1 hash (-Z) so we delete only the exact Agnes cert,
            # never a same-CN cert from an unrelated source.
            HASH="$(openssl x509 -in "$CA_PEM" -noout -fingerprint -sha1 2>/dev/null \
                    | sed 's/^.*=//;s/://g')"
            if [ -n "$HASH" ]; then
                run "security delete-certificate -Z \"$HASH\" \"$HOME/Library/Keychains/login.keychain-db\""
            else
                echo "  (could not compute SHA1 fingerprint — openssl missing?)"
            fi
            ;;
        linux)
            if [ -f /usr/local/share/ca-certificates/agnes.crt ]; then
                run "sudo rm -f /usr/local/share/ca-certificates/agnes.crt"
                run "sudo update-ca-certificates --fresh"
            elif [ -f /etc/pki/ca-trust/source/anchors/agnes.crt ]; then
                run "sudo rm -f /etc/pki/ca-trust/source/anchors/agnes.crt"
                run "sudo update-ca-trust"
            else
                echo "  (no agnes.crt found in system anchors — nothing to do)"
            fi
            ;;
    esac
else
    echo "  (no $CA_PEM — skipping OS trust-store cleanup)"
fi

# ---------------------------------------------------------------------------
# 2. Claude Code marketplace + plugins. Best-effort: claude CLI may not exist.
#    Marketplace name is hard-coded in app/marketplace_server/packager.py as
#    'agnes' — keep this string in sync if it ever changes.
# ---------------------------------------------------------------------------
step "Remove Claude Code marketplace + plugins"
if command -v claude >/dev/null 2>&1; then
    # Removing the marketplace also detaches its plugins from any project
    # that referenced them (they go orphaned on next claude start).
    run "claude plugin marketplace remove agnes 2>/dev/null || true"
    echo "  Note: per-project plugin entries persist in each project's"
    echo "  .claude/settings.json until you re-init that project."
else
    echo "  (claude CLI not found — skipping marketplace removal)"
fi

# ---------------------------------------------------------------------------
# 3. The 'agnes' CLI itself, installed via 'uv tool install'. Plus the uv
#    *build cache* keyed by `agnes-the-ai-analyst==<version>`.
#
#    Why drop the cache too: uv keys its build cache by name+version, and
#    our wheel ships at a stable version string (e.g. `0.38.3`) across many
#    server-side commits. Two distinct builds with the same version number
#    (a stale cached one + a fresh one served from the dashboard wheel
#    endpoint) are indistinguishable to the resolver — `uv tool install
#    --force <https-url>` happily reuses the cached build instead of
#    fetching the new wheel. That's invisible to the operator until they
#    run a freshly-deployed CLI command and find it missing. Reset means
#    "fresh state", so the cache has to go too.
# ---------------------------------------------------------------------------
step "Uninstall 'agnes' CLI"
if command -v uv >/dev/null 2>&1; then
    if uv tool list 2>/dev/null | grep -q '^agnes-the-ai-analyst'; then
        run "uv tool uninstall agnes-the-ai-analyst"
    else
        echo "  (agnes-the-ai-analyst not in 'uv tool list' — skipping)"
    fi
    # Always-safe: `uv cache clean <pkg>` exits 0 with a "no entries" line
    # when the package isn't cached, so it's a no-op when there's nothing
    # to drop. We do this even if uv tool list didn't show the package
    # (the cache and the active install track separately).
    run "uv cache clean agnes-the-ai-analyst 2>/dev/null || true"
else
    echo "  (uv not found — skipping)"
    # Defensive cleanup if uv is gone but the binary lingers.
    [ -e "$HOME/.local/bin/agnes" ] && run "rm -f \"$HOME/.local/bin/agnes\""
fi

# ---------------------------------------------------------------------------
# 4. Filesystem state directories.
# ---------------------------------------------------------------------------
step "Remove Agnes filesystem state"
# Honor the same AGNES_CONFIG_DIR override the CLI reads.
AGNES_CONFIG_DIR_RESOLVED="${AGNES_CONFIG_DIR:-$HOME/.config/agnes}"
# `~/.claude/plugins/cache/agnes/` and `~/.claude/plugins/marketplaces/agnes`
# are normally cleaned by `claude plugin marketplace remove agnes` (step 2),
# but we wipe them defensively because:
#   - `claude` may not be on PATH (e.g. uninstalled in a previous step,
#     fresh machine, etc.) — step 2 silently skips, leaving stale dirs.
#   - Claude Code's cleanup of `cache/` is lazy in some versions; partial
#     dirs from interrupted installs survive `marketplace remove`.
# `rm -rf` handles both file-shaped and dir-shaped registrations
# (the registration entry is a single JSON file when the marketplace was
# added via HTTPS, a full git working tree when added via local path).
for path in \
    "$AGNES_CONFIG_DIR_RESOLVED" \
    "$HOME/.agnes" \
    "$HOME/.claude/skills/agnes" \
    "$HOME/.claude/plugins/marketplaces/agnes" \
    "$HOME/.claude/plugins/cache/agnes" \
; do
    if [ -e "$path" ]; then
        run "rm -rf \"$path\""
    else
        echo "  (no $path — skipping)"
    fi
done

# Wheel cache from step 1 of the install prompt. Match only the exact package
# name (PEP 427 underscore form, the dash form, and the 'agnes.whl' fallback
# from setup_instructions.py:_DEFAULT). A naked /tmp/agnes*.whl glob is too
# loose — it'd catch unrelated wheels that just happen to start with 'agnes'.
step "Remove cached wheel(s)"
# shellcheck disable=SC2086  # glob expansion intentional
WHEELS=$(ls /tmp/agnes_the_ai_analyst-*.whl \
            /tmp/agnes-the-ai-analyst-*.whl \
            /tmp/agnes.whl 2>/dev/null || true)
if [ -n "$WHEELS" ]; then
    # De-dupe (the two normalized forms can both resolve to the same file on
    # case-insensitive filesystems, and `ls` would list it twice).
    for w in $(echo "$WHEELS" | tr ' ' '\n' | sort -u); do
        run "rm -f \"$w\""
    done
else
    echo "  (no Agnes wheel in /tmp — skipping)"
fi

# ---------------------------------------------------------------------------
# 5. Shell rc cleanup. The install heredoc in setup_instructions.py:_tls_trust_block
#    appends a fixed 8-line block: the '# AGNES_CA_PEM_TRUST' marker comment
#    + 7 lines of comments and exports. Delete EXACTLY 8 lines from the marker
#    so we never reach over into unrelated content even if the user hand-edited
#    the block. (`sed ,+Nd` is GNU-only; awk is portable across macOS BSD sed.)
# ---------------------------------------------------------------------------
step "Strip 'AGNES_CA_PEM_TRUST' block from shell rc files"
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -f "$rc" ] || continue
    if grep -q 'AGNES_CA_PEM_TRUST' "$rc" 2>/dev/null; then
        if [ "$DRY" -eq 0 ]; then
            cp "$rc" "$rc.agnes-reset.bak"
            awk '
                /# AGNES_CA_PEM_TRUST/ && skip == 0 { skip = 8 }
                skip > 0 { skip--; next }
                { print }
            ' "$rc.agnes-reset.bak" > "$rc"
            echo "  patched $rc (backup at $rc.agnes-reset.bak)"
        else
            echo "  would patch $rc (delete 8 lines starting at AGNES_CA_PEM_TRUST marker)"
        fi
    else
        echo "  (no AGNES_CA_PEM_TRUST in $rc — skipping)"
    fi
done

step "Done"
cat <<'EOF'
Open a NEW shell (or `source` your rc) so the SSL_CERT_FILE / NODE_EXTRA_CA_CERTS
exports drop out of the environment. You can now re-run the onboarding prompt
from /install on the Agnes server to validate a fresh-machine install.

Sanity checks for "fresh state":
  command -v agnes           # should be absent
  ls ~/.config/agnes ~/.agnes   # both should not exist
  ls ~/.claude/plugins/marketplaces/agnes ~/.claude/plugins/cache/agnes   # both gone
  env | grep -E 'AGNES|SSL_CERT_FILE|NODE_EXTRA_CA_CERTS'   # empty
  claude plugin marketplace list   # no 'agnes' entry

If you used 'agnes init' in workspaces other than the one you're in now,
those workspaces still have:
  <workspace>/.claude/settings.json     # SessionStart/End hooks + statusLine
  <workspace>/.claude/agnes-statusline.sh  # bash script the statusLine calls
  <workspace>/CLAUDE.md                 # RBAC-filtered docs from agnes init
  <workspace>/AGNES_WORKSPACE.md        # human-facing workspace docs
Delete those by hand if you want a fully clean slate per workspace. The
hook commands no-op safely while the CLI is uninstalled (`|| true`), and
the statusline script silent-noops once `~/.agnes/refresh.status` is gone
(which this reset takes care of via the `~/.agnes` recursive cleanup).
EOF
