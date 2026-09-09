# Deployment Guide

Full setup, network, authentication, and multi-agent details for the MKG stack. See the [README](../README.md) for the essential Quick Start; this doc covers everything the README links out to.

---

## How the setup script works

`./scripts/setup.sh` does everything below in one command:

| Step | What happens | How |
|---|---|---|
| Marcus run mode | Asks once: run Marcus in Docker, or natively on this host? Defaults to Docker | See [Hybrid mode: Marcus outside Docker](#hybrid-mode-marcus-outside-docker) |
| Kanboard API token | Set to a known, generated value — no UI login needed | `API_AUTHENTICATION_TOKEN` env var on the `kanboard` container (Kanboard's own app-level auth mechanism) |
| Kanboard default columns | Kanboard's own global default for every FUTURE new project (created via its UI, its API, or Marcus's clone-project feature) is set to `Todo, Ready, In Progress, Blocked, Waiting for Human, Done` — not just the one project below | Kanboard has no API for this either — same direct `settings` table write as the webhook row below (`option='board_columns'`) |
| Kanboard project + columns | Created if missing; columns reconciled to `Todo, Ready, In Progress, Waiting for Human, Blocked, Done` | JSON-RPC calls (`createProject`, `getColumns`, `updateColumn`, `addColumn`) via `scripts/provision_kanboard.py` |
| Kanboard webhook | Set to `http://marcus:4298/webhooks/kanboard` (Docker mode) or `http://host.docker.internal:4298/webhooks/kanboard` (native mode) so board changes reach Marcus instantly instead of on the next 30s poll | Kanboard has no API for this — it's two rows (`webhook_url`, `webhook_token`) in its own SQLite `settings` table, written directly via `docker compose exec kanboard php -r '...'` (PDO SQLite, the same DB driver Kanboard itself uses) |
| Gitea admin account | Created non-interactively | `docker compose exec -u git gitea gitea admin user create ...` |
| Gitea access token | Generated non-interactively | `docker compose exec -u git gitea gitea admin user generate-access-token ...` |
| AI provider | `claude_subscription` if this machine has an authenticated `claude` CLI; `anthropic` if `CLAUDE_API_KEY` is already in `.env`; otherwise the script fails with instructions instead of prompting | See [AI provider](#ai-provider) |
| Network access | Asks once: allow AI agents on other machines to connect to Marcus, or localhost-only? Defaults to localhost-only if there's no terminal to ask | See [Network access](#network-access) |
| Marcus | Docker mode: built and started once everything above has produced the values it needs. Native mode: no container is built — the script `exec`'s into `run_marcus_native.sh` as its own last step instead, so Marcus ends up running either way with one command | `docker compose --profile docker-marcus up -d --build marcus`, or `exec ./scripts/run_marcus_native.sh` |

## Manual setup

If you'd rather do it by hand, or the script fails partway:

**Start Kanboard and Gitea:**
```bash
docker compose up -d kanboard gitea
docker compose logs -f gitea | grep "Listen"   # Gitea boots in seconds
```

**First-time Kanboard setup:**
1. Log in at http://localhost:8080 (`admin` / `admin`)
2. **Settings → API** — copy the API token
3. **Settings → Integrations → Webhook URL** — set to `http://marcus:4298/webhooks/kanboard`
4. Create a project and add columns: `Todo`, `Ready`, `In Progress`, `Waiting for Human`, `Blocked`, `Done`

**First-time Gitea setup** (`-u git`: the Gitea CLI refuses to run admin commands as root, and `docker compose exec` defaults to root):
```bash
docker compose exec -u git gitea gitea admin user create \
  --username root --password Marcus123! \
  --email root@example.com --admin --must-change-password=false
```
Then log in at http://localhost:3000 as `root` / `Marcus123!` → **Settings → Applications → Generate New Token** (scopes `write:repository`, `write:user`).

**Configure and start Marcus** — put the values you just collected into `.env` (see `.env.example`). You **must** set `MARCUS_AI_PROVIDER` explicitly on this manual path — `.env.example` ships it blank and Docker Compose defaults an unset value to `claude_subscription`, so if you meant to use an API key, set `MARCUS_AI_PROVIDER=anthropic` (and `CLAUDE_API_KEY=...`) — see [AI provider](#ai-provider).

If you use `MARCUS_AI_PROVIDER=claude_subscription`, first make sure both `~/.claude.json` and `~/.claude/.credentials.json` **exist as files** on this host:
```bash
mkdir -p ~/.claude && [ -f ~/.claude.json ] || echo '{}' > ~/.claude.json && [ -f ~/.claude/.credentials.json ] || echo '{}' > ~/.claude/.credentials.json
```
This matters because Docker does **not** fail when a bind-mount source is missing — it silently creates a **root-owned directory** at that path, which would break both the container's `claude` CLI and your host's own Claude Code. (`./scripts/setup.sh` does this step for you.) Then:
```bash
docker compose --profile docker-marcus up -d --build marcus
```
(The `marcus` service only starts when this profile is passed — see [Hybrid mode: Marcus outside Docker](#hybrid-mode-marcus-outside-docker) for why, and for the alternative of running Marcus natively instead.)

---

## Running multiple agents / multiple accounts

Marcus is already a parallel multi-agent coordinator — you don't wire anything special. **Each MCP session that calls `marcus_work` with no `agent_id` gets its own worker id auto-generated** (`worker-<hex>`), which it echoes back on later calls. So "N agents" just means **N MCP client sessions each running the orchestrate prompt** from the README's Quick Start. When one worker is handed a ticket, Marcus claims it under that worker's id, so the next worker's `marcus_work` call skips it and takes the next Ready ticket — two agents naturally land on different tickets, different branches, both `In Progress`.

**Two Claude Pro accounts on one machine.** Claude Code stores its login per config directory, so give each account its own (or use two machines / containers / OS users). In two terminals:

```bash
# Terminal 1 — account A
export CLAUDE_CONFIG_DIR=~/.claude-acctA
claude login                                  # log into Pro account A
claude mcp add --transport http marcus http://<HOST>:4298/mcp \
  -H "Authorization: Bearer <MARCUS_AGENT_TOKEN>"   # drop -H on a no-token localhost setup
claude                                         # then paste the orchestrate prompt

# Terminal 2 — account B (identical, different config dir + account)
export CLAUDE_CONFIG_DIR=~/.claude-acctB
claude login                                  # log into Pro account B
claude mcp add --transport http marcus http://<HOST>:4298/mcp \
  -H "Authorization: Bearer <MARCUS_AGENT_TOKEN>"
claude
```

Give **both** sessions the same orchestrate prompt from the README's Quick Start (or the fuller version in `prompts/Kanboard_Agent_Prompt.md` §0).

**Creating actual parallel work.** Concurrency is bounded by how many workable tickets exist. Either:
- put **2+ tickets in `Ready`, each assigned to a human** (assigned-to-anyone + Ready is the trigger) — one agent per ticket; or
- create **one big ticket (4+ acceptance criteria)** — Marcus auto-decomposes it into sub-tickets (each Ready) that the agents pick up independently (or force it with a `@marcus decompose` comment).

Dependencies are respected: a ticket that `depends_on` another is held (Blocked) until its dependency merges, so agents never build on unfinished work. `MARCUS_MAX_PARALLEL_AGENTS` (default `3`) caps Marcus's internal auto-start slot pool — two agents are well under it, so no change is needed.

**Who pays for what.** Each account's *coding* rides its own subscription — that's the parallelism. Marcus's *own* orchestration calls (decomposition, acceptance-criteria generation, report summaries) are a **separate** budget: whatever Marcus itself is configured with (its own `claude` CLI login or an API key — see [AI provider](#ai-provider)). Effectively three LLM identities: A codes, B codes, Marcus coordinates.

---

## Scoping Marcus to specific projects

A single Marcus install can see **every** Kanboard project on the board. Left unchecked, that means a brand-new project you create just to sketch something out would immediately get its own auto-created Gitea repo, its columns reconciled to Marcus's layout, and any ticket in it picked up by an AI agent — whether or not you meant for Marcus to touch it.

**Every project starts disabled** — including the one `scripts/setup.sh` creates. Marcus does nothing on a project — no repo, no columns, no claimed tickets, no agent commits — until a human explicitly opts it in. Open that project's board in Kanboard and click the **"🔒 Marcus: OFF for this project"** button in the header (it's the first control, to the left of the active-agents badge); it flips to **"🔓 Marcus: ON for this project"** and Marcus provisions the repo + columns immediately (no waiting on the backstop poll).

A few things worth knowing:
- **Marcus SEES every project, but only ACTS on enabled ones.** It reads all boards so it can tell you "this project has ready tickets but isn't enabled" and so a deleted ticket is noticed anywhere — but every write (claiming, commenting, moving a card, merging) is gated on the toggle. Disabling a project never hides or deletes its tickets from Marcus's view; it just stops Marcus touching them.
- **Every `marcus_work` poll re-reads the boards.** A ticket you have just assigned and moved to Ready is handed to a polling agent on its next poll, rather than waiting for the background `BoardWatcher` tick (30 s by default) — which also means this works with webhooks disabled. Near-simultaneous polls from several agents share one board read.
- **Deleted tickets stop being tracked.** Kanboard fires no event when a task is deleted (`TaskModel::remove()` dispatches nothing, and there is no `EVENT_REMOVE` constant), so the bundled **MarcusDevEnv plugin** supplies one: it overrides Kanboard's task model to POST a `task.remove` webhook to Marcus. Marcus then drops the ticket, releases any claim on it and stops its preview container. A board read catches it too, so deletions are still noticed if the plugin isn't installed — just not instantly. On startup Marcus re-checks every tracked ticket, which is what catches tickets deleted while it was stopped.
- **It is per project, and project ids are not board names.** Enabling one project never covers another. When Marcus withholds tickets it names the project by id *and* name so you can find the right board.
- **You can see every project's ON/OFF state at once from Kanboard's own `/dashboard` page**, without opening each board — see [Dashboard page](features.md#dashboard-page).
- **This is a separate control from the Human/AI Gate toggle.** The access toggle decides *whether* Marcus may touch a project at all; the Gate toggle (next to it) decides *how* it works once it's already allowed to (pause for your review vs. work autonomously to done).
- **Disabling a project is not a kill switch for work already in flight.** It blocks Marcus from claiming any *new* ticket in that project from that point on; an agent partway through an already-claimed ticket is left to finish rather than being force-interrupted mid-commit.
- **This upgrade is a breaking change on purpose.** If you're updating an existing deployment, every project you were already using goes to disabled the moment you redeploy — including your "main" project. Re-enable it from its board header before expecting Marcus to keep working there.
- **The ON/OFF state survives a teardown.** It lives in `data/project_access_settings.json`, and `scripts/teardown.sh` deletes nothing — so `teardown.sh` followed by `setup.sh` comes back up with exactly the projects you had enabled. Setup never changes it either way.
- Toggle it from a script instead of the UI with `GET`/`PUT /api/project-enabled?project_id=<id>` (see [HTTP endpoints](api-reference.md#http-endpoints)).

---

## Network access

`./scripts/setup.sh` asks once, interactively: **"Allow OTHER machines to reach this stack?"** One answer configures all three services — written to `.env` as `MARCUS_BIND_HOST` / `GITEA_BIND_HOST` / `KANBOARD_BIND_HOST` (separate variables, not one shared value, since each service is exposed for a different reason — see below):

| Answer | Effect |
|---|---|
| No (default) | Marcus, Gitea, and Kanboard only accept connections from this machine. This is the default for a reason: it's the safer choice, and what most local/single-machine setups want. No agent token is needed, and Kanboard's login stays `admin`/`admin` (fine — it's not reachable from anywhere else). |
| Yes | All three become reachable from other machines. Setup also **generates an agent token, offers HTTPS for Marcus, and replaces Kanboard's `admin`/`admin` login** before ever publishing its port — see below and [Authenticating remote agents](#authenticating-remote-agents). |

Answering **Yes** is what a distributed setup needs — Marcus, Kanboard, and Gitea can each run on separate hosts (see [Independent deployment](#independent-deployment)): AI agents connect to Marcus's MCP endpoint and clone/push to Gitea, while humans use Kanboard's UI, all over the network.

If there's no terminal to ask (e.g. running the script from CI), it defaults to **No** rather than guessing. To change your answer later, edit the three `*_BIND_HOST` variables in `.env` and run `docker compose up -d` again.

**Why Kanboard needs special handling.** AI agents never talk to Kanboard directly — they go through Marcus, which reaches Kanboard over the internal Docker network (see [How AI agents reach tickets](architecture.md#how-ai-agents-reach-tickets)). Kanboard's port only matters for a *human* browsing its UI remotely. Unlike `KANBOARD_API_TOKEN`/`MARCUS_AGENT_TOKEN`/`GITEA_ADMIN_PASSWORD` (all randomly generated), Kanboard's JSON-RPC API has **no method to rotate an existing user's password** — so simply publishing its port with the fixed `admin`/`admin` default would hand anyone who finds it full read/write access to every ticket. Instead, when you answer Yes, setup:
1. Generates `KANBOARD_ADMIN_USERNAME` (`marcus_admin`) / `KANBOARD_ADMIN_PASSWORD` (random) in `.env`.
2. Creates that account via Kanboard's JSON-RPC API and **disables the built-in `admin` account** (`ensure_admin_user()` in `scripts/provision_kanboard.py`) — this doesn't affect Marcus's own Kanboard access, which authenticates as a separate app-level API user, not as `admin`.
3. Only then publishes Kanboard's port.

The new credentials are printed at the end of setup (and saved in `.env`) — log in with those, not `admin`/`admin`.

---

## Authenticating remote agents

When you allow remote access, Marcus must not be usable by *unaccounted* ("rogue") AI agents — reaching the MCP endpoint means being able to pull tasks and read/write ticket branches and code. Two mechanisms handle this, both set up automatically when you answer **Yes** to the network prompt:

**1. A bearer token (who is allowed to connect).** Setup generates `MARCUS_AGENT_TOKEN` (a 32-byte random secret, stored in `.env`). Whenever it's set, Marcus requires **every** request — the MCP control plane *and* the gate/description/dev-env API routes — to carry `Authorization: Bearer <token>`, and returns `401` otherwise (`src/core/agent_auth.py`). An agent connects with:

```bash
claude mcp add --transport http marcus http://<this-machine's-address>:4298/mcp \
  -H "Authorization: Bearer <MARCUS_AGENT_TOKEN>"
```

The exact command (with your real token filled in) is printed at the end of setup. Give the token only to the agents you want to admit; anyone with it can drive the board, so treat it like a password. The Kanboard webhook route is exempt — it authenticates with its own separate `?token=` secret that Kanboard sends. With no token set (the localhost-only default), auth is off, keeping local use frictionless.

**2. HTTPS (protecting the token in transit).** A bearer token sent over plain HTTP can be sniffed on the network, so setup offers to terminate TLS with a built-in [Caddy](https://caddyserver.com/) reverse proxy (`docker-compose.tls.yml`), **for Marcus only**. Enter a **public domain** when asked and Caddy automatically obtains and renews a real, browser-trusted **Let's Encrypt** certificate (requires the domain's DNS to point at this host and ports 80+443 open to the internet). In this mode only Caddy's `443` is exposed for Marcus, which stays on loopback behind it and is reached only through the proxy — agents connect over `https://<domain>/mcp`. Gitea and Kanboard are **not** proxied by Caddy and keep their own directly-published ports (plain HTTP) regardless of this choice, since Caddy in this setup fronts Marcus specifically.

If you don't provide a domain, setup leaves the stack on plain HTTP and tells you so — the token still authenticates agents, but **use a VPN or tunnel (Tailscale, WireGuard, Cloudflare Tunnel) to encrypt the connection**. (A self-signed cert without a domain isn't offered as a real option, because `claude mcp add` would reject the untrusted certificate.)

> ⚠️ **Still firewall it.** Gitea's admin password and Kanboard's replacement login are both randomly generated by setup (printed once, saved in `.env`) — but they're still real credentials sitting on an internet-reachable port once you answer Yes. Requiring the bearer token closes the earlier CSRF gap (a browser can't attach the `Authorization` header cross-origin), but defense-in-depth still means restricting the stack to just the hosts your agents/users actually need, with a firewall/security-group, especially on a cloud VPS.

> ℹ️ **Known limitation — the browser dashboard under a token.** The token gates *every* Marcus HTTP route (that's the point: a rogue agent can't read or change board state). But the MarcusDevEnv Kanboard-plugin widgets (Active Agents badge, gate toggle, project-description link) are fetched by your *browser*, which can't attach an `Authorization: Bearer` header — so with `MARCUS_AGENT_TOKEN` set, those widgets show errors and the dashboard degrades. Agent connectivity (the MCP endpoint) is unaffected. If you need the browser dashboard to work over an authenticated remote Marcus, the plugin needs to forward the token — not wired up yet; open an issue / ask if you want it.

---

## AI provider

Marcus's own decomposition, dependency-inference, and effort-estimation calls need an AI provider — separate from whatever auth the coding agents you connect via MCP use for their own work.

`./scripts/setup.sh` never prompts for an API key. It picks a provider automatically, in this order:

1. **`.env` already has `CLAUDE_API_KEY`** → uses the `anthropic` provider (pay-per-token, your existing choice respected).
2. **Otherwise, this machine has an authenticated `claude` CLI** (you've run `claude login` here — the same login Claude Code itself uses) → uses the `claude_subscription` provider. The script bind-mounts your `~/.claude.json` and `~/.claude/.credentials.json` into the `marcus` container (see `docker-compose.yml`), so `claude` CLI calls made *inside* the container ride the same Claude Pro/Max subscription, with no separate API key. Marcus's `Dockerfile` installs the `claude` CLI itself (Node.js + `npm install -g @anthropic-ai/claude-code`) for this.
3. **Neither is available** → the script fails with instructions (`claude login`, or set `CLAUDE_API_KEY` yourself) instead of prompting interactively.

You can also set `MARCUS_AI_PROVIDER` in `.env` yourself to override this — an explicit value always wins over the auto-detection above — see `.env.example`.

> ⚠️ **macOS hosts:** on macOS the `claude` CLI stores its login token in the **login Keychain**, not in `~/.claude/.credentials.json`. That file can't be shared into a Linux container, so `claude_subscription` will **not** authenticate inside Docker on a Mac host — every AI call fails. `setup.sh` detects macOS and warns you (only in Docker mode — see below). Two ways to actually fix this on a Mac, instead of just working around it with an API key:
> 1. **Run Marcus natively** (recommended) — see [Hybrid mode: Marcus outside Docker](#hybrid-mode-marcus-outside-docker). A native macOS process reads the Keychain directly, the same way your interactive `claude login` session does, so this isn't a workaround — it's the actual fix.
> 2. **Use the API-key path** instead: set `CLAUDE_API_KEY` in `.env` before running setup. (Linux hosts, where the token lives in the credentials file, are unaffected by any of this.)

**Trade-offs of `claude_subscription`:**
- Each call spawns a full `claude` CLI process inside the container (several seconds to tens of seconds, versus sub-second for a direct API call), and shares your subscription's usage limits with any interactive Claude Code sessions on the same account.
- The container mounts your **live** `~/.claude.json` / `~/.claude/.credentials.json` read-write and acts as that login. Running interactive Claude Code on the host *at the same time* as Marcus means both share one login — an OAuth token refresh on either side can momentarily invalidate the other, so you may occasionally have to re-run `claude login`. Fine for the local/demo use this stack targets; think twice on a shared host.
- If you'd rather not share host credentials at all, set `CLAUDE_API_KEY` in `.env` before running `./scripts/setup.sh` to use the `anthropic` provider instead.

---

## Hybrid mode: Marcus outside Docker

Kanboard and Gitea always run in Docker (`docker-compose.yml`), but Marcus itself doesn't have to. `./scripts/setup.sh` asks once, up front: run Marcus **in Docker** (default) or **natively on this host**?

**Why you'd choose native.** The whole reason this exists is the macOS Keychain problem described above: Docker Desktop on a Mac runs Linux in a VM, so the `claude` CLI process Marcus spawns inside a container is a Linux process with no access to the macOS Keychain, no matter what files you bind-mount into it. A **native** Marcus process, running directly on macOS, is a genuine macOS process — it reads the Keychain exactly the way your interactive `claude login` session does. No credential extraction, no staleness, no workaround. (Everything else about hybrid mode — reaching Kanboard/Gitea, dev-environment previews — works identically to Docker mode; this is the one thing it actually *fixes*, not just a different way to run the same thing.)

**What "hybrid" means concretely:**
- Kanboard and Gitea keep running exactly as before: `docker compose up -d kanboard gitea`.
- Marcus runs as a normal process on your host: `./scripts/run_marcus_native.sh`.
- They talk to each other over `localhost` ports instead of Docker's internal service names — Marcus reaches Kanboard at `http://localhost:8080/jsonrpc.php` and Gitea at `http://localhost:3000` (the same host-published ports a human's browser already uses), and Kanboard/Gitea reach back OUT to Marcus at `http://host.docker.internal:4298/...` for their webhooks (the standard Docker mechanism for a container to reach a process on its host).

**Setup — one command, same as Docker mode:**
```bash
./scripts/setup.sh
# → "How should Marcus run?" → choose 2 (native)
```
This provisions Kanboard, Gitea, and every token/webhook exactly like Docker mode, then **automatically starts Marcus itself** as the script's last step (it `exec`'s into `./scripts/run_marcus_native.sh` right after printing the summary) — no separate command to run afterward. That terminal becomes Marcus's own log output; run the printed `claude mcp add` command from a different terminal/tab, and stop Marcus with Ctrl-C or `./scripts/teardown.sh`.

Requires Python 3.11+ and Marcus's dependencies installed on the host (`pip install -r requirements.txt && pip install --no-deps -e .`) *before* running setup — `run_marcus_native.sh` checks for this and exits with the exact commands if they're missing (setup.sh's own provisioning of Kanboard/Gitea still completes either way; only the final Marcus launch fails). If you're using `claude_subscription`, it also checks that `claude login` is active on this host before starting.

To start Marcus again later without re-provisioning anything (e.g. after a reboot), run `./scripts/run_marcus_native.sh` directly — re-running the full `./scripts/setup.sh` also works and is safe (it detects an already-running native Marcus and leaves it alone rather than trying to start a second one on the same port).

**What's different from Docker mode:**
- Marcus's own state (`~/.marcus/costs.db`, ticket lifecycle, etc.) lives under the repo's `./data/` directory either way (both modes resolve these as paths relative to Marcus's own working directory, which `run_marcus_native.sh` sets to the repo root) — so switching modes doesn't lose anything, but the two modes don't share `~/.marcus/costs.db` outside that (Docker's copy is bind-mounted from `./data/.marcus`; native mode's is wherever `~/.marcus` really is on your host — usually the same place, but worth knowing if they ever diverge).
- The dev-environment preview containers (`docker-compose.yml`'s Docker-outside-of-Docker setup) get *simpler* in native mode: a native Marcus talks to your host's Docker daemon directly, so there's no container-to-host path translation to worry about.
- The built-in HTTPS proxy ([Authenticating remote agents](#authenticating-remote-agents)'s Caddy option) isn't available in native mode — it only fronts the Marcus *container*. `setup.sh` skips that question when you choose native; put your own reverse proxy in front of the native Marcus process if you need TLS, or keep plain HTTP behind a VPN/tunnel.
- Everything else — the bearer token, `MARCUS_BIND_HOST`, remote access, the Kanboard plugin, AI Verify, hot-reload dev environments — works exactly the same regardless of which mode Marcus runs in.

**Switching modes later:** edit `MARCUS_RUN_MODE` in `.env` (`docker` or `native`) and re-run `./scripts/setup.sh` to pick up the change (it re-seeds the Kanboard webhook URL for the new mode). If you'd previously enabled the HTTPS proxy under Docker mode, clear `MARCUS_PUBLIC_DOMAIN` from `.env` too before switching to native.

---

## Independent deployment

Each service deploys independently:

| Service | Compose file | Suggested platform |
|---|---|---|
| Local all-in-one (Kanboard + Gitea + Marcus) | `docker-compose.yml` (root), via `./scripts/setup.sh` | macOS / Linux laptop |
| Kanboard only | `kanboard/docker-compose.yml` | Railway, Fly.io, any VPS |
| Gitea only | `gitea/docker-compose.yml` | Any small VPS (≥ 512 MB RAM) |
| Marcus only | `Dockerfile` (root), or `pip install -e .` + `python -m marcus --http` locally | A cloud VM, or CI, pointed at remote Kanboard/Gitea instances |
| Marcus + HTTPS proxy | `docker-compose.yml` + `docker-compose.tls.yml` overlay (Caddy) | A cloud VPS with a public domain, for remote agents over TLS |

When Marcus runs apart from the agents that connect to it, set `MARCUS_AGENT_TOKEN` so only authorized agents can reach it, and prefer the HTTPS overlay (or a VPN/tunnel) so the token isn't sent in cleartext — see [Authenticating remote agents](#authenticating-remote-agents).

**Railway (Kanboard):** push to GitHub, create a Railway service pointing at `kanboard/`, set environment variables in the Railway dashboard. Railway reads `kanboard/railway.toml` automatically.
