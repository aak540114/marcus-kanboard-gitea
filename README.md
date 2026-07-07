# Marcus × Kanboard

A production deployment of **[Marcus](https://github.com/lwgray/marcus)** — the board-mediated AI multi-agent orchestrator — wired to **Kanboard** for ticket management, **GitLab CE** for git repositories, and a custom Kanboard plugin that gives every board a live AI control panel.

> **What Marcus is:** see the [Marcus README](https://github.com/lwgray/marcus) and [docs](https://marcus-ai.dev). This repo is an opinionated deployment of it, not a fork.

---

## What this repo adds

| Feature | Description |
|---|---|
| **Kanboard provider** | Full Kanboard JSON-RPC integration — tickets, columns, comments, assignments |
| **GitLab CE integration** | Auto-creates a GitLab repo for each new Kanboard project; branches pushed per ticket |
| **MarcusDevEnv plugin** | Kanboard plugin that adds AI-aware UI to every board and task |
| **Hot-reload dev environments** | One-click per-ticket preview URL; supports any language/framework via project description |
| **Project Description system** | Per-project markdown doc that AI agents read to learn the tech stack; editable from the board |
| **Human Gate / AI Gate toggle** | Per-project and per-ticket control over whether humans review AI work before it merges |
| **AI Verify** | Optional second-LLM code review step that runs before any AI-gate merge; findings are posted as a comment and the worker agent must fix them before merging is allowed |

---

## Built on

| Tool | Role |
|---|---|
| [Marcus](https://github.com/lwgray/marcus) | AI multi-agent orchestrator (MCP server, board watcher, ticket lifecycle, agent coordination) |
| [Kanboard](https://kanboard.org) | Self-hosted kanban board — the shared task board all agents coordinate through |
| [GitLab CE](https://about.gitlab.com) | Self-hosted git — one repo per project, one branch per ticket |
| Python 3.11+ | Marcus server runtime |
| Docker / Docker Compose | Runs Kanboard and GitLab; dev containers for hot-reload previews |
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
| **AI Verify toggle** | Appears when AI Gate is active. When on, a second independent LLM reviews the branch diff against acceptance criteria before the merge is allowed. |

### Task sidebar
| Panel | What it does |
|---|---|
| **Marcus Dev Environment** | Start / Open / Stop a hot-reload preview for this ticket's branch. Any language — stack comes from the project description. |
| **Marcus Gate Mode** | Per-ticket gate override. Shows the project default; lets you switch this ticket to Human or AI gate independently. Ticket setting overrides project setting. Includes a per-ticket AI Verify override when AI Gate is active. |
| **Marcus Dependencies** | Dependency graph: *Depends on*, *Blocks*, *Related* — each with a colour-coded column-status badge. |

---

## Architecture

```
Human (browser + Kanboard)
  │  creates project                  │  creates ticket, assigns, sets "Ready"
  ▼                                   ▼
Kanboard (port 8080) ←──────────── Kanboard JSON-RPC API
  │  ProjectWatcher polls              │  BoardWatcher polls / webhook
  ▼  getAllProjects()                  ▼  getAllTasks()
Marcus (local, port 4298)        Marcus (local)
  │  GitLabManager                     │  BranchManager + HumanGatedWorkflow
  ▼  POST /api/v4/projects             ▼  git push branch
GitLab CE (port 8929)           GitLab CE — branch per ticket

AI agents (Claude Code, Codex, etc.)
  └── connect to http://localhost:4298/mcp  (MCP protocol)
      ├── request_next_task
      ├── signal_ready_for_review    → Human Gate: "Waiting for Human"
      │                              → AI Gate:    auto-merge + "Done"
      ├── signal_waiting_for_human   → Human Gate: pause for input
      │                              → AI Gate:    post note, continue
      └── post_ticket_progress
```

---

## Quick Start

### Prerequisites

- Docker Desktop (macOS/Linux) with **≥ 6 GB RAM** allocated (GitLab needs it)
- Python 3.11+
- An MCP-compatible AI agent (Claude Code, Codex, etc.)

### 1. Start the stack

```bash
docker compose up -d
```

Kanboard is ready in ~5 seconds. GitLab takes **3–10 minutes** on first boot:

```bash
docker compose logs -f gitlab | grep "GitLab is ready to serve"
```

| Service | URL | Default credentials |
|---|---|---|
| Kanboard | http://localhost:8080 | `admin` / `admin` |
| GitLab CE | http://localhost:8929 | `root` / `Marcus123!` |

### 2. First-time Kanboard setup

1. Log in at http://localhost:8080
2. **Settings → API** — copy the API token
3. **Settings → Integrations → Webhook URL** — set to:
   ```
   http://host.docker.internal:4298/webhooks/kanboard
   ```
4. Create a project and add columns: `Ready`, `In Progress`, `Waiting for Human`, `Blocked`, `Done`

### 3. First-time GitLab setup

1. Log in at http://localhost:8929
2. **Edit Profile → Access Tokens** — create a token with `api` + `write_repository` scopes

### 4. Configure Marcus

```bash
export KANBAN_PROVIDER=kanboard
export KANBOARD_URL=http://localhost:8080/jsonrpc.php
export KANBOARD_API_TOKEN=<your-kanboard-token>
export KANBOARD_PROJECT_ID=1
export GITLAB_URL=http://localhost:8929
export GITLAB_TOKEN=<your-gitlab-pat>
export MARCUS_URL=http://localhost:4298
```

Or set these in `config_marcus.json`:

```json
{
  "kanban_provider": "kanboard",
  "kanban": {
    "kanboard_url": "http://localhost:8080/jsonrpc.php",
    "kanboard_api_token": "YOUR_KANBOARD_TOKEN",
    "kanboard_project_id": 1
  },
  "gitlab_url": "http://localhost:8929",
  "gitlab_token": "YOUR_GITLAB_PAT"
}
```

### 5. Start Marcus

```bash
pip install -e .
python -m marcus --provider kanboard
```

### 6. Connect your AI agent

Point any MCP-compatible agent at `http://localhost:4298/mcp`. For Claude Code:

```bash
claude mcp add --transport http marcus http://localhost:4298/mcp
```

---

## Full ticket lifecycle

```
Human creates ticket in Kanboard
  → Marcus generates acceptance criteria (AI)

Human assigns ticket + moves to "Ready"
  → Marcus checks project description for tech stack
  → If stack missing: posts clarification comment, moves to "Waiting for Human"
  → If stack OK: creates branch in GitLab, moves to "In Progress"

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

  AI Gate (AI Verify ON):
    → A second LLM reviews the branch diff against acceptance criteria
    → PASS: branch auto-merges to main, ticket moves to "Done"
    → FAIL: findings posted as a "Marcus AI Verifier — Issues Found" comment
            ticket stays "In Progress"; worker agent fixes issues and
            calls signal_ready_for_review again → verification reruns
            (repeats until clean; LLM errors are fail-open: merge proceeds)
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
1. Set the project gate to **AI Gate** — the AI Verify toggle appears next to it.
2. Click the toggle to enable.

**Per-ticket override (task sidebar):**
1. Open a ticket. The **Marcus Gate Mode** panel shows the current effective verify state.
2. When the effective gate is AI, an **AI Verify** switch appears. Toggle it to override the project setting for this ticket only.

---

## HTTP endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | GET/POST | MCP protocol — all AI agent tooling |
| `/webhooks/kanboard` | POST | Receives Kanboard push webhooks |
| `/dev-env/view?ticket_id=<id>&project_id=<id>` | GET | Starts hot-reload dev environment, redirects to preview URL |
| `/dev-env/stop?ticket_id=<id>` | POST | Tears down a running dev environment |
| `/api/dev-env/status?ticket_id=<id>` | GET | Returns `{running, url}` for a ticket's dev environment |
| `/api/active-agents` | GET | All tickets currently claimed by an AI agent |
| `/api/ticket-links?ticket_id=<id>` | GET | Dependency graph split into `depends_on`, `blocks`, `relates_to` |
| `/project-description?project_id=<id>` | GET | Editable project description page |
| `/api/project-description?project_id=<id>` | GET/PUT | Project description plain-text API |
| `/api/gate-setting?project_id=<id>[&ticket_id=<id>]` | GET | Current gate + verify settings; returns `project_gate`, `ticket_gate`, `effective`, `project_verify`, `ticket_verify`, `effective_verify` |
| `/api/gate-setting/project` | PUT | Set project-level gate (`human`\|`ai`) and/or verify (`true`\|`false`) |
| `/api/gate-setting/ticket` | PUT | Set per-ticket gate override (`human`\|`ai`\|`null`) and/or verify (`true`\|`false`\|`null`) |

---

## Independent deployment

Each service deploys independently:

| Service | Compose file | Suggested platform |
|---|---|---|
| Local all-in-one | `docker-compose.yml` (root) | macOS / Linux laptop |
| Kanboard only | `kanboard/docker-compose.yml` | Railway, Fly.io, any VPS |
| GitLab only | `gitlab/docker-compose.yml` | Dedicated VPS (≥ 4 GB RAM) |
| Marcus | runs locally, no Docker | Your laptop, a cloud VM, or CI |

**Railway (Kanboard):** push to GitHub, create a Railway service pointing at `kanboard/`, set environment variables in the Railway dashboard. Railway reads `kanboard/railway.toml` automatically.

---

## License

MIT — see [LICENSE](LICENSE).
