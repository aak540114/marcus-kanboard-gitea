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
# then either builds and starts the Marcus container too, or (hybrid
# mode — see the "how should Marcus run?" prompt below) leaves Marcus
# for you to start yourself with ./scripts/run_marcus_native.sh while
# Kanboard/Gitea stay in Docker. See README.md's "Hybrid mode: Marcus
# outside Docker" section for when you'd want that (short version: macOS
# + wanting Marcus's own AI calls to use your Claude Pro/Max subscription
# — Docker's Linux container can't reach the macOS Keychain that login
# lives in, a native process can).
#
# Safe to re-run: every step checks live state before creating or
# updating anything (see README.md's "How the setup script works" for
# details). Re-running after `docker compose down` is a fast no-op pass;
# re-running after `docker compose down -v` re-provisions everything.
#
# AI provider: this script never prompts for a Claude API key. If this
# machine already has an authenticated `claude` CLI (i.e. you've run
# `claude login` here, the same login Claude Code itself uses), it wires
# that login into wherever Marcus runs (mounted into the container in
# Docker mode; read directly from the host in native mode) and
# configures Marcus's own decomposition/analysis calls to ride your
# Claude Pro/Max subscription (MARCUS_AI_PROVIDER=claude_subscription) —
# no separate API key, no prompt. If `.env` already has CLAUDE_API_KEY
# set, that choice is respected instead (MARCUS_AI_PROVIDER=anthropic).
# If neither is available, the script fails with instructions rather
# than prompting.

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
if [ -z "$(env_get GITEA_WEBHOOK_TOKEN)" ]; then
    # Shared HMAC secret for the dev-environment instant-refresh webhook
    # (src/core/gitea_webhook_receiver.py). No Gitea UI step needed to use
    # it: ProjectSyncWorkflow.ensure_repo() calls GiteaManager.create_webhook()
    # automatically the first time each project's repo is provisioned,
    # using this same value (passed through docker-compose.yml's
    # GITEA_WEBHOOK_TOKEN env var) to sign deliveries.
    env_set GITEA_WEBHOOK_TOKEN "$(openssl rand -hex 32)"
fi
if [ -z "$(env_get KANBOARD_PROJECT_NAME)" ]; then
    env_set KANBOARD_PROJECT_NAME "Marcus Project"
fi
if [ -z "$(env_get GITEA_ADMIN_PASSWORD)" ]; then
    # Randomly generated (unlike Kanboard's admin/admin, which Kanboard's
    # own JSON-RPC API has no method to rotate — see the KANBOARD_BIND_HOST
    # comment in docker-compose.yml — Gitea's admin account IS created by
    # this script, so it can just pick a strong password up front instead
    # of shipping a fixed, guessable default). Printed once at the end of
    # this script and saved to .env (git-ignored) — that's the only place
    # it's recorded going forward. Override by setting GITEA_ADMIN_PASSWORD
    # in .env yourself before running this script.
    env_set GITEA_ADMIN_PASSWORD "$(openssl rand -hex 12)"
fi

log "Choosing how Marcus itself runs..."

if [ -z "$(env_get MARCUS_RUN_MODE)" ]; then
    if [ -t 0 ]; then
        echo "How should Marcus run?"
        echo "  1) In Docker, as part of this compose stack (default)"
        echo "  2) Natively on this host — Kanboard/Gitea stay in Docker, only Marcus runs outside it."
        echo "     Recommended on macOS if you want Marcus's own AI calls to use your Claude Pro/Max"
        echo "     subscription: Docker's Linux container can't reach the macOS Keychain where your"
        echo "     'claude login' session actually lives, so the claude_subscription provider silently"
        echo "     fails inside Docker on a Mac. Running natively sidesteps that entirely."
        run_mode_choice=""
        read -r -p "Choice [1/2, default 1]: " run_mode_choice || run_mode_choice=""
        case "$run_mode_choice" in
            2)
                env_set MARCUS_RUN_MODE "native"
                log "Marcus will run natively — this script provisions Kanboard/Gitea/tokens as usual, then"
                log "prints instructions to start Marcus with ./scripts/run_marcus_native.sh instead of building a container for it."
                ;;
            *)
                env_set MARCUS_RUN_MODE "docker"
                ;;
        esac
    else
        env_set MARCUS_RUN_MODE "docker"
        log "No terminal available to ask — defaulting to Marcus in Docker."
    fi
else
    log "MARCUS_RUN_MODE=$(env_get MARCUS_RUN_MODE) already set in .env — leaving it as-is."
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
    if [ "$(env_get MARCUS_RUN_MODE)" = "docker" ]; then
        case "$(uname -s)" in
            Darwin)
                # On macOS the CLI keeps its OAuth token in the login Keychain,
                # NOT in ~/.claude/.credentials.json — so the credential file
                # bind-mounted into the (Linux) container is empty and the
                # container's claude cannot authenticate. Warn loudly rather
                # than let it fail silently at first AI call. (Native mode
                # doesn't have this problem at all — the claude subprocess
                # IS a native macOS process there, so it reads the Keychain
                # directly, same as your interactive `claude login` session.)
                log "WARNING: on macOS the 'claude' login token lives in the Keychain, which cannot be shared into a Linux container."
                log "         The claude_subscription provider will most likely FAIL inside Docker on this host. Either set CLAUDE_API_KEY"
                log "         in .env and re-run, or re-run and choose native mode for Marcus (option 2) to use this login directly."
                ;;
        esac
    fi
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
# login on a re-run. Only relevant in Docker mode — native mode has no
# bind mount at all, the claude subprocess just reads the real files.
if [ "$(env_get MARCUS_RUN_MODE)" = "docker" ]; then
    mkdir -p "$HOME/.claude"
    [ -f "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
    [ -f "$HOME/.claude/.credentials.json" ] || echo '{}' > "$HOME/.claude/.credentials.json"
fi

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
                # Remote access opted in. Three things make this safe(r):
                # (1) a bearer token every AGENT must present to Marcus, so
                # unaccounted agents are rejected; (2) an optional HTTPS
                # reverse proxy so that token isn't sent in cleartext;
                # (3) Kanboard's admin/admin default — which has no
                # API-rotatable password — is replaced with a generated
                # account before its port is ever published, so opening it
                # to HUMANS doesn't also open it to anyone who's read this
                # script (see ensure_admin_user() in provision_kanboard.py).
                if [ -z "$(env_get MARCUS_AGENT_TOKEN)" ]; then
                    env_set MARCUS_AGENT_TOKEN "$(openssl rand -hex 32)"
                    log "Generated MARCUS_AGENT_TOKEN — connecting agents must present it as 'Authorization: Bearer <token>'."
                fi

                # Gitea is NOT proxied by Caddy, so it needs its own direct
                # exposure regardless of the HTTPS choice below — set this
                # now, before that choice pins MARCUS_BIND_HOST to loopback
                # in TLS mode (Gitea must NOT follow Marcus to loopback).
                env_set GITEA_BIND_HOST "0.0.0.0"

                # Kanboard is also NOT proxied by Caddy — same reasoning as
                # Gitea, it gets its own direct port regardless of the TLS
                # choice below. Unlike Gitea's, this account's credentials
                # are generated here (not by Kanboard) since setup.sh is
                # what will actually create them via ensure_admin_user()
                # once Kanboard is up (see step 4 below).
                env_set KANBOARD_BIND_HOST "0.0.0.0"
                if [ -z "$(env_get KANBOARD_ADMIN_USERNAME)" ]; then
                    env_set KANBOARD_ADMIN_USERNAME "marcus_admin"
                fi
                if [ -z "$(env_get KANBOARD_ADMIN_PASSWORD)" ]; then
                    env_set KANBOARD_ADMIN_PASSWORD "$(openssl rand -hex 12)"
                fi

                tls_domain=""
                if [ "$(env_get MARCUS_RUN_MODE)" = "docker" ]; then
                    read -r -p "Terminate HTTPS with a built-in proxy for Marcus? Enter a public domain for a real (Let's Encrypt) cert, or leave blank for plain HTTP: " tls_domain || tls_domain=""
                else
                    log "Marcus run mode is 'native' — the built-in HTTPS proxy (Caddy) only fronts the Marcus"
                    log "Docker container, so it isn't available here. Put your own reverse proxy in front of the"
                    log "native Marcus process if you need TLS, or keep plain HTTP behind a VPN/tunnel."
                fi
                if [ -n "$tls_domain" ]; then
                    # TLS mode: only Caddy (443) is exposed off-host for
                    # Marcus. Gitea/Kanboard (set above) stay directly
                    # published — Caddy here fronts Marcus only.
                    env_set MARCUS_BIND_HOST "127.0.0.1"
                    env_set MARCUS_PUBLIC_DOMAIN "$tls_domain"
                    acme_email=""
                    read -r -p "  Email for Let's Encrypt (optional, press Enter to skip): " acme_email || acme_email=""
                    env_set MARCUS_ACME_EMAIL "$acme_email"
                    COMPOSE_FILES+=(-f docker-compose.tls.yml)
                    # MARCUS_URL is embedded server-side into Kanboard's own
                    # pages (kanboard/plugins/MarcusDevEnv/...) so a human's
                    # BROWSER can fetch it — it must be an address that
                    # browser can resolve, not "localhost" (which would
                    # resolve to the visitor's own machine, not this host).
                    # Deliberately only auto-set here (a real domain is a
                    # known-good, deterministic value); the plain-HTTP branch
                    # below has no reliable public address to guess at.
                    if [ -z "$(env_get MARCUS_URL)" ]; then
                        env_set MARCUS_URL "https://${tls_domain}"
                    fi
                    log "HTTPS enabled via built-in Caddy proxy for https://${tls_domain}/ — Marcus stays on loopback behind it; only 443 is exposed."
                    log "Gitea's own port (3000/2222) and Kanboard's (8080) are still published directly over plain HTTP."
                    log "Requires DNS for ${tls_domain} to point at this host and ports 80+443 reachable from the internet."
                else
                    env_set MARCUS_BIND_HOST "0.0.0.0"
                    log "Plain HTTP on all interfaces. Marcus is protected by the bearer token, but the token travels UNENCRYPTED —"
                    log "put the stack behind a VPN/tunnel (Tailscale, WireGuard, Cloudflare Tunnel), or re-run and provide a domain for HTTPS."
                    if [ -z "$(env_get MARCUS_URL)" ]; then
                        log "NOTE: Kanboard's embedded widgets (Active Agents badge, Gate toggle) fetch Marcus from the"
                        log "browser and default to localhost, which won't resolve for a remote visitor. Set MARCUS_URL"
                        log "in .env to this host's real address if you want those widgets to work remotely."
                    fi
                fi
                ;;
            *)
                env_set MARCUS_BIND_HOST "127.0.0.1"
                env_set GITEA_BIND_HOST "127.0.0.1"
                env_set KANBOARD_BIND_HOST "127.0.0.1"
                log "Marcus, Gitea, and Kanboard will only be reachable from this machine (127.0.0.1). No agent token needed for local-only use."
                ;;
        esac
    else
        # No terminal to ask with — default to the safe choice
        # (localhost-only) instead of guessing "yes" and exposing a port
        # to the network without the operator explicitly opting in.
        env_set MARCUS_BIND_HOST "127.0.0.1"
        env_set GITEA_BIND_HOST "127.0.0.1"
        env_set KANBOARD_BIND_HOST "127.0.0.1"
        log "No terminal available to ask — defaulting to localhost-only access (127.0.0.1)."
        log "To allow remote access: set MARCUS_BIND_HOST=0.0.0.0, GITEA_BIND_HOST=0.0.0.0, KANBOARD_BIND_HOST=0.0.0.0,"
        log "and MARCUS_AGENT_TOKEN=\$(openssl rand -hex 32) in .env before re-running."
    fi
else
    log "MARCUS_BIND_HOST=$(env_get MARCUS_BIND_HOST) already set in .env — leaving it as-is."
    # Honor a pre-existing TLS choice on re-runs.
    if [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ]; then
        COMPOSE_FILES+=(-f docker-compose.tls.yml)
        log "MARCUS_PUBLIC_DOMAIN set — including the HTTPS (Caddy) overlay."
    fi
    # Backfill GITEA_BIND_HOST/KANBOARD_BIND_HOST on a re-run against a .env
    # from before these variables existed: if remote access was previously
    # chosen (either MARCUS_BIND_HOST=0.0.0.0 directly, or TLS mode where
    # it's 127.0.0.1 but remote access is still the operator's intent),
    # Gitea/Kanboard need the same direct exposure they always had —
    # defaulting to 127.0.0.1 on their own would silently break previously-
    # working remote access. Never overwrites an explicit existing value.
    _remote_was_chosen="false"
    [ "$(env_get MARCUS_BIND_HOST)" = "0.0.0.0" ] && _remote_was_chosen="true"
    [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ] && _remote_was_chosen="true"
    if [ "$_remote_was_chosen" = "true" ]; then
        if [ -z "$(env_get GITEA_BIND_HOST)" ]; then
            env_set GITEA_BIND_HOST "0.0.0.0"
            log "Backfilled GITEA_BIND_HOST=0.0.0.0 to match your existing remote-access configuration."
        fi
        if [ -z "$(env_get KANBOARD_BIND_HOST)" ]; then
            env_set KANBOARD_BIND_HOST "0.0.0.0"
            log "Backfilled KANBOARD_BIND_HOST=0.0.0.0 to match your existing remote-access configuration."
        fi
        if [ -z "$(env_get KANBOARD_ADMIN_USERNAME)" ]; then
            env_set KANBOARD_ADMIN_USERNAME "marcus_admin"
        fi
        if [ -z "$(env_get KANBOARD_ADMIN_PASSWORD)" ]; then
            env_set KANBOARD_ADMIN_PASSWORD "$(openssl rand -hex 12)"
        fi
    fi
fi

# ---------------------------------------------------------------------
# 3. Start Kanboard + Gitea only — Marcus needs values these produce.
# ---------------------------------------------------------------------

log "Starting Kanboard and Gitea..."
if ! docker compose up -d --wait --wait-timeout 120 kanboard gitea; then
    err "Kanboard and/or Gitea did not become healthy in time."
    # A very common cause: another process or container is ALREADY holding
    # one of the published host ports (Gitea 3000, Kanboard 8080), so Docker
    # can't bind it. The usual offender is a leftover dev server an AI agent
    # (or you) started to test an app on :3000 — Docker/compose can't stop a
    # non-compose process. Name the culprit so it's obvious.
    for p in 3000 8080; do
        holder=""
        if command -v lsof >/dev/null 2>&1; then
            holder="$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null \
                | awk 'NR>1 {print $1" (pid "$2")"}' | sort -u | tr '\n' ' ')"
        fi
        if [ -n "$holder" ]; then
            err "Port $p is already in use by: ${holder}"
            err "  → Stop it, then re-run setup. If it's a stray dev server: kill that PID."
            err "  → If it's a container: docker ps  then  docker rm -f <name>"
            err "  → Leftover Marcus preview containers: ./scripts/teardown.sh removes them."
        fi
    done
    docker compose logs kanboard gitea --tail=50 || true
    exit 1
fi

# ---------------------------------------------------------------------
# 4. Provision the Kanboard project + columns.
# ---------------------------------------------------------------------

log "Provisioning Kanboard project and columns..."
provision_args=(
    --url "http://localhost:8080/jsonrpc.php"
    --token "$(env_get KANBOARD_API_TOKEN)"
    --project-name "$(env_get KANBOARD_PROJECT_NAME)"
)
# Only replace admin/admin when Kanboard is actually being published beyond
# localhost — for the default local-only deployment this stays as-is, same
# as every other "local/demo use" default in this stack.
if [ "$(env_get KANBOARD_BIND_HOST)" = "0.0.0.0" ]; then
    provision_args+=(
        --secure-admin
        "$(env_get KANBOARD_ADMIN_USERNAME)"
        "$(env_get KANBOARD_ADMIN_PASSWORD)"
    )
fi
project_id="$(python3 "$SCRIPT_DIR/provision_kanboard.py" "${provision_args[@]}")"
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
# Kanboard (a container) needs a different address to reach Marcus
# depending on where Marcus runs: the Docker service name only resolves
# to another container on the same compose network; a natively-run
# Marcus is reached via host.docker.internal instead (see
# scripts/run_marcus_native.sh and the matching extra_hosts entries in
# docker-compose.yml).
if [ "$(env_get MARCUS_RUN_MODE)" = "native" ]; then
    kanboard_webhook_url="http://host.docker.internal:4298/webhooks/kanboard"
else
    kanboard_webhook_url="http://marcus:4298/webhooks/kanboard"
fi
webhook_seeded="false"
for webhook_attempt in 1 2 3 4 5; do
    # SQLite allows one writer at a time; Kanboard's own PHP process can
    # briefly hold the lock right after the healthcheck passes (session
    # writes, first-boot migrations still settling). Retry a few times
    # rather than treat a transient "database is locked" as fatal.
    if docker compose exec -T kanboard php -r '
$token = $argv[1];
$webhook_url = $argv[2];
$pdo = new PDO("sqlite:/var/www/app/data/db.sqlite");
$stmt = $pdo->prepare(
    "INSERT INTO settings (option, value) VALUES (?, ?) " .
    "ON CONFLICT(option) DO UPDATE SET value=excluded.value"
);
$stmt->execute(["webhook_url", $webhook_url]);
$stmt->execute(["webhook_token", $token]);
' -- "$(env_get KANBOARD_WEBHOOK_TOKEN)" "$kanboard_webhook_url"; then
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
log "Webhook configured: $kanboard_webhook_url"

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

log "Checking the Gitea access token can actually create repositories..."
gitea_token="$(env_get GITEA_TOKEN)"
token_valid="false"
if [ -n "$gitea_token" ]; then
    # A read check (GET /api/v1/user) is NOT enough: creating a repo needs
    # BOTH write:repository AND write:user scope (Gitea gates POST
    # /user/repos on the /user route group AND the /repos route). A token
    # with only read:user passes GET /user but 403s on repo creation — the
    # exact failure that left projects without repos. So probe the real
    # capability: create a throwaway private repo and delete it. A 201
    # (created) or 409 (already exists → auth+scope passed) means the token
    # can provision repos; 403/401 means it can't.
    probe_repo="marcus-setup-token-probe"
    probe_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H "Authorization: token ${gitea_token}" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"${probe_repo}\",\"private\":true,\"auto_init\":false}" \
        http://localhost:3000/api/v1/user/repos || echo 000)"
    if [ "$probe_code" = "201" ] || [ "$probe_code" = "409" ]; then
        token_valid="true"
        # Best-effort cleanup of the probe repo (needs the same scopes).
        curl -s -o /dev/null -X DELETE \
            -H "Authorization: token ${gitea_token}" \
            "http://localhost:3000/api/v1/repos/root/${probe_repo}" || true
    fi
fi

if [ "$token_valid" = "false" ]; then
    log "Generating a new Gitea access token (existing one missing or lacking repo-create scope)..."
    token_name="marcus-$(date +%s)"
    gitea_token="$(docker compose exec -T -u git gitea gitea admin user generate-access-token \
        --username root --token-name "$token_name" \
        --scopes write:repository,write:user --raw | tr -d '\r\n')"
    env_set GITEA_TOKEN "$gitea_token"
    log "Gitea token generated with write:repository,write:user scopes."
else
    log "Existing Gitea token can create repositories — reusing it."
fi

# ---------------------------------------------------------------------
# 7. Build and start Marcus now that .env has everything it needs —
#    OR, in native mode, hand off to scripts/run_marcus_native.sh instead
#    of building a container for it.
# ---------------------------------------------------------------------

# Set below when native mode should auto-launch Marcus as this script's
# very last action (after the summary prints) — see the bottom of this
# file. Deferred rather than launched here because run_marcus_native.sh
# exec's into a long-running server that never returns, which would skip
# the summary entirely if we jumped to it this early.
NATIVE_LAUNCH_PENDING="false"

if [ "$(env_get MARCUS_RUN_MODE)" = "native" ]; then
    log "Marcus run mode is 'native' — skipping the marcus container build."
    NATIVE_PID_FILE="$REPO_ROOT/.marcus_native.pid"
    if [ -f "$NATIVE_PID_FILE" ] && kill -0 "$(cat "$NATIVE_PID_FILE" 2>/dev/null)" 2>/dev/null; then
        log "A native Marcus process is already running (PID $(cat "$NATIVE_PID_FILE")) — leaving it running."
        log "Stop it first with ./scripts/teardown.sh if you want this run to (re)start it."
    else
        NATIVE_LAUNCH_PENDING="true"
        log "Kanboard, Gitea, and every token/webhook they need are provisioned. Marcus starts"
        log "automatically at the end of this script (./scripts/run_marcus_native.sh)."
    fi
else
    log "Building and starting Marcus..."
    # Start marcus (always) plus caddy (only when the TLS overlay is active —
    # MARCUS_PUBLIC_DOMAIN set adds -f docker-compose.tls.yml to COMPOSE_FILES).
    start_services=(marcus)
    if [ -n "$(env_get MARCUS_PUBLIC_DOMAIN)" ]; then
        start_services+=(caddy)
    fi
    if ! docker compose "${COMPOSE_FILES[@]}" --profile docker-marcus up -d --build --wait --wait-timeout 90 "${start_services[@]}"; then
        err "Marcus (or the TLS proxy) did not become healthy in time — most likely a KANBOARD_API_TOKEN mismatch, a missing ~/.claude.json / ~/.claude/.credentials.json for the claude_subscription provider, or (TLS) DNS/ports for MARCUS_PUBLIC_DOMAIN not yet reachable."
        docker compose "${COMPOSE_FILES[@]}" --profile docker-marcus logs "${start_services[@]}" --tail=50 || true
        exit 1
    fi
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

if [ "$(env_get KANBOARD_BIND_HOST)" = "0.0.0.0" ]; then
    echo " Kanboard:  http://localhost:8080   ($(env_get KANBOARD_ADMIN_USERNAME) / $(env_get KANBOARD_ADMIN_PASSWORD))"
else
    echo " Kanboard:  http://localhost:8080   (admin / admin)"
fi
echo " Gitea:     http://localhost:3000   (root / $(env_get GITEA_ADMIN_PASSWORD))"
if [ "$(env_get MARCUS_RUN_MODE)" = "native" ]; then
    if [ "$NATIVE_LAUNCH_PENDING" = "true" ]; then
        echo " Marcus:    starting now (this script exec's into ./scripts/run_marcus_native.sh below) — will listen on http://localhost:${host_port}/mcp"
    else
        echo " Marcus:    already running natively — http://localhost:${host_port}/mcp"
    fi
else
    echo " Marcus:    http://localhost:${host_port}/mcp"
fi
echo
echo " Marcus run mode: $(env_get MARCUS_RUN_MODE)"
echo " Kanboard project: $(env_get KANBOARD_PROJECT_NAME) (id $(env_get KANBOARD_PROJECT_ID))"
echo " Webhook:   configured — board changes reach Marcus instantly."
echo " AI provider: $(env_get MARCUS_AI_PROVIDER) (Marcus's own decomposition/analysis calls)"
if [ -n "$agent_token" ]; then
    echo " Agent auth: REQUIRED — connecting agents must pass the bearer token below."
else
    echo " Agent auth: none (localhost-only). Set MARCUS_AGENT_TOKEN before exposing remotely."
fi
echo
if [ "$NATIVE_LAUNCH_PENDING" = "true" ]; then
    echo " This terminal is about to become Marcus's own log output (./scripts/run_marcus_native.sh, in the"
    echo " foreground) — Ctrl-C stops it, or run ./scripts/teardown.sh from elsewhere."
    echo
    echo " Connect an AI agent from a DIFFERENT terminal/tab:"
else
    echo " Connect an AI agent from this machine:"
fi
echo "   claude mcp add --transport http marcus http://localhost:${host_port}/mcp${auth_flag}"
echo

bind_host="$(env_get MARCUS_BIND_HOST)"
tls_domain="$(env_get MARCUS_PUBLIC_DOMAIN)"
gitea_bind="$(env_get GITEA_BIND_HOST)"
gitea_bind="${gitea_bind:-127.0.0.1}"
if [ -n "$tls_domain" ]; then
    echo " Remote access (Marcus): ENABLED over HTTPS via built-in proxy (https://${tls_domain}/)."
    echo " Marcus stays on loopback behind the proxy; only 443 is exposed for it. From another machine:"
    echo "   claude mcp add --transport http marcus https://${tls_domain}/mcp${auth_flag}"
    echo " (A real cert requires DNS for ${tls_domain} → this host and ports 80+443 open. Give"
    echo "  Caddy a minute on first run to obtain the certificate.)"
else
    case "$bind_host" in
        127.0.0.1|localhost|"")
            echo " Remote access: DISABLED — Marcus, Gitea, and Kanboard only accept connections from this machine."
            echo " To allow other machines, re-run setup and answer yes (or set MARCUS_BIND_HOST=0.0.0.0,"
            echo " GITEA_BIND_HOST=0.0.0.0, KANBOARD_BIND_HOST=0.0.0.0, and MARCUS_AGENT_TOKEN in .env, then:"
            if [ "$(env_get MARCUS_RUN_MODE)" = "native" ]; then
                echo " docker compose up -d kanboard gitea && ./scripts/run_marcus_native.sh)."
            else
                echo " docker compose --profile docker-marcus up -d --build)."
            fi
            ;;
        *)
            conn_host="$bind_host"
            [ "$conn_host" = "0.0.0.0" ] && conn_host="<this-machine's-address>"
            echo " Remote access (Marcus): ENABLED over plain HTTP (bound to ${bind_host}) — the"
            echo " bearer token authenticates agents but is sent UNENCRYPTED; use a VPN/tunnel, or re-run for HTTPS."
            echo " From another machine:"
            echo "   claude mcp add --transport http marcus http://${conn_host}:${host_port}/mcp${auth_flag}"
            ;;
    esac
fi
if [ "$gitea_bind" = "0.0.0.0" ]; then
    echo " Remote access (Gitea): ENABLED (bound to ${gitea_bind}) — agents on other machines can git clone/push."
else
    echo " Remote access (Gitea): DISABLED — only reachable from this machine."
fi

kanboard_bind="$(env_get KANBOARD_BIND_HOST)"
kanboard_bind="${kanboard_bind:-127.0.0.1}"
if [ "$kanboard_bind" = "0.0.0.0" ]; then
    echo " Remote access (Kanboard): ENABLED (bound to ${kanboard_bind}) — humans on other machines can use its UI."
    echo " Its admin/admin login was replaced (see credentials above) since Kanboard's API can't rotate a"
    echo " password on the built-in account — that account is now disabled."
else
    echo " Remote access (Kanboard): DISABLED — only reachable from this machine. Agents never need this open"
    echo " (Marcus reaches Kanboard internally); this only matters if a human wants remote UI access."
fi
echo
echo " Save the Gitea/Kanboard credentials above if you plan to log in manually —"
echo " they won't be printed again (they're also in .env, which is git-ignored)."
if [ -n "$agent_token" ]; then
    echo " The agent token is stored in .env (git-ignored). Anyone with it can drive the board."
fi
echo "======================================================================"

# ---------------------------------------------------------------------
# 9. Native mode: hand off to run_marcus_native.sh as this script's very
#    last action, so `./scripts/setup.sh` alone is enough to end with
#    Marcus actually running — no separate command needed. Deliberately
#    last: everything above (summary, credentials, connect command) must
#    already be on screen, since run_marcus_native.sh exec's into a
#    long-running server that never returns control here.
# ---------------------------------------------------------------------

if [ "$NATIVE_LAUNCH_PENDING" = "true" ]; then
    echo
    exec "$SCRIPT_DIR/run_marcus_native.sh"
fi
