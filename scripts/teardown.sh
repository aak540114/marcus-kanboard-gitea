#!/usr/bin/env bash
#
# scripts/teardown.sh — cleanly stop everything scripts/setup.sh started:
# every Docker container (Kanboard, Gitea, Marcus, Caddy if TLS was used),
# and a natively-run Marcus process (hybrid mode — see
# scripts/run_marcus_native.sh).
#
# Deliberately does NOT delete any data. Docker's named volumes and the
# host-bind-mounted ./data / ./logs directories all survive a teardown —
# re-running ./scripts/setup.sh afterward picks up right where you left
# off (same Kanboard project, same Gitea repos, same tickets). At the
# end, this script prints every location that holds real data, with
# rough sizes, so YOU can decide what (if anything) to delete by hand.
#
# Usage: ./scripts/teardown.sh

set -uo pipefail
# Deliberately NOT `set -e`: this script's whole job is "try to stop
# everything, then report" — a container that's already stopped, a
# missing PID file, or a volume that was never created are all normal,
# expected outcomes here, not failures that should abort the report.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

log()  { echo "==> $*"; }
warn() { echo "warning: $*" >&2; }

env_get() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2- || true
}

# ---------------------------------------------------------------------
# 1. Stop a natively-run Marcus (hybrid mode), if any.
# ---------------------------------------------------------------------

log "Checking for a natively-run Marcus process..."
PID_FILE="$REPO_ROOT/.marcus_native.pid"
if [ -f "$PID_FILE" ]; then
    native_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$native_pid" ] && kill -0 "$native_pid" 2>/dev/null; then
        log "Stopping native Marcus (PID $native_pid)..."
        kill "$native_pid" 2>/dev/null || true
        # Give it a few seconds to shut down gracefully — HumanGatedWorkflow's
        # own stop() tears down any running dev-environment containers on
        # the way out, which is worth letting finish rather than force-killing.
        stopped="false"
        for _ in 1 2 3 4 5; do
            if ! kill -0 "$native_pid" 2>/dev/null; then
                stopped="true"
                break
            fi
            sleep 1
        done
        if [ "$stopped" != "true" ]; then
            log "Still running after 5s — force killing (PID $native_pid)."
            kill -9 "$native_pid" 2>/dev/null || true
        fi
    else
        log "PID file present but process $native_pid isn't running (stale) — ignoring."
    fi
    rm -f "$PID_FILE"
else
    log "No native Marcus PID file — checking for a stray process anyway..."
    # Fallback for Marcus started without going through
    # run_marcus_native.sh's PID tracking (an older run, or started by
    # hand). Only ever REPORTS a match — never kills something this
    # script didn't start itself without a clear, traceable PID file.
    stray_pids="$(pgrep -f "src\.marcus_mcp\.server.*--http" 2>/dev/null || true)"
    if [ -n "$stray_pids" ]; then
        log "Found process(es) matching Marcus's run command with no PID file: $stray_pids"
        log "Stop them yourself if they should be: kill $stray_pids"
    fi
fi

# ---------------------------------------------------------------------
# 2. Stop and remove Docker containers (Kanboard, Gitea, Marcus, Caddy).
# ---------------------------------------------------------------------

log "Stopping Docker containers..."
COMPOSE_FILES=(-f docker-compose.yml)
if [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ]; then
    COMPOSE_FILES+=(-f docker-compose.tls.yml)
fi
# --profile docker-marcus: the marcus service only has a chance of being
# in this project's container set when this profile is active (see
# docker-compose.yml) — always pass it here so `down` sees (and removes)
# it regardless of which run mode was actually chosen last.
if ! docker compose "${COMPOSE_FILES[@]}" --profile docker-marcus down; then
    warn "docker compose down reported an error — see above. Continuing to the data report regardless."
fi

# Remove ad-hoc per-ticket dev-environment preview containers. Marcus starts
# these with `docker run` (NOT docker compose — they're one-per-ticket and
# created on demand), so the `docker compose down` above never touches them.
# A leftover preview container keeps running — and keeps holding its
# published host port — across a teardown, which can block a later setup
# (e.g. a stray app server squatting on a port Gitea needs). This runs even
# when Marcus wasn't up to tear them down itself.
log "Removing leftover dev-environment preview containers (marcus-dev-*)..."
dev_containers="$(docker ps -aq --filter "name=marcus-dev-" 2>/dev/null || true)"
if [ -n "$dev_containers" ]; then
    # shellcheck disable=SC2086
    if docker rm -f $dev_containers >/dev/null 2>&1; then
        log "Removed $(echo "$dev_containers" | grep -c .) dev-environment container(s)."
    else
        warn "Could not remove some marcus-dev-* containers — remove by hand: docker rm -f <name>"
    fi
else
    log "No dev-environment preview containers to remove."
fi

# ---------------------------------------------------------------------
# 3. Report every location holding real data — nothing here is deleted.
# ---------------------------------------------------------------------

echo
echo "======================================================================"
echo " Teardown complete — containers stopped, nothing deleted."
echo "======================================================================"
echo
echo "Re-running ./scripts/setup.sh will pick up right where you left off"
echo "(same Kanboard project, same Gitea repos, same tickets) — none of the"
echo "locations below were touched. Delete them yourself only if you want"
echo "a truly clean slate."
echo

dir_size() {
    # Portable-ish: `du -sh` exists on both macOS and Linux, just with
    # slightly different flag support — -sh alone works on both.
    [ -d "$1" ] && du -sh "$1" 2>/dev/null | cut -f1 || echo "?"
}

echo "Host directories (bind-mounted — directly deletable with rm -rf):"
if [ -d "$REPO_ROOT/data" ]; then
    echo "  - $REPO_ROOT/data      ($(dir_size "$REPO_ROOT/data"))  — Marcus state: ticket lifecycle,"
    echo "                                          which projects Marcus is enabled for,"
    echo "                                          gate/dev-env settings, project↔Gitea-repo mapping,"
    echo "                                          local git clones (data/repos/), cost-tracking DB"
    if [ -f "$REPO_ROOT/data/project_access_settings.json" ]; then
        echo "                                          (kept: re-running setup.sh restores the same"
        echo "                                           per-project Marcus ON/OFF state you had)"
    fi
else
    echo "  - $REPO_ROOT/data      (not present — nothing was ever written here)"
fi
if [ -d "$REPO_ROOT/logs" ]; then
    echo "  - $REPO_ROOT/logs      ($(dir_size "$REPO_ROOT/logs"))  — Marcus logs"
else
    echo "  - $REPO_ROOT/logs      (not present)"
fi
echo

echo "Docker-managed named volumes (NOT plain host folders — use 'docker volume rm',"
echo "not rm -rf; on Docker Desktop for macOS/Windows their real storage lives inside"
echo "the Docker VM, not anywhere directly browsable from Finder/Explorer):"
project_name="$(docker compose "${COMPOSE_FILES[@]}" --profile docker-marcus config --format json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)"
found_any_volume="false"
for vol_name in kanboard_data gitea_data caddy_data caddy_config; do
    [ -n "$project_name" ] || break
    full_vol="${project_name}_${vol_name}"
    mountpoint="$(docker volume inspect --format '{{.Mountpoint}}' "$full_vol" 2>/dev/null || true)"
    if [ -n "$mountpoint" ]; then
        echo "  - $full_vol  →  $mountpoint"
        found_any_volume="true"
    fi
done
if [ "$found_any_volume" != "true" ]; then
    echo "  (none found — Docker daemon unreachable, or these were never created / already removed)"
else
    echo "  To delete all of them: docker volume rm ${project_name}_kanboard_data ${project_name}_gitea_data"
    echo "  (add ${project_name}_caddy_data ${project_name}_caddy_config too if you used the HTTPS option)"
fi
echo

echo "Credentials and secrets:"
if [ -f "$ENV_FILE" ]; then
    echo "  - $ENV_FILE  — every generated token/password from setup.sh (Kanboard API token,"
    echo "                 webhook secrets, Gitea admin password/token, MARCUS_AGENT_TOKEN, ...)."
    echo "                 Delete this too if you want setup.sh to generate all-new credentials"
    echo "                 on the next run, rather than reusing what's already provisioned."
else
    echo "  - .env not present"
fi
echo
echo "NOT Marcus's data — do NOT delete unless you specifically want to log out of Claude Code:"
echo "  - ~/.claude.json and ~/.claude/.credentials.json are YOUR real Claude Code login,"
echo "    only ever READ by Marcus (claude_subscription provider) — never written or owned by it."
echo "======================================================================"
