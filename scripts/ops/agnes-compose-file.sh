#!/bin/sh
# Single source of truth for the docker-compose overlay list
# (the `COMPOSE_FILE` value / `-f` chain every `docker compose` invocation
# on a customer-instance VM needs).
#
# Before this file existed, three independent pieces of code assembled
# this list and disagreed:
#   - infra/modules/customer-instance/startup-script.sh.tpl (VM create/
#     reboot) derived it from instance.yaml's database.backend.
#   - scripts/ops/agnes-auto-upgrade.sh (every 5 min via cron) trusted
#     whatever COMPOSE_FILE happened to already be in /opt/agnes/.env,
#     falling back to a hardcoded list that OMITTED the postgres overlays
#     when .env didn't have one.
#   - scripts/ops/agnes-state-applier.sh (the DB-backend state machine,
#     every 30s) built its own hardcoded `-f` array, appending the
#     postgres overlays AFTER docker-compose.host-mount.yml instead of
#     before docker-compose.postgres-host-mount.yml (wrong order — see
#     agnes_resolve_compose_file's docstring below).
#
# The concrete failure this caused: agnes-state-applier.sh flips
# /data/state/instance.yaml's database.backend to side_car after a
# successful DuckDB->Postgres migration, but it never touched
# /opt/agnes/.env. The very next agnes-auto-upgrade.sh tick then read
# .env's stale COMPOSE_FILE (or hit its own no-postgres fallback) and
# recreated the stack WITHOUT the postgres container, while the app was
# still configured to connect to postgres:5432. The migration only became
# durable after a full VM reboot, where the startup script recomputed
# COMPOSE_FILE from scratch. This was hit live and worked around by
# hand-editing .env.
#
# Every call site now sources this file and calls agnes_resolve_compose_file
# instead of keeping its own copy of the assembly logic.
#
# POSIX sh — sourced by bash callers (agnes-auto-upgrade.sh,
# agnes-state-applier.sh) and by the Terraform-templated startup script,
# so it must not rely on bash-only syntax (arrays, [[ ]]).
#
# Delivery: shipped from the app image at
# /opt/agnes-host/scripts/ops/agnes-compose-file.sh (Dockerfile) and
# extracted to $APP_DIR/scripts/ops/agnes-compose-file.sh by
# startup-script.sh.tpl's existing recursive
# `docker cp .../opt/agnes-host/. $APP_DIR/` (no separate extraction step
# needed). agnes-auto-upgrade.sh also re-fetches it on every tick (it is
# in CONFIG_FILES) so a running VM picks up a fix within 5 minutes, not
# only at the next reboot.

# agnes_tls_active <compose_dir> <state_dir>
#
# True (exit 0) when Caddy should front the app: both cert files are
# non-empty AND the Caddyfile is present and non-empty. Mirrors the check
# agnes-auto-upgrade.sh already applied before this file existed;
# agnes-state-applier.sh's weaker directory-only check
# (`[ -d "$state_dir/certs" ]`, which does not verify the certs actually
# have content) is folded into this single, stricter test.
agnes_tls_active() {
    _acf_cdir=$1
    _acf_sdir=$2
    [ -s "$_acf_sdir/certs/fullchain.pem" ] \
        && [ -s "$_acf_sdir/certs/privkey.pem" ] \
        && [ -s "$_acf_cdir/Caddyfile" ]
}

# agnes_resolve_compose_file <compose_dir> <state_dir> [backend_override]
#
# Prints the colon-separated COMPOSE_FILE value to stdout.
#
#   compose_dir       - directory holding the docker-compose*.yml files
#                        and Caddyfile (/opt/agnes on a provisioned VM).
#   state_dir          - directory holding instance.yaml + certs/
#                        (/data/state on a provisioned VM).
#   backend_override   - optional. When given, used INSTEAD of reading
#                         state_dir/instance.yaml. agnes-state-applier.sh
#                         needs this: its db-state-target.flag flips to
#                         "side-car-enabled" (and the postgres container
#                         must come up) BEFORE instance.yaml's
#                         database.backend leaves the transient
#                         *_in_progress value, so the flag — not the
#                         persisted backend — is authoritative for that
#                         script's own lifecycle decisions.
#
# Order is significant: docker-compose.postgres-host-mount.yml is a
# bridge overlay (see its own header comment) that restates the
# data-migrate service's mount — it must load AFTER BOTH
# docker-compose.postgres.yml (which defines that service) and
# docker-compose.host-mount.yml (which deliberately does not reference
# data-migrate at all, precisely so it stays loadable without the
# postgres overlay) — so it is always last.
agnes_resolve_compose_file() {
    _acf_compose_dir=$1
    _acf_state_dir=$2
    _acf_backend=${3:-}

    if [ -z "$_acf_backend" ] && [ -f "$_acf_state_dir/instance.yaml" ]; then
        # Exact match on "side_car" only — deliberately excludes the
        # transient "side_car_in_progress": engaging the postgres overlay
        # for an in-progress migration could start (or restart) the app
        # against an empty side-car before the migrator has filled it,
        # which is indistinguishable from a healthy instance.
        _acf_backend=$(sed -n 's/^[[:space:]]*backend:[[:space:]]*//p' \
            "$_acf_state_dir/instance.yaml" 2>/dev/null | tr -d '"' | head -1)
    fi

    _acf_list="docker-compose.yml:docker-compose.prod.yml"
    if [ "$_acf_backend" = "side_car" ]; then
        _acf_list="$_acf_list:docker-compose.postgres.yml"
    fi
    _acf_list="$_acf_list:docker-compose.host-mount.yml"
    if [ "$_acf_backend" = "side_car" ]; then
        _acf_list="$_acf_list:docker-compose.postgres-host-mount.yml"
    fi

    if agnes_tls_active "$_acf_compose_dir" "$_acf_state_dir"; then
        _acf_list="$_acf_list:docker-compose.tls.yml"
    fi

    if [ -f "$_acf_compose_dir/docker-compose.gcp-logging.yml" ]; then
        _acf_list="$_acf_list:docker-compose.gcp-logging.yml"
    fi

    printf '%s' "$_acf_list"
}

# agnes_compose_file_missing <resolved> <candidate>
#
# Prints (space-separated) any overlay present in <resolved> but absent
# from <candidate>. Used to decide whether an operator-set /opt/agnes/.env
# COMPOSE_FILE (or a pre-fix hardcoded fallback) is missing an overlay the
# authoritative state requires — empty output means <candidate> already
# satisfies every overlay <resolved> asks for and is safe to keep as-is
# (an operator's extra entries, e.g. an m-tier topology's
# docker-compose.mtier.yml, are preserved rather than clobbered).
agnes_compose_file_missing() {
    _acf_resolved=$1
    _acf_candidate=$2
    _acf_missing=""
    for _acf_f in $(printf '%s' "$_acf_resolved" | tr ':' ' '); do
        case ":$_acf_candidate:" in
            *":$_acf_f:"*) : ;;
            *) _acf_missing="$_acf_missing $_acf_f" ;;
        esac
    done
    printf '%s' "$_acf_missing" | sed 's/^ //'
}
