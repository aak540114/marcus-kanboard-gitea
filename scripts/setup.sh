#!/usr/bin/env bash
#
# scripts/setup.sh — one-command first-time setup for the Marcus +
# Kanboard + Gitea stack.
#
# Provisions everything the manual README steps used to require by hand:
#   - Kanboard: app-level API token (via env var, no UI login)
#   - Kanboard: the target project + its six required columns
#   - Kanboard: the outbound webhook (instant board updates instead of
#     Marcus's 30s poll)
#   - Gitea: admin account + access token
# then builds and starts all three containers.
#
# Safe to re-run: every step checks live state before creating or
# updating anything (see README.md's "How the setup script works" for
# details). Re-running after `docker compose down` is a fast no-op pass;
# re-running after `docker compose down -v` re-provisions everything.
#
# AI provider: this script never prompts for a Claude API key. If this
# machine already has an authenticated `claude` CLI (i.e. you've run
# `claude login` here, the same login Claude Code itself uses), it
# mounts that login into the marcus container and configures Marcus's
# own decomposition/analysis calls to ride your Claude Pro/Max
# subscription (MARCUS_AI_PROVIDER=claude_subscription) — no separate
# API key, no prompt. If `.env` already has CLAUDE_API_KEY set, that
# choice is respected instead (MARCUS_AI_PROVIDER=anthropic). If neither
# is available, the script fails with instructions rather than prompting.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

trap 'echo "[setup.sh] failed at line $LINENO — see the error above." >&2' ERR

log()  { echo "==> $*"; }
err()  { echo "error: $*" >&2; }

# Return 0 iff this machine has a real, authenticated `claude` subscription
# login. Deliberately checks for the `oauthAccount` key inside
# ~/.claude.json (written only when logged into a subscription) rather than
# just the file's existence — otherwise a bare `{}` placeholder (which this
# script itself writes below so the container bind-mount source exists) or
# an installed-but-never-logged-in CLI would falsely pass, and Marcus would
# select claude_subscription and then fail every AI call at runtime.
claude_login_present() {
    command -v claude >/dev/null 2>&1 || return 1
    [ -f "$HOME/.claude.json" ] || return 1
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("oauthAccount") else 1)' \
        "$HOME/.claude.json" 2>/dev/null
}

# ---------------------------------------------------------------------
# .env helpers — idempotent get/set against a simple KEY=VALUE file.
# ---------------------------------------------------------------------

touch "$ENV_FILE"

env_get() {
    local key="$1"
    # "not found" is a normal outcome for this helper, not an error — the
    # trailing `|| true` stops a missing key's non-zero grep/pipefail exit
    # from propagating through a bare `var="$(env_get X)"` assignment and
    # killing the whole script under `set -e` (unlike `[ -z "$(env_get X)" ]`
    # checks, a bare assignment's exit status IS the substitution's exit
    # status, and that's exactly what happens for GITEA_TOKEN on a
    # brand-new .env before it's ever been generated).
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2- || true
}

env_set() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    echo "${key}=${value}" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# ---------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------

log "Checking prerequisites..."
for cmd in docker curl python3 openssl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        err "'$cmd' is required but not found on PATH."
        exit 1
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    err "'docker compose' (v2 plugin) is required. Install Docker Desktop or the compose-plugin package."
    exit 1
fi

# ---------------------------------------------------------------------
# 2. .env bootstrap — generate anything missing, keep anything present.
# ---------------------------------------------------------------------

log "Preparing .env..."

if [ -z "$(env_get KANBOARD_API_TOKEN)" ]; then
    env_set KANBOARD_API_TOKEN "$(openssl rand -hex 32)"
fi
if [ -z "$(env_get KANBOARD_WEBHOOK_TOKEN)" ]; then
    env_set KANBOARD_WEBHOOK_TOKEN "$(openssl rand -hex 32)"
fi
if [ -z "$(env_get KANBOARD_PROJECT_NAME)" ]; then
    env_set KANBOARD_PROJECT_NAME "Marcus Project"
fi
if [ -z "$(env_get GITEA_ADMIN_PASSWORD)" ]; then
    # Fixed, predictable default — matches Kanboard's own admin/admin
    # default. This stack is intended for local/demo use, not exposed to
    # the internet. Override by setting GITEA_ADMIN_PASSWORD in .env
    # before running this script.
    env_set GITEA_ADMIN_PASSWORD "Marcus123!"
fi

log "Selecting AI provider..."

if [ -n "$(env_get MARCUS_AI_PROVIDER)" ]; then
    # Explicit choice in .env always wins — never second-guess it.
    log "MARCUS_AI_PROVIDER=$(env_get MARCUS_AI_PROVIDER) already set in .env — leaving it as-is."
elif [ -n "$(env_get CLAUDE_API_KEY)" ]; then
    # An API key is configured but no provider chosen — use the metered
    # API provider (the key's presence is the signal of intent).
    env_set MARCUS_AI_PROVIDER "anthropic"
    log "CLAUDE_API_KEY found in .env — using the 'anthropic' provider."
elif claude_login_present; then
    env_set MARCUS_AI_PROVIDER "claude_subscription"
    log "Found an authenticated 'claude' CLI login — using the 'claude_subscription' provider (your Claude Pro/Max subscription, no API key)."
    case "$(uname -s)" in
        Darwin)
            # On macOS the CLI keeps its OAuth token in the login Keychain,
            # NOT in ~/.claude/.credentials.json — so the credential file
            # bind-mounted into the (Linux) container is empty and the
            # container's claude cannot authenticate. Warn loudly rather
            # than let it fail silently at first AI call.
            log "WARNING: on macOS the 'claude' login token lives in the Keychain, which cannot be shared into a Linux container."
            log "         The claude_subscription provider will most likely FAIL inside Docker on this host. If Marcus's AI"
            log "         calls error out (docker compose logs marcus), set CLAUDE_API_KEY in .env and re-run to use the API provider."
            ;;
    esac
else
    err "No Claude API key configured and no authenticated 'claude' CLI login found on this machine."
    err "Either:"
    err "  - run 'claude login' here, then re-run ./scripts/setup.sh, or"
    err "  - set an API key yourself: echo 'CLAUDE_API_KEY=sk-ant-...' >> .env, then re-run ./scripts/setup.sh"
    exit 1
fi

# The claude-credential bind-mount sources in docker-compose.yml must
# exist on the host, because Docker Compose *silently creates a
# root-owned directory* at a bind-mount source path that doesn't exist
# (it does NOT fail) — and a directory where claude expects its config
# file would break both the container's CLI and the host's own Claude
# Code. Pre-create them as empty files so the mount binds a real file.
# Never overwrites a real login if one is already there. NOTE: an empty
# `{}` here is only a placeholder to satisfy the mount; the
# claude_login_present() check above deliberately ignores `{}` so a
# leftover placeholder from a prior run can never be mistaken for a real
# login on a re-run.
mkdir -p "$HOME/.claude"
[ -f "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
[ -f "$HOME/.claude/.credentials.json" ] || echo '{}' > "$HOME/.claude/.credentials.json"

log "Configuring network access..."

# COMPOSE_FILES is the set of compose files every subsequent `docker
# compose` call in this script uses. The TLS overlay is appended to it
# only when the operator opts into HTTPS below.
COMPOSE_FILES=(-f docker-compose.yml)

if [ -z "$(env_get MARCUS_BIND_HOST)" ]; then
    if [ -t 0 ]; then
        # `|| allow_remote=""` so a Ctrl-D (EOF) at the prompt falls
        # through to the safe "No" default instead of returning non-zero
        # and aborting the whole script under `set -e`.
        allow_remote=""
        read -r -p "Allow OTHER machines (e.g. a remote VPS, agents on other hosts) to reach this stack? [y/N]: " allow_remote || allow_remote=""
        case "$allow_remote" in
            [yY]|[yY][eE][sS])
                # Remote access opted in. Two things make this safe(r):
                # (1) a bearer token every agent must present, so
                # unaccounted agents are rejected; (2) an optional HTTPS
                # reverse proxy so the token isn't sent in cleartext.
                if [ -z "$(env_get MARCUS_AGENT_TOKEN)" ]; then
                    env_set MARCUS_AGENT_TOKEN "$(openssl rand -hex 32)"
                    log "Generated MARCUS_AGENT_TOKEN — connecting agents must present it as 'Authorization: Bearer <token>'."
                fi

                tls_domain=""
                read -r -p "Terminate HTTPS with a built-in proxy? Enter a public domain for a real (Let's Encrypt) cert, or leave blank for plain HTTP: " tls_domain || tls_domain=""
                if [ -n "$tls_domain" ]; then
                    # TLS mode: only Caddy (443) is exposed off-host; the
                    # stack itself stays on loopback.
                    env_set MARCUS_BIND_HOST "127.0.0.1"
                    env_set MARCUS_PUBLIC_DOMAIN "$tls_domain"
                    acme_email=""
                    read -r -p "  Email for Let's Encrypt (optional, press Enter to skip): " acme_email || acme_email=""
                    env_set MARCUS_ACME_EMAIL "$acme_email"
                    COMPOSE_FILES+=(-f docker-compose.tls.yml)
                    log "HTTPS enabled via built-in Caddy proxy for https://${tls_domain}/ — Marcus/Kanboard/Gitea stay on loopback; only 443 is exposed."
                    log "Requires DNS for ${tls_domain} to point at this host and ports 80+443 reachable from the internet."
                else
                    env_set MARCUS_BIND_HOST "0.0.0.0"
                    log "Plain HTTP on all interfaces. Marcus is protected by the bearer token, but the token travels UNENCRYPTED —"
                    log "put the stack behind a VPN/tunnel (Tailscale, WireGuard, Cloudflare Tunnel), or re-run and provide a domain for HTTPS."
                    log "Kanboard/Gitea also use default credentials — change them before real use."
                fi
                ;;
            *)
                env_set MARCUS_BIND_HOST "127.0.0.1"
                log "Marcus, Kanboard, and Gitea will only be reachable from this machine (127.0.0.1). No agent token needed for local-only use."
                ;;
        esac
    else
        # No terminal to ask with — default to the safe choice
        # (localhost-only) instead of guessing "yes" and exposing a port
        # to the network without the operator explicitly opting in.
        env_set MARCUS_BIND_HOST "127.0.0.1"
        log "No terminal available to ask — defaulting to localhost-only access (127.0.0.1)."
        log "To allow remote agents: set MARCUS_BIND_HOST=0.0.0.0 and MARCUS_AGENT_TOKEN=\$(openssl rand -hex 32) in .env before re-running."
    fi
else
    log "MARCUS_BIND_HOST=$(env_get MARCUS_BIND_HOST) already set in .env — leaving it as-is."
    # Honor a pre-existing TLS choice on re-runs.
    if [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ]; then
        COMPOSE_FILES+=(-f docker-compose.tls.yml)
        log "MARCUS_PUBLIC_DOMAIN set — including the HTTPS (Caddy) overlay."
    fi
fi

# ---------------------------------------------------------------------
# 3. Start Kanboard + Gitea only — Marcus needs values these produce.
# ---------------------------------------------------------------------

log "Starting Kanboard and Gitea..."
if ! docker compose up -d --wait --wait-timeout 120 kanboard gitea; then
    err "Kanboard and/or Gitea did not become healthy in time."
    docker compose logs kanboard gitea --tail=50 || true
    exit 1
fi

# ---------------------------------------------------------------------
# 4. Provision the Kanboard project + columns.
# ---------------------------------------------------------------------

log "Provisioning Kanboard project and columns..."
project_id="$(python3 "$SCRIPT_DIR/provision_kanboard.py" \
    --url "http://localhost:8080/jsonrpc.php" \
    --token "$(env_get KANBOARD_API_TOKEN)" \
    --project-name "$(env_get KANBOARD_PROJECT_NAME)")"
env_set KANBOARD_PROJECT_ID "$project_id"
log "Kanboard project id: $project_id"

# ---------------------------------------------------------------------
# 5. Seed the Kanboard webhook so board changes reach Marcus instantly
#    instead of on the next 30s poll. Kanboard has no JSON-RPC method or
#    env var for this setting — it's a plain key/value row in its
#    `settings` SQLite table (option='webhook_url'/'webhook_token'),
#    read fresh on every event with no caching, so this write takes
#    effect immediately with no Kanboard restart needed.
# ---------------------------------------------------------------------

log "Seeding Kanboard webhook..."
webhook_seeded="false"
for webhook_attempt in 1 2 3 4 5; do
    # SQLite allows one writer at a time; Kanboard's own PHP process can
    # briefly hold the lock right after the healthcheck passes (session
    # writes, first-boot migrations still settling). Retry a few times
    # rather than treat a transient "database is locked" as fatal.
    if docker compose exec -T kanboard php -r '
$token = $argv[1];
$pdo = new PDO("sqlite:/var/www/app/data/db.sqlite");
$stmt = $pdo->prepare(
    "INSERT INTO settings (option, value) VALUES (?, ?) " .
    "ON CONFLICT(option) DO UPDATE SET value=excluded.value"
);
$stmt->execute(["webhook_url", "http://marcus:4298/webhooks/kanboard"]);
$stmt->execute(["webhook_token", $token]);
' -- "$(env_get KANBOARD_WEBHOOK_TOKEN)"; then
        webhook_seeded="true"
        break
    fi
    log "Webhook seed attempt $webhook_attempt failed (likely a transient SQLite lock) — retrying..."
    sleep 2
done
if [ "$webhook_seeded" != "true" ]; then
    err "Could not seed the Kanboard webhook after 5 attempts."
    exit 1
fi
log "Webhook configured: http://marcus:4298/webhooks/kanboard"

# ---------------------------------------------------------------------
# 6. Gitea: admin account + access token.
# ---------------------------------------------------------------------

log "Setting up Gitea admin account..."
create_log="$(mktemp)"
if ! docker compose exec -T -u git gitea gitea admin user create \
        --username root --password "$(env_get GITEA_ADMIN_PASSWORD)" \
        --email root@example.com --admin --must-change-password=false \
        > "$create_log" 2>&1; then
    if grep -qi "user already exists" "$create_log"; then
        log "Gitea admin account already exists — skipping."
    else
        err "Failed to create Gitea admin account:"
        cat "$create_log" >&2
        rm -f "$create_log"
        exit 1
    fi
fi
rm -f "$create_log"

log "Checking for a valid Gitea access token..."
gitea_token="$(env_get GITEA_TOKEN)"
token_valid="false"
if [ -n "$gitea_token" ]; then
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: token ${gitea_token}" \
        http://localhost:3000/api/v1/user || echo 000)"
    [ "$http_code" = "200" ] && token_valid="true"
fi

if [ "$token_valid" = "false" ]; then
    log "Generating a new Gitea access token..."
    token_name="marcus-$(date +%s)"
    gitea_token="$(docker compose exec -T -u git gitea gitea admin user generate-access-token \
        --username root --token-name "$token_name" \
        --scopes write:repository,read:user --raw | tr -d '\r\n')"
    env_set GITEA_TOKEN "$gitea_token"
    log "Gitea token generated."
else
    log "Existing Gitea token is still valid — reusing it."
fi

# ---------------------------------------------------------------------
# 7. Build and start Marcus now that .env has everything it needs.
# ---------------------------------------------------------------------

log "Building and starting Marcus..."
# Start marcus (always) plus caddy (only when the TLS overlay is active —
# MARCUS_PUBLIC_DOMAIN set adds -f docker-compose.tls.yml to COMPOSE_FILES).
start_services=(marcus)
if [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ]; then
    start_services+=(caddy)
fi
if ! docker compose "${COMPOSE_FILES[@]}" up -d --build --wait --wait-timeout 90 "${start_services[@]}"; then
    err "Marcus (or the TLS proxy) did not become healthy in time — most likely a KANBOARD_API_TOKEN mismatch, a missing ~/.claude.json / ~/.claude/.credentials.json for the claude_subscription provider, or (TLS) DNS/ports for MARCUS_PUBLIC_DOMAIN not yet reachable."
    docker compose "${COMPOSE_FILES[@]}" logs "${start_services[@]}" --tail=50 || true
    exit 1
fi

# ---------------------------------------------------------------------
# 8. Summary.
# ---------------------------------------------------------------------

echo
echo "======================================================================"
echo " Setup complete."
echo "======================================================================"
host_port="$(env_get MARCUS_PORT)"
host_port="${host_port:-4298}"

# Bearer-token suffix for the connect command, when a token is configured.
agent_token="$(env_get MARCUS_AGENT_TOKEN)"
auth_flag=""
if [ -n "$agent_token" ]; then
    auth_flag=" \\
     -H \"Authorization: Bearer ${agent_token}\""
fi

echo " Kanboard:  http://localhost:8080   (admin / admin)"
echo " Gitea:     http://localhost:3000   (root / $(env_get GITEA_ADMIN_PASSWORD))"
echo " Marcus:    http://localhost:${host_port}/mcp"
echo
echo " Kanboard project: $(env_get KANBOARD_PROJECT_NAME) (id $(env_get KANBOARD_PROJECT_ID))"
echo " Webhook:   configured — board changes reach Marcus instantly."
echo " AI provider: $(env_get MARCUS_AI_PROVIDER) (Marcus's own decomposition/analysis calls)"
if [ -n "$agent_token" ]; then
    echo " Agent auth: REQUIRED — connecting agents must pass the bearer token below."
else
    echo " Agent auth: none (localhost-only). Set MARCUS_AGENT_TOKEN before exposing remotely."
fi
echo
echo " Connect an AI agent from this machine:"
echo "   claude mcp add --transport http marcus http://localhost:${host_port}/mcp${auth_flag}"
echo

bind_host="$(env_get MARCUS_BIND_HOST)"
tls_domain="$(env_get MARCUS_PUBLIC_DOMAIN)"
if [ -n "$tls_domain" ]; then
    echo " Remote access: ENABLED over HTTPS via built-in proxy (https://${tls_domain}/)."
    echo " The stack itself stays on loopback; only the proxy's 443 is exposed. From another machine:"
    echo "   claude mcp add --transport http marcus https://${tls_domain}/mcp${auth_flag}"
    echo " (A real cert requires DNS for ${tls_domain} → this host and ports 80+443 open. Give"
    echo "  Caddy a minute on first run to obtain the certificate.)"
else
    case "$bind_host" in
        127.0.0.1|localhost|"")
            echo " Remote access: DISABLED — the stack only accepts connections from this machine."
            echo " To allow other machines, re-run setup and answer yes (or set MARCUS_BIND_HOST=0.0.0.0"
            echo " and MARCUS_AGENT_TOKEN in .env, then: docker compose up -d --build)."
            ;;
        *)
            conn_host="$bind_host"
            [ "$conn_host" = "0.0.0.0" ] && conn_host="<this-machine's-address>"
            echo " Remote access: ENABLED over plain HTTP (bound to ${bind_host}) — the bearer token"
            echo " authenticates agents but is sent UNENCRYPTED; use a VPN/tunnel, or re-run for HTTPS."
            echo " From another machine:"
            echo "   claude mcp add --transport http marcus http://${conn_host}:${host_port}/mcp${auth_flag}"
            ;;
    esac
fi
echo
echo " Save the Gitea admin password above if you plan to log in manually —"
echo " it won't be printed again (it's also in .env, which is git-ignored)."
if [ -n "$agent_token" ]; then
    echo " The agent token is stored in .env (git-ignored). Anyone with it can drive the board."
fi
echo "======================================================================"
