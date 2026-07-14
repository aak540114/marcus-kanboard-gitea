# Marcus × Kanboard

A production deployment of **[Marcus](https://github.com/lwgray/marcus)** — the board-mediated AI multi-agent orchestrator — wired to **Kanboard** for ticket management, **Gitea** for git repositories, and a custom Kanboard plugin that gives every board a live AI control panel.

> **What Marcus is:** see the [Marcus README](https://github.com/lwgray/marcus) and [docs](https://marcus-ai.dev). This repo is an opinionated deployment of it, not a fork.

---

## What this repo adds

| Feature | Description |
|---|---|
| **Kanboard provider** | Full Kanboard JSON-RPC integration — tickets, columns, comments, assignments |
| **Gitea integration** | `GiteaManager` + `ProjectSyncWorkflow` (`src/integrations/gitea_manager.py`, `src/workflows/project_sync_workflow.py`) auto-create a Gitea repo the first time an agent calls `get_work_context` for a ticket whose project doesn't have one yet — no manual repo setup. |
| **MarcusDevEnv plugin** | Kanboard plugin that adds AI-aware UI to every board and task |
| **Hot-reload dev environments** | One-click per-ticket preview URL; supports any language/framework via project description. Refreshes **instantly** on every `git push` to the ticket branch via a Gitea webhook (`/webhooks/gitea`) — no polling, no manual webhook setup. A global "Max dev environments" setting caps how many preview containers can run at once. |
| **Project Description system** | Per-project markdown doc (tech stack, architecture notes) that AI agents read via the `get_project_description` MCP tool; editable from the board |
| **Enriched ticket context** | `get_work_context` — the first call every agent makes — also returns priority, labels, due date, estimated hours, dependency links (`depends_on`/`blocks`/`relates_to`), and the ticket's last 10 comments, so a human's reply to a paused ticket is actually visible to the agent |
| **Human Gate / AI Gate toggle** | Per-project and per-ticket control over whether humans review AI work before it merges |
| **AI Verify** | Configurable N-round LLM code review before any AI-gate merge; each round posts a comment with findings; agent fixes issues between rounds; 0 rounds = disabled |
| **Claude subscription provider** | Marcus's own planner calls (decomposition, dependency inference, effort estimation) can run through a locally logged-in `claude` CLI instead of a metered API key — see [AI provider](#ai-provider). No `CLAUDE_API_KEY` prompt during setup. |
| **Remote agents + auth** | Opt in during setup to let AI agents on other machines connect; access is gated by a bearer token so unaccounted agents are rejected, with optional built-in HTTPS — see [Authenticating remote agents](#authenticating-remote-agents). |
| **Remote Kanboard access for humans** | The same setup opt-in also makes Kanboard's UI reachable remotely — its `admin`/`admin` login is replaced with a generated account first, since Kanboard's API can't rotate an existing password — see [Network access](#network-access). |

---

## Built on

| Tool | Role |
|---|---|
| [Marcus](https://github.com/lwgray/marcus) | AI multi-agent orchestrator (MCP server, board watcher, ticket lifecycle, agent coordination) |
| [Kanboard](https://kanboard.org) | Self-hosted kanban board — the shared task board all agents coordinate through |
| [Gitea](https://about.gitea.com) | Self-hosted git — one repo per project, one branch per ticket. A single lightweight Go binary, chosen over GitLab CE for its low resource footprint |
| Python 3.11+ | Marcus server runtime |
| Docker / Docker Compose | Runs Kanboard and Gitea; dev containers for hot-reload previews |
| [Caddy](https://caddyserver.com) | Optional TLS reverse proxy (`docker-compose.tls.yml`) — auto HTTPS for remote agents via Let's Encrypt |
| [MCP](https://modelcontextprotocol.io) | Protocol agents use to talk to Marcus (Claude Code, Codex, Gemini CLI, etc.) |

---

## MarcusDevEnv Kanboard Plugin

The plugin ships in `kanboard/plugins/MarcusDevEnv/` and is automatically active in all supported deployment paths. It adds these panels to every board and task:

### Board header
| Widget | What it does |
|---|---|
| **Active AI Agents badge** | Live green/grey/amber badge showing how many tickets are currently held by an AI agent. Updates every 15 s; hover to see ticket IDs. |
| **Project Description button** | Opens the Marcus-served project description page for this project — the AI agents' shared source of truth for language, framework, and architecture. |
| **Human Gate / AI Gate toggle** | Sets the project-level gate mode. Human Gate (default): AI pauses for human review before done. AI Gate: AI merges and closes autonomously. |
| **AI Verify counter** | Appears when AI Gate is active. `[−] N [+]` sets how many sequential LLM review rounds run before the branch auto-merges. 0 = disabled. |
| **Max dev environments counter** | Global, always visible. `[−] N [+]` caps how many "Open Dev Environment" Docker containers can run at once across every ticket — `∞` (default) means unlimited. Once the limit is reached, starting a new one fails until an existing one is stopped. |

### Task sidebar
| Panel | What it does |
|---|---|
| **Marcus Dev Environment** | Start / Open / Stop a hot-reload preview for this ticket's branch. Any language — stack comes from the project description. |
| **Marcus Gate Mode** | Per-ticket gate override. Shows the project default; lets you switch this ticket to Human or AI gate independently. Ticket setting overrides project setting. Includes a per-ticket AI Verify override when AI Gate is active. |
| **Marcus Dependencies** | Dependency graph: *Depends on*, *Blocks*, *Related* — each with a colour-coded column-status badge. |

---

## Architecture

All three services run as containers on one `docker compose` network and reach each other by service name (`kanboard`, `gitea`, `marcus`) — only the host-side port mappings (8080, 3000, 4298) matter from outside Docker.

```
Human (browser)
  │  creates project, ticket           │  assigns, sets "Ready"
  ▼                                    ▼
kanboard (container, host port 8080) ← Kanboard JSON-RPC API (internal port 80)
  │  ProjectWatcher polls               │  BoardWatcher polls (30s) + webhook (instant)
  ▼  getAllProjects()                   ▼  getAllTasks()
marcus (container, host port 4298) ─── marcus (container)
  │  GiteaManager*                      │  BranchManager + HumanGatedWorkflow
  ▼  POST /api/v1/user/repos            ▼  git push branch
gitea (container, host port 3000) ──── gitea — branch per ticket

AI agents (Claude Code, Codex, etc.)
  └── connect to http://localhost:4298/mcp  (MCP protocol)
      │   (remote agents: + Authorization: Bearer <MARCUS_AGENT_TOKEN>)
      ├── request_next_task
      ├── signal_ready_for_review    → Human Gate: "Waiting for Human"
      │                              → AI Gate:    auto-merge + "Done"
      ├── signal_waiting_for_human   → Human Gate: pause for input
      │                              → AI Gate:    post note, continue
      └── post_ticket_progress
```

\* Not wired into the running server yet — see the Gitea integration row above.

### How AI agents reach tickets

AI agents never call Kanboard's JSON-RPC API and never receive Kanboard's API token. They call Marcus's MCP tools (`get_work_context`, `get_project_description`, `post_ticket_progress`, `signal_ready_for_review`, …); Marcus alone holds `KANBOARD_API_TOKEN` and is the only thing that makes JSON-RPC calls to Kanboard, over the internal Docker network (`http://kanboard/jsonrpc.php`, not the host-published `:8080`). No tool response ever contains a Kanboard URL or credential. This is why gating Marcus's HTTP endpoint with a bearer token (see [Authenticating remote agents](#authenticating-remote-agents)) is sufficient to control ticket access: it's the *only* door.

`get_work_context` — the first call every agent makes — returns everything Marcus knows about a ticket: title, description, acceptance criteria, branch/repo info, priority, labels, due date, estimated hours, dependency links (`depends_on`/`blocks`/`relates_to`), and its last 10 comments (see `prompts/Kanboard_Agent_Prompt.md` for the full field reference). `get_project_description` returns the project-wide tech stack and architecture notes when per-ticket context isn't enough.

Agents do talk to **Gitea** directly, but only for `git clone`/`fetch`/`push` on the one branch Marcus created for them — a different, narrower surface than the board itself.

---

## Quick Start

### Prerequisites

- Docker Desktop (macOS/Linux) — **2 GB RAM** is plenty (Gitea is lightweight; no GitLab-sized allocation needed)
- `curl`, `python3`, `openssl` (all preinstalled on macOS/most Linux distros)
- Either a **Claude Pro/Max subscription** (run `claude login` on this machine once, beforehand — the setup script picks it up automatically, no API key) **or** a Claude API key from [console.anthropic.com](https://console.anthropic.com/) if you'd rather pay per token. See [AI provider](#ai-provider) below.
- An MCP-compatible AI agent (Claude Code, Codex, etc.)

### 1. Run the setup script

```bash
./scripts/setup.sh
```

This one command does everything the individually-numbered steps below used to require by hand: starts Kanboard and Gitea, creates the Kanboard project and its six required columns, sets the Kanboard API token and webhook, creates the Gitea admin account and access token, picks and wires up an AI provider for Marcus's own decomposition/analysis calls (see [AI provider](#ai-provider) — no API key prompt), then builds and starts Marcus itself.

It's safe to re-run — every step checks live state before creating or changing anything, so running it again after `docker compose down` is a fast no-op, and running it after `docker compose down -v` (which wipes volumes) re-provisions everything from scratch.

When it finishes it prints the Kanboard/Gitea/Marcus URLs, the Gitea admin password, which AI provider got selected, and the exact `claude mcp add` command for step 2 below — both for connecting from this machine and from a remote one.

<details>
<summary><strong>How the setup script works</strong> (click to expand)</summary>

| Step | What happens | How |
|---|---|---|
| Kanboard API token | Set to a known, generated value — no UI login needed | `API_AUTHENTICATION_TOKEN` env var on the `kanboard` container (Kanboard's own app-level auth mechanism) |
| Kanboard project + columns | Created if missing; columns reconciled to `Todo, Ready, In Progress, Waiting for Human, Blocked, Done` | JSON-RPC calls (`createProject`, `getColumns`, `updateColumn`, `addColumn`) via `scripts/provision_kanboard.py` |
| Kanboard webhook | Set to `http://marcus:4298/webhooks/kanboard` so board changes reach Marcus instantly instead of on the next 30s poll | Kanboard has no API for this — it's two rows (`webhook_url`, `webhook_token`) in its own SQLite `settings` table, written directly via `docker compose exec kanboard php -r '...'` (PDO SQLite, the same DB driver Kanboard itself uses) |
| Gitea admin account | Created non-interactively | `docker compose exec -u git gitea gitea admin user create ...` |
| Gitea access token | Generated non-interactively | `docker compose exec -u git gitea gitea admin user generate-access-token ...` |
| AI provider | `claude_subscription` if this machine has an authenticated `claude` CLI; `anthropic` if `CLAUDE_API_KEY` is already in `.env`; otherwise the script fails with instructions instead of prompting | See [AI provider](#ai-provider) |
| Network access | Asks once: allow AI agents on other machines to connect to Marcus, or localhost-only? Defaults to localhost-only if there's no terminal to ask | See [Network access](#network-access) |
| Marcus | Built and started once everything above has produced the values it needs | `docker compose up -d --build marcus` |

</details>

<details>
<summary><strong>Manual setup</strong> (if you'd rather do it by hand, or the script fails partway)</summary>

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
Then log in at http://localhost:3000 as `root` / `Marcus123!` → **Settings → Applications → Generate New Token** (scopes `write:repository`, `read:user`).

**Configure and start Marcus** — put the values you just collected into `.env` (see `.env.example`). You **must** set `MARCUS_AI_PROVIDER` explicitly on this manual path — `.env.example` ships it blank and Docker Compose defaults an unset value to `claude_subscription`, so if you meant to use an API key, set `MARCUS_AI_PROVIDER=anthropic` (and `CLAUDE_API_KEY=...`) — see [AI provider](#ai-provider).

If you use `MARCUS_AI_PROVIDER=claude_subscription`, first make sure both `~/.claude.json` and `~/.claude/.credentials.json` **exist as files** on this host:
```bash
mkdir -p ~/.claude && [ -f ~/.claude.json ] || echo '{}' > ~/.claude.json && [ -f ~/.claude/.credentials.json ] || echo '{}' > ~/.claude/.credentials.json
```
This matters because Docker does **not** fail when a bind-mount source is missing — it silently creates a **root-owned directory** at that path, which would break both the container's `claude` CLI and your host's own Claude Code. (`./scripts/setup.sh` does this step for you.) Then:
```bash
docker compose up -d --build marcus
```

</details>

### 2. Connect your AI agent

Point any MCP-compatible agent at `http://localhost:4298/mcp`. For Claude Code:

```bash
claude mcp add --transport http marcus http://localhost:4298/mcp
```

This always works from the same machine Marcus runs on. Connecting from a **different machine** (another laptop, a remote VPS) additionally requires you to have opted in during setup — see [Network access](#network-access).

---

## Network access

`./scripts/setup.sh` asks once, interactively: **"Allow OTHER machines to reach this stack?"** One answer configures all three services — written to `.env` as `MARCUS_BIND_HOST` / `GITEA_BIND_HOST` / `KANBOARD_BIND_HOST` (separate variables, not one shared value, since each service is exposed for a different reason — see below):

| Answer | Effect |
|---|---|
| No (default) | Marcus, Gitea, and Kanboard only accept connections from this machine. This is the default for a reason: it's the safer choice, and what most local/single-machine setups want. No agent token is needed, and Kanboard's login stays `admin`/`admin` (fine — it's not reachable from anywhere else). |
| Yes | All three become reachable from other machines. Setup also **generates an agent token, offers HTTPS for Marcus, and replaces Kanboard's `admin`/`admin` login** before ever publishing its port — see below and [Authenticating remote agents](#authenticating-remote-agents). |

Answering **Yes** is what a distributed setup needs — Marcus, Kanboard, and Gitea can each run on separate hosts (see [Independent deployment](#independent-deployment)): AI agents connect to Marcus's MCP endpoint and clone/push to Gitea, while humans use Kanboard's UI, all over the network.

If there's no terminal to ask (e.g. running the script from CI), it defaults to **No** rather than guessing. To change your answer later, edit the three `*_BIND_HOST` variables in `.env` and run `docker compose up -d` again.

**Why Kanboard needs special handling.** AI agents never talk to Kanboard directly — they go through Marcus, which reaches Kanboard over the internal Docker network (see [How AI agents reach tickets](#how-ai-agents-reach-tickets)). Kanboard's port only matters for a *human* browsing its UI remotely. Unlike `KANBOARD_API_TOKEN`/`MARCUS_AGENT_TOKEN`/`GITEA_ADMIN_PASSWORD` (all randomly generated), Kanboard's JSON-RPC API has **no method to rotate an existing user's password** — so simply publishing its port with the fixed `admin`/`admin` default would hand anyone who finds it full read/write access to every ticket. Instead, when you answer Yes, setup:
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

> ⚠️ **macOS hosts:** on macOS the `claude` CLI stores its login token in the **login Keychain**, not in `~/.claude/.credentials.json`. That file can't be shared into a Linux container, so `claude_subscription` will **not** authenticate inside Docker on a Mac host — every AI call fails. `setup.sh` detects macOS and warns you. On a Mac, use the API-key path instead: set `CLAUDE_API_KEY` in `.env` before running setup. (Linux hosts, where the token lives in the credentials file, are unaffected.)

**Trade-offs of `claude_subscription`:**
- Each call spawns a full `claude` CLI process inside the container (several seconds to tens of seconds, versus sub-second for a direct API call), and shares your subscription's usage limits with any interactive Claude Code sessions on the same account.
- The container mounts your **live** `~/.claude.json` / `~/.claude/.credentials.json` read-write and acts as that login. Running interactive Claude Code on the host *at the same time* as Marcus means both share one login — an OAuth token refresh on either side can momentarily invalidate the other, so you may occasionally have to re-run `claude login`. Fine for the local/demo use this stack targets; think twice on a shared host.
- If you'd rather not share host credentials at all, set `CLAUDE_API_KEY` in `.env` before running `./scripts/setup.sh` to use the `anthropic` provider instead.

---

## Full ticket lifecycle

```
Human creates ticket in Kanboard
  → Marcus generates acceptance criteria (AI)

Human assigns ticket + moves to "Ready"
  → Marcus checks project description for tech stack
  → If stack missing: posts clarification comment, moves to "Waiting for Human"
  → If stack OK: creates branch in Gitea, moves to "In Progress"

AI agent works on the branch
  → Posts progress comments at 25 / 50 / 75 / 100 %
  → Calls signal_ready_for_review when done

  Human Gate (default):
    → Ticket moves to "Waiting for Human"
    → Human reviews branch + live preview
    → Human moves card to "Done" → branch auto-merges to main

  AI Gate (AI Verify OFF):
    → Branch auto-merges to main immediately
    → Ticket moves to "Done" automatically
    → No human step required

  AI Gate (AI Verify ON, e.g. verify_count=2):
    → signal_ready_for_review → Round 1 of 2:
        PASS: comment "Round 1/2: PASSED" → agent calls signal_ready again
        FAIL: comment "Round 1/2: Issues Found" → agent fixes → signal_ready
    → signal_ready_for_review → Round 2 of 2:
        PASS: branch auto-merges to main, ticket moves to "Done"
        FAIL: comment "Round 2/2: Issues Found (final)" → agent fixes → signal_ready
              next signal_ready → merges with no further verification
    (LLM errors are fail-open: merge proceeds; kanban errors are fail-safe: default to 1 round)
```

---

## AI Verify

AI Verify adds an independent LLM code-review step to the AI Gate auto-merge path. It is disabled by default and can be toggled per-project or per-ticket from the Kanboard UI.

### How it works

1. The worker AI agent finishes its task and calls `signal_ready_for_review`.
2. Marcus fetches the unified diff between the ticket branch and `main`.
3. A second LLM call is made with a prompt containing the ticket title, acceptance criteria, and the diff. The LLM acts as a senior code reviewer.
4. The LLM responds with a JSON object `{"passed": bool, "findings": [...]}`.
5. **If passed:** the branch merges to `main` and the ticket closes as usual.
6. **If failed:** Marcus posts a "Marcus AI Verifier — Issues Found" comment listing each finding and tells the worker what to fix. The ticket stays "In Progress". The worker reads the comment, fixes the issues, and calls `signal_ready_for_review` again — triggering a fresh verification run. This repeats until the review passes.

### Failure modes and safety

| Scenario | Behaviour |
|---|---|
| LLM API is down or returns garbage | **Fail-open** — merge proceeds. A transient outage should not block shipping. |
| Kanban API unreachable when checking verify setting | **Fail-safe** — verification runs. An outage should not silently bypass the review. |
| Branch diff is empty (no code changed) | **Fail** — verification returns "No implementation found" immediately without calling the LLM. |
| Diff exceeds 12,000 characters | Diff is truncated before sending. Truncation is noted in the prompt so the LLM knows. |

### Enabling AI Verify

**Project level (board header):**
1. Set the project gate to **AI Gate** — the **AI Verify** round counter appears next to it (`[−] 0 [+]`).
2. Click **`+`** to increase the number of required verification rounds (0 = disabled).

**Per-ticket override (task sidebar):**
1. Open a ticket. The **Marcus Gate Mode** panel shows the current effective verify state.
2. When the effective gate is AI, an **AI Verify rounds** counter appears. Use `[−]` and `[+]` to set a per-ticket round count. Click **↩** to reset and inherit from the project setting.

---

## HTTP endpoints

When `MARCUS_AGENT_TOKEN` is set (automatic once you allow remote access — see [Authenticating remote agents](#authenticating-remote-agents)), **every** endpoint below except `/webhooks/kanboard` and `/webhooks/gitea` requires an `Authorization: Bearer <token>` header and returns `401` without it. Those two webhook routes authenticate separately (their own `?token=` / HMAC signature). With no token set (localhost-only default), all endpoints are open.

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | GET/POST | MCP protocol — all AI agent tooling |
| `/webhooks/kanboard` | POST | Receives Kanboard push webhooks (own `?token=` auth) |
| `/webhooks/gitea` | POST | Receives Gitea push webhooks, triggers an instant dev-env refresh (own `X-Gitea-Signature` HMAC auth — see [Hot-reload dev environments](#hot-reload-dev-environments)) |
| `/dev-env/view?ticket_id=<id>&project_id=<id>` | GET | Starts hot-reload dev environment, redirects to preview URL |
| `/dev-env/stop?ticket_id=<id>` | POST | Tears down a running dev environment |
| `/api/dev-env/status?ticket_id=<id>` | GET | Returns `{running, url}` for a ticket's dev environment |
| `/api/dev-env-setting` | GET/PUT | Global `max_parallel_containers` limit (`null` = unlimited) — see [Hot-reload dev environments](#hot-reload-dev-environments) |
| `/api/active-agents` | GET | All tickets currently claimed by an AI agent |
| `/api/ticket-links?ticket_id=<id>` | GET | Dependency graph split into `depends_on`, `blocks`, `relates_to` |
| `/project-description?project_id=<id>` | GET | Editable project description page |
| `/api/project-description?project_id=<id>` | GET/PUT | Project description plain-text API |
| `/api/gate-setting?project_id=<id>[&ticket_id=<id>]` | GET | Current gate + verify settings; returns `project_gate`, `ticket_gate`, `effective`, `project_verify_count`, `ticket_verify_count`, `effective_verify_count` |
| `/api/gate-setting/project` | PUT | Set project-level gate (`human`\|`ai`) and/or `verify_count` (int ≥ 0) |
| `/api/gate-setting/ticket` | PUT | Set per-ticket gate override (`human`\|`ai`\|`null`) and/or `verify_count` (int ≥ 0 or `null` to inherit) |

### Hot-reload dev environments

Clicking **Open** in a ticket's **Marcus Dev Environment** panel (or visiting `/dev-env/view?ticket_id=<id>&project_id=<id>`) starts a Docker container running that ticket's branch, with hot reload, and redirects your browser to it. Marcus spawns this as a *sibling* container on the host — not nested inside its own container — via a `/var/run/docker.sock` mount (Docker-outside-of-Docker; see `docker-compose.yml`'s `marcus.volumes` comment for the security tradeoff this implies).

**Instant refresh, no polling:** every dev-env container's code directory is the same host path Marcus and the agent both operate on. The first time an agent asks for a ticket's work context, Marcus auto-creates that project's Gitea repo *and* a push webhook (`GiteaManager.create_webhook`, signed with `GITEA_WEBHOOK_TOKEN`) — zero manual clicks in Gitea's UI. From then on, every `git push` to the ticket branch POSTs to `/webhooks/gitea`, which runs `git fetch && git reset --hard` inside the running container. The container's own file-watcher (inotify restart loop, or the stack's native hot-module-reload for Node/Vite, cargo-watch, air) picks up the change automatically — the same mechanism as if you'd run `git pull` and the dev server noticed on its own.

**Resource limit:** the board header's "Max dev environments" `[−] N [+]` counter (backed by `/api/dev-env-setting`) caps how many of these containers can run at once, globally. Once the limit is hit, `/dev-env/view` returns an error until an existing environment is stopped — `∞` (the default) means no limit.

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

---

## License

MIT — see [LICENSE](LICENSE).
