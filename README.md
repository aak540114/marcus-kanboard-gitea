# The MKG Project

**MKG** stands for **Marcus-Kanboard-Gitea** — a production deployment of **[Marcus](https://github.com/lwgray/marcus)** — the board-mediated AI multi-agent orchestrator — wired to **Kanboard** for ticket management, **Gitea** for git repositories, and a custom Kanboard plugin that gives every board a live AI control panel.

> **What Marcus is:** see the [Marcus README](https://github.com/lwgray/marcus) and [docs](https://marcus-ai.dev). This repo is an opinionated deployment of it, not a fork.

> **What Kanboard is:** [Kanboard](https://kanboard.org) is a free, open-source, self-hosted kanban board — plain columns and cards for tracking tickets, with no dependency on a third-party SaaS. This repo runs a stock, unmodified Kanboard instance plus one custom plugin, **MarcusDevEnv** (see below), that adds AI-aware controls to the board UI.

> **What Gitea is:** [Gitea](https://about.gitea.com) is a lightweight, self-hosted git server — repositories, branches, pull requests, and a REST API, GitHub-like but running entirely on your own infrastructure. This repo runs a stock, unmodified Gitea instance purely as the git host for each Kanboard project's code.

---

## What this repo adds

| Feature | Description |
|---|---|
| **Kanboard provider** | Full Kanboard JSON-RPC integration — tickets, columns, comments, assignments |
| **Per-project access gate** | Marcus can see multiple Kanboard projects, but only works tickets in ones a human has explicitly allowed. Every project starts **disabled** — no Gitea repo, no column reconciliation, no AI agent claiming a ticket — until you flip the **"Marcus: OFF/ON for this project"** toggle in that project's board header. Enabling provisions it immediately; disabling blocks new ticket claims from that point on (a ticket an agent is already mid-way through is not force-interrupted). See [Scoping Marcus to specific projects](#scoping-marcus-to-specific-projects). |
| **Gitea integration** | `GiteaManager` + `ProjectSyncWorkflow` (`src/integrations/gitea_manager.py`, `src/workflows/project_sync_workflow.py`) auto-create a Gitea repo per **[Marcus-enabled](#scoping-marcus-to-specific-projects)** Kanboard project. Creation is **instant** once enabled: opening the project's page pings `/api/project-seen` so Marcus provisions the repo (and Marcus's columns) right away — Kanboard has no server-side "project created" event, so this browser ping is what replaces waiting on a poll. A slow `ProjectWatcher` backstop poll (default 5 min, `PROJECT_POLL_INTERVAL`) still catches API/DB-created projects and retries failures, and the repo is also created on-demand the first time an agent calls `get_work_context` — no manual repo setup. |
| **Parallel agents** | `HumanGatedWorkflow` keeps up to `MARCUS_MAX_PARALLEL_AGENTS` (default 3) tickets in progress at once, each held by a distinct agent "slot". A busy slot is never preempted, so an agent actively working is never interrupted; extra assigned tickets simply wait for a free slot. |
| **Orchestrate mode (`marcus_work`)** | Marcus is the manager, the agent is a worker. Prompt any agent to "connect to Marcus and do what it says": it loops on ONE tool, `marcus_work`, which hands out the next ticket that's **assigned to a human (anyone) and in Ready**, returns exact instructions, LLM-summarizes each worker report onto the ticket as a comment (~every 10 s, driven by the worker's callbacks), and completes the ticket through the project's gate on `DONE`. |
| **Ticket decomposition** | Marcus splits a big ticket into 2–5 independent sub-tickets — created on the **parent's board**, linked "is a child of", inheriting the parent's owner and Ready status — so multiple agents work them in parallel; the parent parks in Blocked until its children finish, then moves itself to **Waiting for Human** for review (never straight to Done — a human always signs off on the assembled work). Automatic when a ticket with 4+ acceptance criteria is handed to a worker, or on demand via a **`@marcus decompose`** comment. Needs an LLM configured (`claude_subscription` works). A background sweep re-checks every Blocked parent every 60s (not just the moment a child closes), so a parent whose children finished while Marcus was mid-restart or a webhook was dropped still advances on its own instead of staying stuck. |
| **Per-project decompose toggle** | A second board-header switch, independent from the Marcus ON/OFF gate — **"🪓 Decompose: ON/OFF"** — controls only whether Marcus is allowed to auto-split large tickets (or honor `@marcus decompose`) *in that project*. Defaults **ON**. Useful for a project where you always want tickets worked as single units. See [HTTP endpoints](#http-endpoints) (`/api/decompose-setting`). |
| **Approve from the board or a comment** | Dragging a card to **Done** merges its branch to `main` (fixed: Kanboard fires a column-move event, not a close event — Marcus now honors both). Commenting **`@marcus approve`** (or plain "approve"/"lgtm"/"merge to main") on a waiting ticket does the same; negated/conditional comments ("don't merge", "approve after you fix X") count as change requests instead. Marcus merges the agent's **pushed** branch (fetched from Gitea), not its own stale local copy. |
| **Merge-conflict tag** | If a merge to `main` fails, Marcus tags the ticket's Kanboard card **`merge-conflict`** — visible on the board without opening the ticket — so a card that bounces back to Ready/In Progress doesn't read as an unexplained reset. The tag clears automatically once the ticket is resubmitted (moves back to Waiting for Human) or a later merge succeeds. |
| **Live board refresh (SSE push)** | The Kanboard UI updates the instant Marcus or an agent changes anything — comments, column moves, state — via one Server-Sent Events stream (`/api/events/stream`). No polling, no manual reload; a pending refresh is held while you're typing a comment and applied when you stop. |
| **Zero-setup agent clone** | `get_work_context` returns a ready-to-run `clone_url` (browser-facing host from `GITEA_PUBLIC_URL`, git credentials embedded unless `MARCUS_EMBED_GIT_CREDENTIALS=false`). The agent clones into **its own** directory — no manual clone, and parallel agents never share a working tree. Prefer a scoped `GITEA_AGENT_TOKEN` over the admin token. |
| **Kanboard → code links** | The board header links to the project's Gitea repo; each ticket's sidebar links to the exact branch it's worked on — jump from board to code in one click. |
| **MarcusDevEnv plugin** | Kanboard plugin that adds AI-aware UI to every board and task |
| **Hot-reload dev environments** | One-click per-ticket preview URL; supports any language/framework. Marcus decides how to run the code in three steps, cheapest first: a declared Tech Stack (see below), corrected against a mismatched repo; failing that, file-sniffing the repo itself (`package.json`, `manage.py`, etc.); failing that, an AI read of the repo's own README/manifests/entrypoints — so a preview no longer depends on a human ever writing a project description at all. Whichever stack wins is mirrored into the repo's own `README.md` under a "Dev Environment Preview" section, so `git clone`-ing the repo directly still tells you how to run it. Refreshes **instantly** on every `git push` to the ticket branch via a Gitea webhook (`/webhooks/gitea`) — no polling, no manual webhook setup — including on a follow-up push after a "waiting for human" review comment sends the ticket back to an agent, so an already-open preview never needs to be stopped and reopened to see the update. A global "Max dev environments" setting caps how many preview containers can run at once. |
| **Main-branch preview** | A second, project-level Start/Stop Preview button (board header, not the task sidebar) deploys the project's `main` branch instead of a ticket's — so a human can see what's currently live/merged at any time. Refreshes automatically on every push to `main`, whether from a direct push or a ticket merge, the same way a ticket preview refreshes on pushes to its own branch. Shares the same global "Max dev environments" limit as ticket previews. |
| **Project Description system** | Per-project markdown doc (tech stack, architecture notes) AI agents read via `get_project_description`. Marcus **infers** it from the ticket when it's missing — instead of blocking the ticket on a human — and agents can refine it via `update_project_description`. A human's edit always wins and is never overwritten. Editable from the board. Dev-environment preview start also self-heals this doc's Tech Stack section straight from the repo (file-sniffing, then an AI read) whenever it's missing or contradicts the real code — see **Hot-reload dev environments** above. |
| **Enriched ticket context** | `get_work_context` — the first call every agent makes — also returns labels, dependency links (`depends_on`/`blocks`/`relates_to`), and the ticket's last 10 comments, so a human's reply to a paused ticket is actually visible to the agent |
| **Human Gate / AI Gate toggle** | Per-project and per-ticket control over whether humans review AI work before it merges |
| **Manual-testing instructions** | The "Ready for Review" comment posted when a Human Gate ticket reaches **Waiting for Human** includes an LLM-authored, ticket-specific "How to test this" checklist — concrete steps to exercise the change in the live preview (e.g. "open the Checkout page, confirm the Submit button now renders green"), not just a generic AC checklist. Falls back to a heuristic built from the acceptance criteria when no LLM is configured. |
| **AI Verify** | Configurable N-round LLM code review before any AI-gate merge; each round posts a comment enumerating every finding verbatim; agent fixes issues between rounds; 0 rounds = disabled |
| **Clone this project** | A **"📋 Clone this project"** button in every board header creates a brand-new Kanboard project + Gitea repo that replicates a baseline project's entire visible state — every ticket (title, description, column, labels, links), the project description, gate/access/decompose settings, and the full git history (every branch, via a mirror clone) — under a human-supplied new name. The clone is fully isolated from the baseline the moment it's created. See [Cloning a project](#cloning-a-project). |
| **Project stats** | A **"📊 Project Stats"** board-header link opens a per-project page tracking tickets moved into **Done** and **Waiting for Human** per hour (starting from each column's first-ever move), an hours-vs-tickets chart, and the repository's total line count on `main`, kept current on every Done move. See [Project stats](#project-stats). |
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
| **Marcus ON/OFF toggle** | The master switch for this project — see [Scoping Marcus to specific projects](#scoping-marcus-to-specific-projects). Off by default; nothing else in this table does anything until you turn it on. |
| **Agent presence badge** | Two live counts: **connected** (agents polling Marcus for work every ~10 s, counted even when idle) and **working** (agents actively working a claimed ticket — a strict subset). Hover to see each claimed ticket, its agent, and that agent's reported subscription usage. Updates every 15 s. |
| **Actively-worked card highlight** | Cards an AI agent is working **right now** get a pulsing golden ring. It's driven by a *liveness* signal — the agent reported progress within the last ~40 s — **not** by ticket state/column, so a state-management bug that leaves a card stuck can't make the ring lie. It clears the moment the agent stops (finished, handed off, blocked, or went silent). Re-applied after Kanboard's own board redraws, so it never gets lost. |
| **Project Description button** | Opens the Marcus-served project description page for this project — the AI agents' shared source of truth for language, framework, and architecture. |
| **Project Stats button** | Opens the Marcus-served [project stats](#project-stats) page — tickets/hour into Done and Waiting for Human, plus the repo's total line count on `main`. |
| **Repository button** | Links to this project's Gitea repository (opens in a new tab). Appears once the repo has been provisioned. |
| **Clone this project button** | Prompts for a new project name, then creates a full, isolated copy of this project (tickets, settings, description, git history) under that name. See [Cloning a project](#cloning-a-project). |
| **Human Gate / AI Gate toggle** | Sets the project-level gate mode. Human Gate (default): AI pauses for human review before done. AI Gate: AI merges and closes autonomously. |
| **Decompose ON/OFF toggle** | Separate from the Marcus ON/OFF switch above — controls only whether Marcus may auto-split a large ticket into sub-tickets (or honor `@marcus decompose`) in this project. Defaults **ON**. |
| **AI Verify counter** | Appears when AI Gate is active. `[−] N [+]` sets how many sequential LLM review rounds run before the branch auto-merges. 0 = disabled. |
| **Max dev environments counter** | Global, always visible. `[−] N [+]` caps how many "Open Dev Environment" Docker containers can run at once across every ticket — `∞` (default) means unlimited. Once the limit is reached, starting a new one fails until an existing one is stopped. |
| **Start/Stop Main Preview** | Project-level (not per-ticket) hot-reload preview of the project's `main` branch — separate from each ticket's own preview button in its sidebar. Starts/stops a container the same way, refreshes automatically on every push to `main`, and counts against the Max dev environments limit above. |
| **Live refresh** | (Invisible widget.) The page holds one SSE connection to Marcus and reloads the moment Marcus/an agent changes anything — no manual refresh. Deferred while you're typing or a Kanboard dialog is open. |

### Task sidebar
| Panel | What it does |
|---|---|
| **Marcus Code** | Link to the exact Gitea branch this ticket is worked on, so you can review the code updates on the branch at any time. |
| **Agent Subscription Usage** | When an AI agent is actively working this ticket and its account reported usage, shows that account's usage / limit (self-reported via `marcus_work`; a self-hosted/unlimited model shows the limit as **∞**). Usage is kept **per account**: two agents on one subscription show the same shared figure, while agents on different accounts stay separate — each ticket shows only its own agent's account. |
| **Marcus Dev Environment** | Start / Open / Stop a hot-reload preview for this ticket's branch. Any language — stack comes from the project description. |
| **Marcus Gate Mode** | Per-ticket gate override. Shows the project default; lets you switch this ticket to Human or AI gate independently. Ticket setting overrides project setting. Includes a per-ticket AI Verify override when AI Gate is active. |
| **Marcus Dependencies** | Dependency graph: *Depends on*, *Blocks*, *Related* — each with a colour-coded column-status badge. |
| **Live refresh** | (Invisible.) Same SSE stream as the board: a new comment or state change from Marcus/an agent reloads the task view instantly — never while you're mid-comment. |

### Dashboard page

Kanboard's own `/dashboard` page (the "My projects" list you land on after login, not a Marcus-served page) gets a small badge next to each project's name:

| Badge | Meaning |
|---|---|
| 🔓 **Marcus: ON** | Marcus is enabled for this project — see [Scoping Marcus to specific projects](#scoping-marcus-to-specific-projects). |
| 🔒 **Marcus: OFF** | Marcus is not enabled for this project. Open its board to turn it on. |
| ⚠ **Marcus: unknown** | The badge couldn't reach Marcus to check (e.g. Marcus is down). Not the same as OFF. |

This means you can tell which of your projects Marcus is working without opening each one individually. Each badge starts as "⏳ Marcus" (checking) and resolves a moment later: the page collects every listed project's id, then makes one `GET /api/project-enabled?project_id=<id>` call per project (the same endpoint the board-header ON/OFF toggle uses) and fills in the badge from the response.

---

## Architecture

All three services run as containers on one `docker compose` network and reach each other by service name (`kanboard`, `gitea`, `marcus`) — only the host-side port mappings (8080, 3000, 4298) matter from outside Docker.

```
Human (browser)
  │  creates project, ticket           │  assigns, sets "Ready"
  ▼                                    ▼
kanboard (container, host port 8080) ← Kanboard JSON-RPC API (internal port 80)
  │  plugin push (instant) +            │  BoardWatcher polls (30s) + webhook (instant)
  │  ProjectWatcher backstop (5m)       │
  ▼  /api/project-seen, getAllProjects  ▼  getAllTasks()
marcus (container, host port 4298) ─── marcus (container)
  │  GiteaManager + ProjectWatcher     │  BranchManager + HumanGatedWorkflow
  ▼  POST /api/v1/user/repos           ▼  git push branch
gitea (container, host port 3000) ──── gitea — branch per ticket

AI agents (Claude Code, Codex, etc.)
  └── connect to http://localhost:4298/mcp  (MCP protocol)
      │   (remote agents: + Authorization: Bearer <MARCUS_AGENT_TOKEN>)
      ├── get_work_context           → clone_url → git clone (own dir)
      ├── signal_ready_for_review    → Human Gate: "Waiting for Human"
      │                              → AI Gate:    auto-merge + "Done"
      ├── signal_waiting_for_human   → Human Gate: pause for input
      │                              → AI Gate:    post note, continue
      └── post_ticket_progress
```

### How AI agents reach tickets

AI agents never call Kanboard's JSON-RPC API and never receive Kanboard's API token. They call Marcus's MCP tools — in orchestrate mode just **`marcus_work`** (Marcus assigns, guides, and summarizes), or the individual tools (`get_work_context`, `get_project_description`, `post_ticket_progress`, `signal_ready_for_review`, …); Marcus alone holds `KANBOARD_API_TOKEN` and is the only thing that makes JSON-RPC calls to Kanboard, over the internal Docker network (`http://kanboard/jsonrpc.php`, not the host-published `:8080`). No tool response ever contains a Kanboard URL or credential. This is why gating Marcus's HTTP endpoint with a bearer token (see [Authenticating remote agents](#authenticating-remote-agents)) is sufficient to control ticket access: it's the *only* door.

`get_work_context` — the first call every agent makes — returns everything Marcus knows about a ticket: title, description, acceptance criteria, a ready-to-run `clone_url` (plus `repo_web_url`/`branch_web_url`), branch name, labels, dependency links (`depends_on`/`blocks`/`relates_to`), and its last 10 comments (see `prompts/Kanboard_Agent_Prompt.md` for the full field reference). `get_project_description` returns the project-wide tech stack and architecture notes when per-ticket context isn't enough.

Agents do talk to **Gitea** directly, but only to `git clone` the `clone_url` into their own directory and `fetch`/`push` on the one branch Marcus created for them — a different, narrower surface than the board itself. They never share Marcus's own clone, so parallel agents don't collide.

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

This one command does everything the individually-numbered steps below used to require by hand: asks how Marcus itself should run (in Docker, or natively on this host — see [Hybrid mode: Marcus outside Docker](#hybrid-mode-marcus-outside-docker)), starts Kanboard and Gitea, creates the Kanboard project and its six required columns, sets the Kanboard API token and webhook, creates the Gitea admin account and access token, picks and wires up an AI provider for Marcus's own decomposition/analysis calls (see [AI provider](#ai-provider) — no API key prompt), then starts Marcus itself either way — builds and starts its container (Docker mode), or `exec`'s into `./scripts/run_marcus_native.sh` as its own last step (native mode) — so `./scripts/setup.sh` alone is enough to end with Marcus actually running, no second command needed.

It's safe to re-run — every step checks live state before creating or changing anything, so running it again after `docker compose down` is a fast no-op, and running it after `docker compose down -v` (which wipes volumes) re-provisions everything from scratch.

When it finishes it prints the Kanboard/Gitea/Marcus URLs, the Gitea admin password, which AI provider got selected, and the exact `claude mcp add` command for step 2 below — both for connecting from this machine and from a remote one.

<details>
<summary><strong>How the setup script works</strong> (click to expand)</summary>

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

</details>

### 2. Connect your AI agent

Point any MCP-compatible agent at `http://localhost:4298/mcp`. For Claude Code:

```bash
claude mcp add --transport http marcus http://localhost:4298/mcp
```

This always works from the same machine Marcus runs on. Connecting from a **different machine** (another laptop, a remote VPS) additionally requires you to have opted in during setup — see [Network access](#network-access).

Once connected, the simplest way to run an agent is **orchestrate mode** — prompt it with roughly:

> Start `n` agents. Each does the following: call the `marcus_work` tool with no arguments and do exactly what the returned `message` says. Every ~10 seconds call `marcus_work` again with the `agent_id`/`ticket_id` it gave you plus a one-line `report`. Report `DONE - <summary>` when finished.

Replace `n` with however many agents you want polling Marcus in parallel (e.g. "Start 3 agents..."). Each one calls `marcus_work` independently and gets its own auto-generated worker id, so they naturally land on different tickets — see [Running multiple agents](#3-running-multiple-agents--multiple-accounts) for how that works under the hood.

Marcus hands out the next human-readied ticket, posts a summarized progress comment on each report, and completes the ticket through the gate — the agent needs no other tool. Alternatively, point the agent at a specific ticket: it calls `get_work_context`, which returns a `clone_url` it uses to `git clone` the repo into its own directory, then works on the pre-made branch. `prompts/Kanboard_Agent_Prompt.md` is the full agent operating manual (auth, gate modes, both flows). For a **remote** agent to clone a private repo seamlessly, set `GITEA_PUBLIC_URL` to a browser-reachable address and provide a `GITEA_AGENT_TOKEN`.

### 3. Running multiple agents / multiple accounts

Marcus is already a parallel multi-agent coordinator — you don't wire anything special. **Each MCP session that calls `marcus_work` with no `agent_id` gets its own worker id auto-generated** (`worker-<hex>`), which it echoes back on later calls. So "N agents" just means **N MCP client sessions each running the orchestrate prompt above**. When one worker is handed a ticket, Marcus claims it under that worker's id, so the next worker's `marcus_work` call skips it and takes the next Ready ticket — two agents naturally land on different tickets, different branches, both `In Progress`.

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

Give **both** sessions the same orchestrate prompt from step 2 (or the fuller version in `prompts/Kanboard_Agent_Prompt.md` §0).

**Creating actual parallel work.** Concurrency is bounded by how many workable tickets exist. Either:
- put **2+ tickets in `Ready`, each assigned to a human** (assigned-to-anyone + Ready is the trigger) — one agent per ticket; or
- create **one big ticket (4+ acceptance criteria)** — Marcus auto-decomposes it into sub-tickets (each Ready) that the agents pick up independently (or force it with a `@marcus decompose` comment).

Dependencies are respected: a ticket that `depends_on` another is held (Blocked) until its dependency merges, so agents never build on unfinished work. `MARCUS_MAX_PARALLEL_AGENTS` (default `3`) caps Marcus's internal auto-start slot pool — two agents are well under it, so no change is needed.

**Who pays for what.** Each account's *coding* rides its own subscription — that's the parallelism. Marcus's *own* orchestration calls (decomposition, acceptance-criteria generation, report summaries) are a **separate** budget: whatever Marcus itself is configured with (its own `claude` CLI login or an API key — see [AI provider](#ai-provider)). Effectively three LLM identities: A codes, B codes, Marcus coordinates.

### Scoping Marcus to specific projects

A single Marcus install can see **every** Kanboard project on the board. Left unchecked, that means a brand-new project you create just to sketch something out would immediately get its own auto-created Gitea repo, its columns reconciled to Marcus's layout, and any ticket in it picked up by an AI agent — whether or not you meant for Marcus to touch it.

**Every project starts disabled** — including the one `scripts/setup.sh` creates. Marcus does nothing on a project — no repo, no columns, no claimed tickets, no agent commits — until a human explicitly opts it in. Open that project's board in Kanboard and click the **"🔒 Marcus: OFF for this project"** button in the header (it's the first control, to the left of the active-agents badge); it flips to **"🔓 Marcus: ON for this project"** and Marcus provisions the repo + columns immediately (no waiting on the backstop poll).

A few things worth knowing:
- **Marcus SEES every project, but only ACTS on enabled ones.** It reads all boards so it can tell you "this project has ready tickets but isn't enabled" and so a deleted ticket is noticed anywhere — but every write (claiming, commenting, moving a card, merging) is gated on the toggle. Disabling a project never hides or deletes its tickets from Marcus's view; it just stops Marcus touching them.
- **Every `marcus_work` poll re-reads the boards.** A ticket you have just assigned and moved to Ready is handed to a polling agent on its next poll, rather than waiting for the background `BoardWatcher` tick (30 s by default) — which also means this works with webhooks disabled. Near-simultaneous polls from several agents share one board read.
- **Deleted tickets stop being tracked.** Kanboard fires no event when a task is deleted (`TaskModel::remove()` dispatches nothing, and there is no `EVENT_REMOVE` constant), so the bundled **MarcusDevEnv plugin** supplies one: it overrides Kanboard's task model to POST a `task.remove` webhook to Marcus. Marcus then drops the ticket, releases any claim on it and stops its preview container. A board read catches it too, so deletions are still noticed if the plugin isn't installed — just not instantly. On startup Marcus re-checks every tracked ticket, which is what catches tickets deleted while it was stopped.
- **It is per project, and project ids are not board names.** Enabling one project never covers another. When Marcus withholds tickets it names the project by id *and* name so you can find the right board.
- **You can see every project's ON/OFF state at once from Kanboard's own `/dashboard` page**, without opening each board — see [Dashboard page](#dashboard-page).
- **This is a separate control from the Human/AI Gate toggle.** The access toggle decides *whether* Marcus may touch a project at all; the Gate toggle (next to it) decides *how* it works once it's already allowed to (pause for your review vs. work autonomously to done).
- **Disabling a project is not a kill switch for work already in flight.** It blocks Marcus from claiming any *new* ticket in that project from that point on; an agent partway through an already-claimed ticket is left to finish rather than being force-interrupted mid-commit.
- **This upgrade is a breaking change on purpose.** If you're updating an existing deployment, every project you were already using goes to disabled the moment you redeploy — including your "main" project. Re-enable it from its board header before expecting Marcus to keep working there.
- **The ON/OFF state survives a teardown.** It lives in `data/project_access_settings.json`, and `scripts/teardown.sh` deletes nothing — so `teardown.sh` followed by `setup.sh` comes back up with exactly the projects you had enabled. Setup never changes it either way.
- Toggle it from a script instead of the UI with `GET`/`PUT /api/project-enabled?project_id=<id>` (see [HTTP endpoints](#http-endpoints)).

### Tearing down

```bash
./scripts/teardown.sh
```

Stops every container (Kanboard, Gitea, Marcus, Caddy if you used HTTPS) and a natively-run Marcus process (hybrid mode), then prints every location that holds real data — `./data`, `./logs`, Docker's named volumes, `.env` — with rough sizes, so you can decide what to delete yourself. **It doesn't delete anything on its own** — re-running `./scripts/setup.sh` afterward picks up exactly where you left off. It also explicitly calls out `~/.claude.json` / `~/.claude/.credentials.json` as *not* Marcus's data (that's your real Claude Code login — Marcus only ever reads it), so you don't mistake it for something safe to clear out.

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

## Full ticket lifecycle

> Everything below assumes the ticket's project has been [enabled for Marcus](#scoping-marcus-to-specific-projects) — a new project starts disabled, and none of this happens until you flip that toggle.

```
Human creates ticket in Kanboard
  → Marcus generates acceptance criteria (AI)

Human assigns ticket (to anyone) + moves to "Ready"
  → Marcus checks project description for tech stack
  → If stack missing: INFERS it from the ticket (LLM); only asks the human
    if it can't even guess
  → If the ticket is big (4+ acceptance criteria) and handed out via
    marcus_work: Marcus may DECOMPOSE it into linked sub-tickets on the
    same board (parent parks in Blocked until children finish, then
    moves itself to Waiting for Human for review)
  → Creates branch in Gitea, moves to "In Progress"

AI agent works on the branch (its own clone)
  → Orchestrate mode: agent loops on marcus_work; Marcus posts a
    summarized progress comment on each ~10 s report
  → Classic mode: agent posts progress comments itself, then calls
    signal_ready_for_review when done

  Human Gate (default):
    → Ticket moves to "Waiting for Human"
    → Marcus posts a "Ready for Review" comment: AC checklist, preview
      link, and a "How to test this" step-by-step walkthrough tailored
      to what THIS ticket actually changed (LLM-authored from the
      branch diff; falls back to the AC checklist without an LLM)
    → Human reviews branch + live preview, following those steps
    → Approve: drag the card to "Done" OR comment "@marcus approve"
      (plain "approve"/"lgtm" works too) → Marcus fetches the agent's
      pushed branch and merges it to main
    → Request changes: any other comment → back to "In Progress", agent
      resumes with your feedback

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

## Cloning a project

Every board header has a **"📋 Clone this project"** button. Click it, type a new project name, and Marcus creates a brand-new Kanboard project + Gitea repo that replicates the baseline project's entire visible state — under that new name, in the background (the click starts a job and polls for its result, since a large project can take a while to clone).

**What gets copied:**
- Every ticket — title, description, column/status, labels, and dependency/relation links between cloned tickets — recreated as brand-new tickets on the new project's board, not references to the originals.
- The project description document, including whether a human has locked it against automated updates.
- Gate mode, AI Verify round count, decompose-enabled, and the Marcus ON/OFF access setting — each copied only if the baseline has an explicit value; an unconfigured baseline setting means the clone also falls back to Marcus's hard default, not a frozen copy of it.
- The git repository — every branch, under its original name, via a full mirror clone (not just `main`). A ticket that was in progress on the baseline gets its clone's branch seeded from the baseline ticket's branch, so an agent can resume exactly where the original left off, and the clone's lifecycle state (Ready / In Progress / Blocked / Waiting for Human / Done) mirrors the baseline ticket's state at clone time.

**What starts fresh, not copied:** the new project's [Project Stats](#project-stats) (ticket-movement history, line-of-code count) and Marcus cost-tracking data — a clone's own history starts the moment its own tickets start moving, not backdated from the baseline's.

**Isolation.** The moment a clone is created, it is completely independent of its baseline — a separate Kanboard project, a separate Gitea repository, separate settings entries, separate lifecycle records for every ticket. Changing the baseline's gate mode, editing its description, or moving one of its tickets afterward never touches the clone, and vice versa. This is verified directly: `tests/unit/workflows/test_project_clone_isolation.py` wires the clone workflow against the same real settings/lifecycle stores Marcus runs in production and asserts each direction explicitly.

Triggered via `POST /api/clone-project` (`{"baseline_project_id": int, "new_name": str}` → `{"job_id": str}`) and polled via `GET /api/clone-project-status?job_id=<id>` — see [HTTP endpoints](#http-endpoints).

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

## Project stats

Every board header links to a **"📊 Project Stats"** page (`/project-stats?project_id=<id>`) tracking three things per project, refreshed automatically every 30s:

| Stat | Tracked from |
|---|---|
| **Tickets moved to Done, per hour** | Every real move into the Done column, deduplicated against the double-delivery that happens when both a Gitea/Kanboard webhook and the next board poll report the same transition. Tracking for a project starts the first time any of its tickets is ever moved to Done — there's no backfill before that. |
| **Tickets moved to Waiting for Human, per hour** | Same tracking, for the Waiting for Human column. |
| **Lines of code on `main`** | `git diff --shortstat` against the empty tree, on the project's Gitea repo — every tracked line counts as an "insertion" relative to nothing, and git itself excludes binary files from that count. Recomputed every time a ticket is freshly counted as moved to Done, so the figure is always current without polling git on every page load. |

The page shows each stat's count for the current hour as a headline number, plus an hours-vs-tickets bar chart for Done and Waiting for Human — hours with zero movement are simply omitted (not shown as empty bars), and each bar is labeled with its actual date/time.

Backed by `GET /api/project-stats?project_id=<id>` (see [HTTP endpoints](#http-endpoints)) and `src/core/project_stats.py`.

---

## HTTP endpoints

When `MARCUS_AGENT_TOKEN` is set (automatic once you allow remote access — see [Authenticating remote agents](#authenticating-remote-agents)), **every** endpoint below except `/webhooks/kanboard` and `/webhooks/gitea` requires an `Authorization: Bearer <token>` header and returns `401` without it. Those two webhook routes authenticate separately (their own `?token=` / HMAC signature). With no token set (localhost-only default), all endpoints are open.

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | GET/POST | MCP protocol — all AI agent tooling |
| `/webhooks/kanboard` | POST | Receives Kanboard push webhooks (own `?token=` auth) |
| `/webhooks/gitea` | POST | Receives Gitea push webhooks, triggers an instant dev-env refresh for a ticket branch or a project's main branch (own `X-Gitea-Signature` HMAC auth — see [Hot-reload dev environments](#hot-reload-dev-environments)) |
| `/dev-env/view?ticket_id=<id>&project_id=<id>` | GET | Starts hot-reload dev environment; serves a "building preview" page that auto-redirects the instant the app is actually listening (no more `ERR_CONNECTION_REFUSED`) |
| `/dev-env/stop?ticket_id=<id>` | POST | Tears down a running dev environment |
| `/api/dev-env/status?ticket_id=<id>` | GET | Returns `{running, serving, url}` — `running` = container alive; `serving` = the app is actually listening (probed *inside* the container, so it's correct even when Marcus itself runs in a container) |
| `/dev-env/logs?ticket_id=<id>` | GET | Auto-refreshing docker-logs viewer page for a ticket's preview container — see [Dev environment logs](#dev-environment-logs) |
| `/api/dev-env/logs?ticket_id=<id>` | GET | Returns `{running, logs, command}` — fresh `docker logs` output fetched live on every call, not a one-time snapshot |
| `/dev-env/main/view?project_id=<id>` | GET | Project-level counterpart to `/dev-env/view` — starts a hot-reload preview of the project's `main` branch instead of a ticket branch; same "building preview" polling page — see [Main-branch preview](#main-branch-preview) |
| `/dev-env/main/stop?project_id=<id>` | POST | Tears down a running main-branch preview |
| `/api/dev-env/main/status?project_id=<id>` | GET | Same response shape as `/api/dev-env/status`, for the project's main-branch preview |
| `/api/dev-env-setting` | GET/PUT | Global `max_parallel_containers` limit (`null` = unlimited), shared by ticket and main-branch previews alike — see [Hot-reload dev environments](#hot-reload-dev-environments) |
| `/api/active-agents` | GET | All tickets currently claimed by an AI agent |
| `/api/project-seen?project_id=<id>` | GET | Instant "this project exists" ping from the MarcusDevEnv plugin when a project page opens — Marcus provisions its Gitea repo + columns immediately (Kanboard has no project-created event). Idempotent; auth via `?token=`. A no-op for a project not [enabled for Marcus](#scoping-marcus-to-specific-projects) |
| `/api/project-enabled?project_id=<id>` | GET | Whether Marcus is allowed to work this project's tickets at all — `{"project_id": int, "enabled": bool}`. Default `false` |
| `/api/project-enabled` | PUT | Body `{"project_id": int, "enabled": bool}` — see [Scoping Marcus to specific projects](#scoping-marcus-to-specific-projects). Setting `enabled: true` immediately provisions the repo/columns instead of waiting on the backstop poll |
| `/api/events/stream` | GET | Server-Sent Events stream — pushes a `refresh` event the instant Marcus/an agent changes anything; the MarcusDevEnv plugin reloads the page on it (auth via `?token=`, since EventSource can't send headers) |
| `/api/ticket-links?ticket_id=<id>` | GET | Dependency graph (`depends_on`/`blocks`/`relates_to`) plus the ticket's `repo_web_url` and `branch_web_url` |
| `/api/project-repo?project_id=<id>` | GET | Browser URL of the project's Gitea repo (`null` until provisioned) — backs the board's Repository button |
| `/project-description?project_id=<id>` | GET | Editable project description page |
| `/api/project-description?project_id=<id>` | GET/PUT | Project description plain-text API (a human PUT locks it against automated overwrites) |
| `/api/gate-setting?project_id=<id>[&ticket_id=<id>]` | GET | Current gate + verify settings; returns `project_gate`, `ticket_gate`, `effective`, `project_verify_count`, `ticket_verify_count`, `effective_verify_count` |
| `/api/gate-setting/project` | PUT | Set project-level gate (`human`\|`ai`) and/or `verify_count` (int ≥ 0) |
| `/api/gate-setting/ticket` | PUT | Set per-ticket gate override (`human`\|`ai`\|`null`) and/or `verify_count` (int ≥ 0 or `null` to inherit) |
| `/api/decompose-setting?project_id=<id>` | GET | Whether Marcus may auto-decompose large tickets in this project — `{"project_id": int, "decompose_enabled": bool}`. Default `true`. Project-scoped only, no per-ticket override |
| `/api/decompose-setting` | PUT | Body `{"project_id": int, "decompose_enabled": bool}` — see [board header](#board-header) Decompose toggle |
| `/project-stats?project_id=<id>` | GET | Human-readable [project stats](#project-stats) page — tickets/hour into Done and Waiting for Human, plus the repo's line count on `main` |
| `/api/project-stats?project_id=<id>` | GET | JSON backing the stats page — `{"project_id", "loc_count", "done": {"last_hour", "hourly"}, "waiting_for_human": {"last_hour", "hourly"}}` |
| `/api/clone-project` | POST | Body `{"baseline_project_id": int, "new_name": str}` — starts [cloning a project](#cloning-a-project) as a background job, returns `{"job_id": str}` immediately |
| `/api/clone-project-status?job_id=<id>` | GET | Poll a clone job — `{"job_id", "status": "running"\|"done"\|"failed", "new_project_id", "warnings", "error"}` |

### Hot-reload dev environments

Clicking **Open** in a ticket's **Marcus Dev Environment** panel (or visiting `/dev-env/view?ticket_id=<id>&project_id=<id>`) starts a Docker container running that ticket's branch, with hot reload, and redirects your browser to it. The board header's **Start Main Preview** button does the same for a project's `main` branch instead (`/dev-env/main/view?project_id=<id>`) — see [Main-branch preview](#main-branch-preview) below. Marcus spawns this as a *sibling* container on the host — not nested inside its own container — via a `/var/run/docker.sock` mount (Docker-outside-of-Docker; see `docker-compose.yml`'s `marcus.volumes` comment for the security tradeoff this implies).

**Isolated checkout per preview:** each preview gets its **own** working tree. Marcus mounts the source repo **read-only** at `/src` and the container clones it into its own writable `/app` — so a preview can never mutate the shared repo, switch the host's branch, or race Marcus's own git operations, and two previews of different branches of the same project never fight over one working tree. The `/app` clone lives in the container's writable layer and is discarded when the container is removed.

**Instant refresh, no polling:** the isolated clone's `origin` is left pointing at the read-only `/src` mount (a local path), so live refresh works with no network and no credentials — a preview container on Docker's default bridge can't resolve the `gitea:3000` compose hostname or reach `localhost:3000` (its own loopback), so fetching from `/src` is the reliable path. The first time an agent asks for a ticket's work context, Marcus auto-creates that project's Gitea repo *and* a push webhook (`GiteaManager.create_webhook`, signed with `GITEA_WEBHOOK_TOKEN`) — zero manual clicks in Gitea's UI. From then on, every `git push` to the ticket branch POSTs to `/webhooks/gitea`, which runs `git fetch origin && git reset --hard origin/<branch>` inside the running container's isolated clone (fetching from `/src`, which Marcus's own merge/diff flows keep updated from Gitea). The container's own file-watcher (inotify restart loop, or the stack's native hot-module-reload for Node/Vite, cargo-watch, air) picks up the change automatically.

**Resource limit:** the board header's "Max dev environments" `[−] N [+]` counter (backed by `/api/dev-env-setting`) caps how many of these containers can run at once, globally. Once the limit is hit, `/dev-env/view` returns an error until an existing environment is stopped — `∞` (the default) means no limit.

**Tiny base image, runtime installed per stack:** the container runs on a bare `alpine` base (~7 MB) that ships **no** language runtime. Each ticket installs exactly the languages/packages its stack needs at start-up via `apk` (from the project description, or auto-detected from files like `package.json`/`requirements.txt`) — nothing is pre-baked, so the image stays tiny and never carries a runtime a project doesn't use.

**Node.js projects run their own script, not a guess:** rather than always running `npm run dev`, Marcus reads the project's own `package.json` and prefers whichever of `dev`, `start`, `serve`, or `develop` actually exists (in that order), falling back to `dev` only when none of them do. A project that names its dev-server script something other than `dev` now just works, instead of failing with `npm error Missing script: "dev"` and requiring a human to add an explicit dev-server command to the project description by hand.

**Always serves something, never a blank error page:** the start-up script is written so the preview port is *always* answered. If the project's real dev command can't start (a static HTML game with no build step, a missing dev script, a crash on boot), the container automatically falls back to a plain static file server and serves the branch's files as a website — so a human can still open it and see what the agents built. That fallback is BusyBox's `httpd` applet from Alpine's `busybox-extras` package (installed unconditionally alongside `git`, so it's always present, without needing a full language runtime); note it must be invoked as plain `httpd`, not `busybox httpd` — `busybox-extras` installs `httpd` as its own standalone binary rather than adding it to the base `busybox` multi-call binary, which doesn't include it. Practically, this fixes the old failure mode where a container would exit on a bad start command, vanish from `docker ps`, and leave the browser stuck on `ERR_CONNECTION_REFUSED`. If the entrypoint itself fails hard (a bad checkout or package install), the container is deliberately **not** run with `--rm` — it lingers in `exited` state so Marcus can capture its last log lines and the resolved dev-server command, then show them on the "Preview could not start" page (and force-remove the container afterward, so nothing accumulates).

**No redirect to a dead port:** starting a container is asynchronous — `docker run` returns before the app inside is listening. `/dev-env/view` therefore serves a small "building preview…" page that polls `/api/dev-env/status` and redirects your browser the instant `serving` flips true. Readiness is probed **inside** the container (a `docker exec` that reads `/proc/net/tcp` for a LISTEN socket on port 3000, using only BusyBox `sh`/`awk`), so it's correct no matter where the published port lives — including when Marcus itself runs inside a container (Docker-outside-of-Docker), where a host-loopback probe from Marcus's own network namespace would wrongly report "not up". You watch a spinner for a few seconds instead of hitting a connection-refused error and guessing when to refresh.

#### Main-branch preview

The board header's **Start Main Preview** button (see [MarcusDevEnv Kanboard Plugin](#marcusdevenv-kanboard-plugin)) is a second, project-scoped preview, separate from every ticket's own preview button in its sidebar — it deploys the project's `main` branch rather than a ticket branch, so a human can see what's actually live/merged without opening any particular ticket. It reuses everything above (isolated checkout, tiny base image, always-serves-something fallback, no-dead-redirect polling page) and shares the same global "Max dev environments" limit — it's just a different branch under a synthetic identity (`main-<project_id>`) internally.

It refreshes the same way a ticket preview does — on every `git push` — but `main` needs one extra safeguard ticket branches don't: a ticket branch's name (`ticket/<provider>/<id>`) is globally unique, so matching a push to a running preview by branch name alone is always unambiguous. A branch literally named `main` is **not** unique — every project has one — so a push to one project's `main` must not refresh a *different* project's main-branch preview. Marcus resolves the pushed Gitea repo to its Kanboard project first, then refreshes that project's preview specifically, so two projects can each run their own main-branch preview at once without cross-talk. This also means Marcus's own local clone of a project's `main` branch is fetched fresh from Gitea right before the preview starts, since (unlike a ticket branch, which Marcus creates itself) `main` can predate this feature and be stale from before any webhook was ever wired up for it.

#### Dev environment logs

A preview container can be fully "up" — port open, `/api/dev-env/status` reporting `serving: true` — while still showing a 404 or the wrong content. That happens when the real dev command Marcus resolved (from the project's Tech Stack, file-sniffing, or the AI-inferred fallback — see [Project Description system](#what-this-repo-adds)) fails inside the entrypoint script: per the "always serves something" fallback above, the container never exits, it just silently starts serving the raw repo directory as static files instead. Nothing about that failure is visible from the preview URL itself.

The **View Logs** button next to **Open Preview** / **Stop Preview** in a ticket's sidebar panel opens `/dev-env/logs?ticket_id=<id>`, a small auto-refreshing page (polls `/api/dev-env/logs` every 3s) showing that container's current `docker logs` output — stdout and stderr combined, so whatever the real dev command printed on its way to failing (a missing npm script, a Python import error, a wrong working directory) is visible immediately, without needing shell access to the host. Unlike the one-time log snapshot shown on the "Preview could not start" page (captured only when a container is found dead), this fetches fresh output on every poll, so it works for a container that's still running.

**Safety and robustness (hardened after an adversarial review pass):**
- `/dev-env/view` verifies a ticket actually belongs to the `project_id` it's given (via a live Kanboard lookup) before auto-provisioning that project's Gitea repo/webhook — a stray or spoofed `project_id` can't force-create a repo for a project it isn't tied to.
- Every `docker run`/`exec`/`stop` call has a 60-second timeout, so an unresponsive Docker daemon fails that request instead of hanging it (and, before this fix, the ASGI worker thread behind it) indefinitely.
- `/webhooks/kanboard` and `/webhooks/gitea` cap request body size before reading it into memory — both are intentionally exempt from `MARCUS_AGENT_TOKEN` bearer auth (they authenticate via their own token/HMAC signature instead), so an oversized POST is rejected (`413`) before that check ever has a chance to run.
- `refresh()` waits for a readiness marker the dev-env container writes right after its own first `git checkout`, so a push landing while the container is still installing dependencies can't race that checkout.

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
