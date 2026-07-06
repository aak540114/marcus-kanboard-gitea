<p align="center">
  <img src="docs/assets/logo.png" alt="Marcus" width="140">
</p>

<h1 align="center">Marcus</h1>

<p align="center">
  <strong>Agents coordinate through shared state, not conversation.</strong>
</p>

<p align="center">
  <a href="#get-started">Quickstart</a> •
  <a href="https://marcus-ai.dev">Docs</a> •
  <a href="https://discord.gg/DZWTbXr4">Discord</a> •
  <a href="ROADMAP.md">Roadmap</a> •
  <a href="PROTOCOL.md">Protocol</a>
</p>

<p align="center">
  <a href="https://github.com/lwgray/marcus"><img src="https://img.shields.io/github/stars/lwgray/marcus?style=social" alt="GitHub Stars"></a>
  <a href="https://discord.gg/DZWTbXr4"><img
src="https://img.shields.io/discord/1409498120739487859?color=7289da&label=Discord&logo=discord&logoColor=white" alt="Discord"></a>
  <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="Python 3.11+">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Compatible-green" alt="MCP Compatible"></a>
  <a href="docs/source/getting-started/setup-local-llm.md"><img src="https://img.shields.io/badge/Contribute-Free%20with%20Ollama-ff6b35?logo=ollama&logoColor=white" alt="Contribute for free with Ollama"></a>
</p>

  ---

## What is Marcus?

Marcus is an open-source orchestration server for AI coding agents. You describe what
to build. Marcus breaks the work into tasks on a shared kanban board. Multiple agents
pull tasks independently, write the code, and coordinate through the board — never
through chat. You walk away; you come back to working software.

Any [MCP](https://modelcontextprotocol.io/)-compatible agent works: Claude Code,
Codex, Gemini CLI, Kimi, AutoGen, LangGraph, or a custom runtime.

---

## Why Marcus

Multi-agent AI is broken at scale. Every framework today coordinates agents through
**conversation** — group chats, message passing, chain-of-thought relays. This works
with 2–3 agents. At scale, it collapses:

- **Context degrades.** Each agent gets a growing wall of chat history. Signal drowns in noise.
- **Work duplicates.** Without shared state, agents don't know what others have done.
- **Failures cascade.** One agent crashes and the conversation context is gone. No recovery.
- **Adding agents adds chaos.** More agents = more messages = slower, less reliable coordination.

The fundamental mistake: treating coordination as a conversation problem. It's a
**state management** problem.

|                        | Group Chat Coordination     | Board-Mediated Coordination   |
|------------------------|-----------------------------|-------------------------------|
| **Used by**            | AutoGen, CrewAI, LangGraph  | **Marcus**                    |
| **Coordination**       | Conversation between agents | Shared board state            |
| **Context at scale**   | Degrades                    | Preserved per-task            |
| **Agent failure**      | Lost context, no recovery   | Resume from board state       |
| **Visibility**         | Chat logs                   | Full audit trail + dashboard  |
| **Add more agents**    | More chaos                  | More throughput               |
| **Enterprise ready**   | Limited governance          | Audit trails, accountability  |

Marcus doesn't compete on raw speed. It competes on **coordination quality,
observability, and enterprise readiness.**

Still — coordination quality shows up in the timing. The board-mediated
thesis is validated head-to-head in
[`marcus-mini`](https://github.com/lwgray/marcus-mini), our lean
reference implementation: against AutoGen `SelectorGroupChat` (chat-based)
and LangGraph supervisor-worker (orchestrator-based) on identical
workloads, the gap grows with project size — ~3× at 9 tasks, ~7× at 27 tasks.
Full numbers and reproduction steps:
[`experiments/topologies/`](https://github.com/lwgray/marcus-mini/tree/main/experiments/topologies).

> *The moment it clicks: I didn't have to manage any of this.*

[![Demo](docs/assets/frontpage.png)](https://youtu.be/Js8cdsFGbHk/)
---

## How It Works

Marcus uses a simple idea: **give agents a shared task board instead of making them
talk to each other.** We call this **board-mediated coordination** — a modern,
agent-native take on the classical
[blackboard pattern](https://en.wikipedia.org/wiki/Blackboard_(design_pattern))
(Hayes-Roth, 1985), applied to autonomous LLM agents coordinating over MCP.

<p align="center">
  <img src="docs/assets/tasks.png" width="720" alt="The anatomy of a pristine task — requirements, dependencies, artifacts">
</p>

Each task carries its own context — requirements, dependencies, artifacts from
prior tasks. When an agent picks up a task, it gets exactly the context it needs.
No chat history. No lost threads. No duplicate work.

When an agent fails, the task stays on the board with its progress. Another agent
picks it up and continues. **The board is the system of record.**

### Architecture

<p align="center">
  <img src="docs/assets/stack.png" width="720" alt="How Marcus fits into your stack — dependency ordering, artifact routing, lease isolation">
</p>

- **Agents are stateless.** All state lives on the board.
- **Tasks are the unit of coordination.** Each has context, dependencies, artifacts.
- **MCP is the interface.** Any MCP-compatible agent works with Marcus.
- **Observability is built in.** Every action is traceable through the board and Cato.

See [Architecture Docs](https://marcus-ai.dev) for deep dives.

---

## What Marcus does

The board-mediated model gives you four properties that conversation-based
frameworks can't offer:

- **Atomic work claiming.** A lease-based SQL transaction with a dependency check
  guarantees two agents can't grab the same task — no lock manager, no consensus
  protocol. A task in progress is held under a time-limited lease; if the agent
  dies, the lease expires and another agent picks it up automatically.
- **Async handoffs.** Agent A finishes a task and logs an artifact — a spec,
  schema, or design decision. Agent B, running independently, picks up a dependent
  task hours later and reads that artifact as context. Neither needs to know the
  other exists.
- **Agent blindness as a feature.** Because agents don't know about each other,
  you can add, remove, swap providers, or lose an agent mid-run freely. This is
  why Marcus can run Claude Code, Codex, and a custom runtime on the same project
  simultaneously.
- **Full observability.** Every task, artifact, decision, and agent action is
  persisted to the board. Replay what happened, audit who did what, measure
  coordination overhead, and debug why an agent went off-rails — without
  instrumenting anything. Cato gives you a live visualization; the board gives
  you the ground truth.

<p align="center">
  <img src="docs/assets/modes.png" width="720" alt="Universal agent support via MCP — Runner mode and Attach mode">
</p>

---

## Human-Gated Workflow

Marcus includes a **human-gated AI workflow** — a mode where humans keep final control over when AI starts and when work is accepted, without slowing down the automation.

### How it works

```
Human creates ticket       →  Marcus generates acceptance criteria (AI)
Human assigns + moves to   →  AI picks up the ticket, creates a branch,
  "Ready"                      starts coding (fully automated)
AI is done or stuck        →  AI moves ticket to "Waiting for Human"
Human reviews + approves   →  Human moves to "Done"
Marcus merges branch       →  Branch merged to main automatically
```

**Two conditions must both be true before AI starts:**
the ticket must have an assignee **and** its kanban column must be `Ready`.
Whichever arrives second triggers the work. This prevents accidental AI starts
on half-configured tickets.

### Ticket status model

| Status | Who sets it | Meaning |
|---|---|---|
| `todo` | System | Ticket created, not yet ready for AI |
| `ready` | Human | Human has assigned and greenlit the ticket |
| `in_progress` | AI | AI has claimed the ticket and is working |
| `waiting_for_human` | AI | AI finished or needs input — human's turn |
| `blocked` | AI | Dependency on another unfinished ticket |
| `done` | Human | Human accepted the work; triggers branch merge |

### What AI agents can signal (MCP tools)

| Tool | When to call it |
|---|---|
| `signal_ready_for_review` | AI finished implementation |
| `signal_waiting_for_human` | AI needs clarification or credentials |
| `signal_blocked` | Dependency ticket is unfinished |
| `post_ticket_progress` | Periodic progress update (0–100%) |
| `generate_acceptance_criteria` | Kick off AC generation for a ticket |
| `get_ticket_lifecycle_state` | Query current state + metadata |
| `get_pending_tickets` | List tickets in a given state |
| `start_ticket_dev_environment` | Spin up a hot-reload preview URL |
| `get_ticket_dev_environment_url` | Get the URL of a running dev environment |

### Marcus HTTP endpoints (Kanboard stack)

| Endpoint | Method | Purpose |
|---|---|---|
| `/mcp` | GET/POST | MCP protocol — all AI agent tooling |
| `/webhooks/kanboard` | POST | Receives Kanboard push webhooks; instant event delivery |
| `/dev-env/view?ticket_id=<id>` | GET | Starts dev environment and redirects browser to hot-reload URL |

---

## Get Started

**Prerequisites:**
- macOS or Linux (Windows users: install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install), then follow the Linux instructions)
- Python 3.11+
- `tmux` (`brew install tmux` on macOS, `sudo apt install tmux` on Ubuntu/Debian)
- An LLM provider (Anthropic, OpenAI, or [Ollama](https://ollama.ai))
- An MCP-compatible coding agent
  - **Runner mode** (one-command): [Claude Code](https://claude.ai/code) + `tmux`
  - **Attach mode** (any agent): Codex, Gemini CLI, Kimi, AutoGen, LangGraph, custom

### Step 1: Install

```bash
git clone https://github.com/lwgray/marcus.git
cd marcus
pip install -e .
cp -r skills/marcus ~/.claude/skills/marcus  # Claude Code + Runner mode only
```

### Step 2: Configure your LLM provider

```bash
cp .env.example .env
cp config_marcus.example.json config_marcus.json
```

Edit `.env` for your API key:

```bash
CLAUDE_API_KEY=sk-ant-api03-your-key-here
```

> Marcus reads `CLAUDE_API_KEY` (not `ANTHROPIC_API_KEY`) so it doesn't
> interfere with Claude Code's subscription auth.

| Provider  | Cost | Setup |
|-----------|------|-------|
| Anthropic | Paid | Set `CLAUDE_API_KEY` in `.env` — works out of the box |
| OpenAI    | Paid | Set `OPENAI_API_KEY` in `.env`, set `ai.provider` to `"openai"` in `config_marcus.json` |
| Ollama    | Free | Install [Ollama](https://ollama.ai), pull a model, set `ai.provider` to `"local"` |

> This is the LLM Marcus uses for task decomposition. Your coding agents use their own keys separately.

### Step 3: Start Marcus

```bash
./marcus start
```

See the board in your terminal at any time:

```bash
./marcus board               # snapshot — print once and exit
./marcus board --watch       # live view — refreshes every 2 s (Ctrl+C to stop)
./marcus board -w --interval 5   # live view with a 5-second refresh rate
./marcus board --project my-project   # filter to a specific project
./marcus board --list        # list all projects in the database
```

The live board (`--watch`) polls the SQLite database and re-renders in-place — you can watch tasks move from **Backlog → In Progress → Done** as agents work in real time.

### Step 4: Choose a visual dashboard (optional)

**Cato** — real-time visualization with a built-in kanban board. Watch every agent decision, every coordination event, every artifact lineage as it happens.

📹 **[Watch the Cato demo →](assets/demo/cato_demo.mp4)** *(One prompt, eight agents, zero chat — fully observable.)*

```bash
# In a sibling directory
git clone https://github.com/lwgray/cato.git
cd cato && pip install -e . && ./cato start
# Open http://localhost:5173
```

**Kanboard + GitLab** — fully self-hosted task management and git server with real-time push webhooks, per-ticket dev environments, and a "View Live Changes" button on every ticket. See [Local Kanboard + GitLab setup](#local-kanboard--gitlab-setup) below.

### Step 5: Your first project — Runner mode

```bash
mkdir ~/projects/my-todo-app
cd ~/projects/my-todo-app
claude --dangerously-skip-permissions
```

Then inside Claude Code, prompt:

```
/marcus Build a todo app with authentication using 3 agents
```

The `/marcus` skill registers the MCP server, injects the agent prompt, decomposes
the project, and spawns agents in tmux panes. You walk away, you come back to
working software.

<details>
<summary><strong>Using a different agent? Use Attach mode — same board, same coordination, you wire the agents yourself.</strong></summary>

Marcus is an MCP server at `http://localhost:4298/mcp`. Any agent that speaks MCP
can participate. Runner mode automates the wiring below; Attach mode gives you
manual control.

---

**Step 1 — Connect your agent to Marcus**

Point your agent's MCP configuration at the running Marcus server:

```
http://localhost:4298/mcp   (HTTP transport)
```

For Claude Code users without tmux, register from inside your project directory:

```bash
cd ~/projects/my-todo-app
claude mcp add --transport http marcus http://localhost:4298/mcp
```

For other runtimes, consult your agent's MCP docs for how to register an HTTP MCP server.

---

**Step 2 — Give your agent the system prompt**

`prompts/Agent_prompt.md` is the complete behavioral spec for a Marcus worker. It
tells your agent exactly how to call Marcus tools, manage the work loop, handle
context and artifacts, report blockers, and when to exit. **Without it your agent
won't know the protocol and will stall.**

Copy it into every project directory an agent will run in:

```bash
cp <marcus-dir>/prompts/Agent_prompt.md ~/projects/my-todo-app/CLAUDE.md
```

For non-Claude runtimes, paste the contents into your agent's system prompt instead.

---

**Step 3 — Bootstrap the board (one agent, once)**

One agent must call `create_project` to decompose the work and populate the board.
Do this before workers start:

Ask your agent to call the `create_project` MCP tool:

- `description` — what you want to build, in plain language
- `project_name` — a name for the board

Marcus returns a `project_id`, `recommended_agents` count, and the full task graph. When it returns, tasks are on the board and immediately available.

That same agent can then join the work loop as a worker.

---

**Step 4 — Start workers**

Each worker calls `register_agent` once at startup (name, role, skills), then enters
the work loop driven by `Agent_prompt.md`:

1. Call `request_next_task` — Marcus returns a task or a retry signal
2. If no task: wait `retry_after_seconds` and try again — **do not exit**; work may still arrive
3. If task received: call `get_task_context` to fetch artifacts from dependency tasks
4. Do the work
5. Call `log_decision` for any significant architectural choice
6. Call `log_artifact` for any file other agents will need (specs, schemas, design docs)
7. Call `report_task_progress` at 25%, 50%, 75%, 100%
8. Immediately call `request_next_task` again — do not wait

Marcus handles dependency ordering, lease isolation, and artifact routing.

> **Building a runner for another runtime (AutoGen, LangGraph, custom)?**
> See [PROTOCOL.md](PROTOCOL.md) for the machine-readable agent protocol spec.

</details>

<details>
<summary><strong>Running experiments at scale? Use Posidonius for multi-run management.</strong></summary>

[Posidonius](https://github.com/lwgray/posidonius) is the experiment dashboard
for launching and managing multi-agent runs across any CLI agent. It handles
spawning, experiment tracking, and provides a web UI for monitoring.

```bash
git clone https://github.com/lwgray/posidonius.git
cd posidonius && pip install -e .
```

See the [Posidonius README](https://github.com/lwgray/posidonius) for setup.
By default Posidonius writes projects to `~/experiments/`.

</details>

---

## Local Kanboard + GitLab Setup

Run a fully self-hosted stack on macOS (or Linux) to test the complete human-gated workflow: Kanboard for tickets, GitLab CE for git repositories, and Marcus running locally to wire them together.

> **RAM requirement:** GitLab CE needs at least 4 GB free RAM. In Docker Desktop → Settings → Resources, set memory to **6 GB or more** before starting.

### Start the stack

```bash
docker compose up -d
```

Kanboard is ready in ~5 seconds. GitLab takes **3–10 minutes on first boot** — wait for:

```bash
docker compose logs -f gitlab | grep "GitLab is ready to serve"
```

| Service | URL | Default credentials |
|---|---|---|
| Kanboard | http://localhost:8080 | `admin` / `admin` |
| GitLab CE | http://localhost:8929 | `root` / `Marcus123!` |

### First-time Kanboard setup

1. Log in at http://localhost:8080
2. **Settings → API** — copy the API token
3. **Settings → Integrations → Webhook URL** — paste this URL so Kanboard pushes changes to Marcus in real time instead of waiting for the next poll:
   ```
   http://host.docker.internal:4298/webhooks/kanboard
   ```
   *(Kanboard runs inside Docker; `host.docker.internal` is the hostname that reaches your Mac from inside a container.)*
4. Create a project (e.g. "My App")
5. Add columns named exactly: `Ready`, `In Progress`, `Waiting for Human`, `Blocked`, `Done`
   *(column names are case-insensitive in Marcus's mapping)*

**Optional — "View Live Changes" button:**
Install the MarcusDevEnv Kanboard plugin to add a sidebar button on every task that spins up a hot-reload dev environment with one click:

```bash
# Copy the plugin into the Kanboard container and restart
docker compose cp kanboard-plugins/MarcusDevEnv marcus-kanboard:/var/www/app/plugins/
docker compose restart kanboard
```

After restart, every task detail page shows a **View Live Changes** button. Clicking it calls Marcus's `/dev-env/view` endpoint, starts the dev environment for that ticket's branch, and redirects the browser to the hot-reload URL.

### First-time GitLab setup

1. Log in at http://localhost:8929
2. **Edit Profile → Access Tokens** — create a token with `api` + `write_repository` scopes
3. Copy the token

### Configure Marcus

```bash
export KANBAN_PROVIDER=kanboard
export KANBOARD_URL=http://localhost:8080/jsonrpc.php
export KANBOARD_API_TOKEN=<your-kanboard-token>
export KANBOARD_PROJECT_ID=1
export GITLAB_URL=http://localhost:8929
export GITLAB_TOKEN=<your-gitlab-pat>
export MARCUS_URL=http://localhost:4298       # used by the MarcusDevEnv plugin

# Optional: validate that webhook POSTs come from your Kanboard
# (set the same value in Kanboard → Settings → Integrations → Webhook Token)
export KANBOARD_WEBHOOK_TOKEN=<shared-secret>
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

### How the full flow works end-to-end

**Project creation → GitLab repo (automatic):**
1. Create a project in Kanboard
2. Within ~60 s, Marcus's `ProjectWatcher` detects it
3. GitLab repo auto-created (e.g. `http://localhost:8929/root/my-app`)
4. Local clone created at `./repos/my-app/`
5. Mapping saved to `./data/project_repos.json`

**Ticket → Branch → AI work:**
1. Create a task in Kanboard and assign it to yourself
2. Move the task to the `Ready` column
3. **Instantly** (webhook) or within ~30 s (poll fallback), Marcus detects both conditions
4. Branch `ticket/kanboard/<task_id>` created and pushed to GitLab
5. Kanboard column moves to `In Progress` automatically
6. Marcus posts a "Started" comment with the branch name on the task

**Viewing live changes (dev environment):**
- Click the **View Live Changes** button in the task sidebar (requires MarcusDevEnv plugin), **or**
- Open `http://localhost:4298/dev-env/view?ticket_id=<id>` directly in your browser
- Marcus starts a hot-reload dev environment for that ticket's branch and redirects you to it
- Each ticket gets its own port — you can test multiple tickets simultaneously

**Review and merge:**
7. AI signals completion → column moves to `Waiting for Human`
8. Human reviews the branch in GitLab and the live preview in the dev environment
9. Human moves Kanboard card to `Done`
10. **Instantly** (webhook) or within ~30 s (poll fallback), Marcus merges the branch to `main`

---

## Documentation

- [Configuration Reference](docs/source/developer/configuration.md) — all options
- [Agent Workflow Guide](docs/source/guides/agent-workflows/agent-workflow.md) — how agents interact
- [Development Workflow](docs/source/developer/development-workflow.md) — daily dev workflows
- [Local Development Setup](docs/source/developer/local-development.md) — first-time setup
- [Architecture Deep Dive](docs/source/architecture/) — the board pattern in detail
- [PROTOCOL.md](PROTOCOL.md) — agent protocol spec (build your own runner)
- [ROADMAP.md](ROADMAP.md) — where we're headed

---

## Community

- [**Discord**](https://discord.gg/DZWTbXr4) — real-time help, questions, and feedback. Report errors in `#bugs`
- [**GitHub Issues**](https://github.com/lwgray/marcus/issues) — detailed bug reports (use [bug report template](/.github/ISSUE_TEMPLATE/bug_report.md)) and feature requests
- [**GitHub Discussions**](https://github.com/lwgray/marcus/discussions) — ideas and long-form questions

**For researchers and educators:** Board-mediated coordination extends the blackboard pattern to autonomous LLM agents - a named, citable variant for multi-agent-over-MCP systems. Marcus is MIT-licensed — use it in courses,
papers, and experiments.  The pattern is documented in the [Architecture Docs](docs/source/architecture/).

Named after Marcus Aurelius. The Stoic philosophy runs deep: discipline,
transparency, and letting the system — not any single agent — hold the truth.

---

<details>
<summary><strong>Changelog &amp; milestones</strong></summary>

### Recent updates

| Date           | Update |
|----------------|--------|
| **2026-07-06** | Kanboard push webhooks for instant event delivery; `/dev-env/view` HTTP endpoint; MarcusDevEnv Kanboard plugin ("View Live Changes" sidebar button); removed Planka/Linear/GitHub/Jira providers |
| **2026-07-05** | Human-gated workflow, Kanboard provider, GitLab integration, ProjectWatcher, ProjectSyncWorkflow |
| **2026-04-26** | v0.3.6 — parallel experiment isolation, agent auto-termination, DONE-task board integrity guards |
| **2026-04-17** | v0.3.4 — `contract_first` default decomposer, `recommended_agents` in API response, `PROTOCOL.md` |
| **2026-04-16** | Presented Marcus and Cato at Machine Learning Ambassador Conference, John Deere Financial (Des Moines, IA) |
| **2026-04-03** | v0.3.0 — SQLite default provider, Epictetus evaluation, `/marcus` skill |
| **2026-03-21** | v0.2.1 — lease recovery, progressive timeouts, structured agent handoffs |
| **2026-03-16** | v0.2.0 — AI-powered validation, centralized config, 115 commits since v0.1.3.1 |
| **2025-10-20** | v0.1.3.1 — sweep-line parallelism algorithm, tmux multi-agent support |
| **2025-10-19** | Presented Marcus to "AI Assistants" Biweekly Group, Blue River Technology (Santa Clara, CA) |
| **2025-10-13** | v0.1.1 — initial release as "PM Agent", MCP protocol, Planka integration |
| **2025-06-15** | Project started as "PM Agent" — rebranded to Marcus in October 2025 |

### Version milestones

| Version    | Date       | Commits | Highlights |
|------------|------------|---------|------------|
| **dev**    | 2026-07-06 | —       | Kanboard push webhooks (`POST /webhooks/kanboard`) for instant event delivery; `/dev-env/view` HTTP endpoint; MarcusDevEnv Kanboard PHP plugin with "View Live Changes" sidebar button; removed Planka/Linear/GitHub/Jira providers; human-gated workflow; Kanboard JSON-RPC provider; GitLab CE integration; `ProjectWatcher`; `ProjectSyncWorkflow`; 6-state ticket lifecycle; 10 new MCP tools for AI agents |
| **v0.3.6** | 2026-04-26 | 28      | Parallel experiment isolation, agent auto-termination, DONE-task board integrity guards, Phase 4 lease tuning, spec-coverage ordering fix |
| **v0.3.4** | 2026-04-17 | —       | `contract_first` default decomposer, `recommended_agents` in `create_project` response, `PROTOCOL.md`, pre-fork synthesis, scope annotation |
| **v0.3.0** | 2026-04-03 | 59      | SQLite default provider, Epictetus evaluation, `/marcus` one-command experiments, resilience overhaul |
| **v0.2.1** | 2026-03-21 | 1       | Lease recovery, structured handoffs, configurable LLM temperature |
| **v0.2.0** | 2026-03-16 | 115     | AI-powered validation, centralized config, constraint propagation, MLflow tracking |
| **v0.1.3** | 2025-10-18 | 2       | Optimized project creation, subtask assignment fix |
| **v0.1.2** | 2025-10-16 | ~50     | CPM scheduling, unified dependency graphs, cross-parent wiring |
| **v0.1.1** | 2025-10-13 | ~80     | Initial release. Rebranded PM Agent → Marcus. MCP, Planka, NLP tools |

Full history in [CHANGELOG.md](CHANGELOG.md). What's next in [ROADMAP.md](ROADMAP.md).

</details>

---

## License

MIT — see [LICENSE](LICENSE).

<p align="center"><em>The board is the system.</em></p>
