# The MKG Project

**MKG** stands for **Marcus-Kanboard-Gitea** — a production deployment of **[Marcus](https://github.com/lwgray/marcus)** — the board-mediated AI multi-agent orchestrator — wired to **Kanboard** for ticket management, **Gitea** for git repositories, and a custom Kanboard plugin that gives every board a live AI control panel.

> **What Marcus is:** see the [Marcus README](https://github.com/lwgray/marcus) and [docs](https://marcus-ai.dev). This repo is an opinionated deployment of it, not a fork.

> **What Kanboard is:** [Kanboard](https://kanboard.org) ([GitHub](https://github.com/kanboard/kanboard)) is a free, open-source, self-hosted kanban board — plain columns and cards for tracking tickets, with no dependency on a third-party SaaS. This repo runs a stock, unmodified Kanboard instance plus one custom plugin, **MarcusDevEnv** (see below), that adds AI-aware controls to the board UI.

> **What Gitea is:** [Gitea](https://about.gitea.com) ([GitHub](https://github.com/go-gitea/gitea)) is a lightweight, self-hosted git server — repositories, branches, pull requests, and a REST API, GitHub-like but running entirely on your own infrastructure. This repo runs a stock, unmodified Gitea instance purely as the git host for each Kanboard project's code.

---

## Documentation

This README covers the essentials. Full details live under [`documents/`](documents/):

| Doc | Covers |
|---|---|
| [documents/deployment-guide.md](documents/deployment-guide.md) | Setup script internals, manual setup, running multiple agents/accounts, scoping Marcus to specific projects, network access, remote-agent authentication, AI provider selection, native (non-Docker) mode, independent per-service deployment |
| [documents/features.md](documents/features.md) | MarcusDevEnv Kanboard plugin UI reference, the full ticket lifecycle, AI Verify, cloning a project, project stats |
| [documents/api-reference.md](documents/api-reference.md) | Every HTTP endpoint Marcus exposes, plus hot-reload dev-environment internals |
| [documents/architecture.md](documents/architecture.md) | Service topology and the security model behind how AI agents reach tickets |

---

## Core Components

### What this repo adds

| Feature | Description |
|---|---|
| **Kanboard provider** | Full Kanboard JSON-RPC integration — tickets, columns, comments, assignments |
| **Per-project access gate** | Marcus can see multiple Kanboard projects, but only works tickets in ones a human has explicitly allowed. Every project starts **disabled** until you flip the **"Marcus: OFF/ON for this project"** toggle in that project's board header. See [Scoping Marcus to specific projects](documents/deployment-guide.md#scoping-marcus-to-specific-projects). |
| **Gitea integration** | `GiteaManager` + `ProjectSyncWorkflow` auto-create a Gitea repo per **[Marcus-enabled](documents/deployment-guide.md#scoping-marcus-to-specific-projects)** Kanboard project — instantly once enabled, with a slow backstop poll as a safety net. |
| **Parallel agents** | `HumanGatedWorkflow` keeps up to `MARCUS_MAX_PARALLEL_AGENTS` (default 3) tickets in progress at once, each held by a distinct agent "slot". |
| **Orchestrate mode (`marcus_work`)** | Marcus is the manager, the agent is a worker. Prompt any agent to "connect to Marcus and do what it says": it loops on ONE tool, `marcus_work`, which hands out the next ticket that's **assigned to a human (anyone) and in Ready**, and completes it through the project's gate on `DONE`. |
| **Ticket decomposition** | Marcus splits a big ticket into 2–5 independent sub-tickets so multiple agents work them in parallel; the parent parks in Blocked until its children finish, then moves to **Waiting for Human** for review. Automatic on 4+ acceptance criteria, or on demand via `@marcus decompose`. See [Full ticket lifecycle](documents/features.md#full-ticket-lifecycle). |
| **Approve from the board or a comment** | Dragging a card to **Done**, or commenting **`@marcus approve`** ("approve"/"lgtm"/"merge to main" also work) merges the branch to `main`. |
| **Live board refresh (SSE push)** | The Kanboard UI updates the instant Marcus or an agent changes anything, via one Server-Sent Events stream. |
| **Zero-setup agent clone** | `get_work_context` returns a ready-to-run `clone_url`; each agent clones into its own directory — no manual clone, no shared working tree. |
| **MarcusDevEnv plugin** | Kanboard plugin that adds AI-aware UI to every board and task — see [Feature reference](documents/features.md). |
| **Hot-reload dev environments** | One-click per-ticket preview URL, any language/framework, refreshing instantly on every `git push`. See [Hot-reload dev environments](documents/api-reference.md#hot-reload-dev-environments). |
| **Project Description system** | Per-project markdown doc (tech stack, architecture notes) AI agents read via `get_project_description`; Marcus infers it when missing. |
| **Human Gate / AI Gate toggle** | Per-project and per-ticket control over whether humans review AI work before it merges. |
| **AI Verify** | Configurable N-round LLM code review before any AI-gate merge. See [AI Verify](documents/features.md#ai-verify). |
| **Clone this project** | A **"📋 Clone this project"** button replicates a project's entire visible state — tickets, settings, description, and full git history — under a new name. See [Cloning a project](documents/features.md#cloning-a-project). |
| **Project stats** | A **"📊 Project Stats"** page tracking tickets/hour into Done and Waiting for Human, plus the repo's line count on `main`. See [Project stats](documents/features.md#project-stats). |
| **Claude subscription provider** | Marcus's own planner calls can run through a locally logged-in `claude` CLI instead of a metered API key. See [AI provider](documents/deployment-guide.md#ai-provider). |
| **Remote agents + auth** | Opt in during setup to let AI agents on other machines connect, gated by a bearer token, with optional built-in HTTPS. See [Authenticating remote agents](documents/deployment-guide.md#authenticating-remote-agents). |

### Built on

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

## Architecture

All three services run as containers on one `docker compose` network and reach each other by service name (`kanboard`, `gitea`, `marcus`) — only the host-side port mappings (8080, 3000, 4298) matter from outside Docker. AI agents never talk to Kanboard directly: they call Marcus's MCP tools, and Marcus alone holds the Kanboard API token — see [How AI agents reach tickets](documents/architecture.md#how-ai-agents-reach-tickets) for the full security model.

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

---

## Getting Started

### Prerequisites

- Docker Desktop (macOS/Linux) — **2 GB RAM** is plenty (Gitea is lightweight; no GitLab-sized allocation needed)
- `curl`, `python3`, `openssl` (all preinstalled on macOS/most Linux distros)
- Either a **Claude Pro/Max subscription** (run `claude login` on this machine once, beforehand — the setup script picks it up automatically, no API key) **or** a Claude API key from [console.anthropic.com](https://console.anthropic.com/) if you'd rather pay per token. See [AI provider](documents/deployment-guide.md#ai-provider).
- An MCP-compatible AI agent (Claude Code, Codex, etc.)

### 1. Run the setup script

```bash
./scripts/setup.sh
```

This one command asks how Marcus itself should run (Docker, or natively on this host), starts Kanboard and Gitea, creates the Kanboard project and its six required columns, sets tokens and webhooks, picks and wires up an AI provider, then starts Marcus itself — no second command needed. It's safe to re-run.

When it finishes it prints the Kanboard/Gitea/Marcus URLs, the Gitea admin password, which AI provider got selected, and the exact `claude mcp add` command for step 2 below.

For exactly what the script does under the hood, manual setup instructions, running Marcus natively instead of in Docker, and deploying each service independently, see [documents/deployment-guide.md](documents/deployment-guide.md).

### 2. Connect your AI agent

Point any MCP-compatible agent at `http://localhost:4298/mcp`. For Claude Code:

```bash
claude mcp add --transport http marcus http://localhost:4298/mcp
```

This always works from the same machine Marcus runs on. Connecting from a **different machine** additionally requires you to have opted in during setup — see [Network access](documents/deployment-guide.md#network-access).

Once connected, the simplest way to run an agent is **orchestrate mode** — prompt it with roughly:

> Start `n` agents. Each does the following: call the `marcus_work` tool with no arguments and do exactly what the returned `message` says. Every ~10 seconds call `marcus_work` again with the `agent_id`/`ticket_id` it gave you plus a one-line `report`. Report `DONE - <summary>` when finished.

Replace `n` with however many agents you want polling Marcus in parallel. Each one gets its own auto-generated worker id and naturally lands on a different ticket — see [Running multiple agents / multiple accounts](documents/deployment-guide.md#running-multiple-agents--multiple-accounts) for how that works, including using two separate Claude Pro accounts.

Marcus hands out the next human-readied ticket, posts a summarized progress comment on each report, and completes the ticket through the gate. Alternatively, point the agent at a specific ticket: it calls `get_work_context`, which returns a `clone_url` it uses to `git clone` the repo, then works on the pre-made branch. `prompts/Kanboard_Agent_Prompt.md` is the full agent operating manual.

**New Kanboard projects start disabled** — Marcus does nothing on a project until a human flips its "Marcus: OFF/ON" board-header toggle. See [Scoping Marcus to specific projects](documents/deployment-guide.md#scoping-marcus-to-specific-projects).

### Tearing down

```bash
./scripts/teardown.sh
```

Stops every container and a natively-run Marcus process, then prints every location that holds real data (`./data`, `./logs`, Docker's named volumes, `.env`) with rough sizes so you can decide what to delete yourself — **it doesn't delete anything on its own**. Re-running `./scripts/setup.sh` afterward picks up exactly where you left off.

---

## Limitations & Scale

> ⚠️ **This is a research/demo deployment, not a production system for large-scale use.** MKG was built to demonstrate the board-mediated multi-agent pattern (see the [Multi-Agency Proclamation](https://github.com/lwgray/marcus) in Marcus's own docs) end-to-end against real, self-hosted Kanboard and Gitea instances — not to run thousands of Kanboard projects or hundreds of concurrent agents. **The underlying concept — agents self-selecting work off a shared kanban board, coordinated by Marcus — does extend to that scale.** Getting there requires replacing several pieces below that were built for "one team, a handful of projects," not "an org-wide install."

The table below is the honest list of what actually breaks first, and why, ordered roughly by how soon it bites:

| Limitation | Why it doesn't scale | Where it lives |
|---|---|---|
| **Single Python process, one event loop** | Every project's polling, every agent's MCP request, and every board-watcher tick run in the same `asyncio` event loop in the same OS process (`asyncio.run(main())`). There's no worker pool, no sharding by project, no multiprocessing anywhere in `src/`. A slow LLM call, a stuck Docker command, or a large diff computation for one project can add latency to every other project's requests, and the whole deployment is capped by one machine's CPU/RAM. | `src/marcus_mcp/server.py` (`cli_main()` / `__main__`, `asyncio.run(main())`) |
| **Polling cost grows linearly with project/ticket count** | `BoardWatcher` (30s default) and the `ProjectWatcher` backstop (5 min default, `PROJECT_POLL_INTERVAL`) each do **one full pass over every enabled Kanboard project, every cycle** — not an incremental "what changed" query. Ten enabled projects means ten RPC round-trips per 30-second tick; a thousand means a thousand, all serialized through the one event loop above. Webhooks (instant) reduce how often this matters but don't remove the backstop cost. | `src/core/board_watcher.py` (`_run_poll_cycle`); `src/integrations/providers/kanboard_kanban.py` (`for pid in project_ids: ...`); `src/core/project_watcher.py` (`poll_once`) |
| **Settings stored as flat JSON files, rewritten whole on every change** | Per-project access settings (`data/project_access_settings.json`) and the known-projects list (`data/known_projects.json`) are each a single JSON file: any single `set_project_enabled()` call, or any single newly-discovered project, reads the **entire** file, updates one entry in memory, and writes the **entire** file back out. That's an O(n)-sized disk write for a 1-entry change, and it doesn't survive two Marcus instances writing concurrently (no locking, no transactions) — a non-starter once you have enough projects that these files are more than a few KB, or more than one Marcus process. | `src/core/project_access_settings.py` (`_load`/`_save`); `src/core/project_watcher.py` (`_load_known_ids`/`_save_known_ids`) |
| **Kanboard defaults to SQLite** | Both `docker-compose.yml` and `kanboard/docker-compose.yml` run Kanboard against its default SQLite database — no `DB_DRIVER=mysql`/`postgres` override is set anywhere in this repo. SQLite is single-writer: once enough tickets/comments are being written concurrently (many agents, many projects), Kanboard itself starts throwing `database is locked` errors — this is a limitation of Kanboard's own default config, not Marcus's code, but this repo doesn't change it. | `docker-compose.yml`, `kanboard/docker-compose.yml` (Kanboard's `DB_DRIVER` is left unset → SQLite) |
| **Decision/implementation history capped by a periodic sweep, not a real store** | Marcus's per-project `Context` (architectural decisions, past implementations — see `src/core/context.py`) now self-prunes via a background sweep (`sweep_context_retention`, `CONTEXT_RETENTION_DAYS`, default 30) instead of growing forever, and the exact-match half of its decision lookup is indexed by `task_id`. But it's still all **in one process's RAM**, gone on restart unless persistence is configured, and the other half of the lookup (matching a task id against the free-text `impact` field agents write) is still a linear scan over every decision in the project — bounded in growth by the sweep, not bounded in per-call cost. | `src/core/context.py` (`Context._decisions_by_task_id`, `sweep_context_retention`, `clear_old_data`) |
| **Decisions Log still makes one Kanboard round-trip per ticket** | Compiling a project's Decisions Log tab fetches every ticket's comments to find agent-flagged notes. Kanboard's JSON-RPC API has no bulk "comments for N tickets" call, so this is fundamentally N round-trips for N tickets — currently run with bounded concurrency (10 in flight at once) rather than sequentially, which helps wall-clock time but doesn't reduce the RPC count itself. A project with thousands of tickets means thousands of RPCs every time the tab (or its refresh) is opened. | `src/core/decision_notes.py` (`get_project_decision_notes`, `_MAX_CONCURRENT_COMMENT_FETCHES`) |
| **AI Verify silently truncates large diffs** | The LLM code-review step caps the diff it sends at 12,000 characters (`_MAX_DIFF_CHARS`) and appends a "truncated" note — there's no chunking, summarization, or multi-pass review of what got cut off. A large refactor ticket gets reviewed on its first ~12K characters of diff only; anything past that is invisible to the reviewer. | `src/ai/verification/ai_verifier.py` (`_MAX_DIFF_CHARS`) |

### What would actually need to change for very large projects (thousands of projects/tickets)

The pattern itself — a shared kanban board as the coordination substrate, agents pulling work via `request_next_task`/`marcus_work`, Marcus authoring the task graph and contracts rather than the code — has nothing in it that caps it at small scale. These are the concrete engineering changes that would need to happen first, roughly in the order you'd hit them:

1. **Move Kanboard off SQLite** — point `DB_DRIVER` at MySQL or PostgreSQL (Kanboard supports both natively) to remove the single-writer bottleneck before anything else, since every other fix still funnels through Kanboard's API.
2. **Replace the flat-JSON settings files with a real datastore** — per-project access settings and the known-projects list need row-level reads/writes (a proper database, even just SQLite-per-key via a real ORM/driver instead of whole-file JSON), so a single toggle doesn't cost an O(n) rewrite and two Marcus processes can write concurrently without clobbering each other.
3. **Split the single process into a worker pool sharded by project** — since agents already self-select work off the board (Invariant #1 in the [Multi-Agency Proclamation](https://github.com/lwgray/marcus)), multiple Marcus worker processes could each own a disjoint slice of projects and poll/serve only those, coordinating through the shared datastore from step 2 instead of shared in-process memory. This is the change with the biggest payoff and the biggest effort.
4. **Move from full-pass polling to incremental/webhook-first polling** — `BoardWatcher` and `ProjectWatcher` would need to track a per-project "last seen" cursor and only re-check projects that webhooks haven't already confirmed are current, instead of re-listing every enabled project on every tick.
5. **Persist `Context` outside process memory** — architectural decisions and implementation history need to live in the same real datastore as step 2, not one process's RAM, both so a restart doesn't lose them and so multiple worker processes (step 3) share one consistent history per project.
6. **Replace the flat diff-truncation cap in AI Verify with real chunking** — a map-reduce-style review (summarize diff hunks, then review summaries plus the highest-risk hunks in full) instead of a hard 12,000-character cutoff, so a large ticket's out-of-scope changes past the current cap aren't invisible to the reviewer.
7. **Give the Decisions Log a persistent index instead of a live per-request scan** — build the notes list incrementally as comments are posted (e.g. driven off the same webhook that already reaches Marcus on every ticket update) rather than re-fetching every ticket's comments on each page load.

---

## License

MIT — see [LICENSE](LICENSE).
