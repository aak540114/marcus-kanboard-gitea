# Architecture

Service topology and the security model behind how AI agents reach tickets. See the [README](../README.md) for the condensed diagram and a one-paragraph summary.

---

## Service topology

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

---

## How AI agents reach tickets

AI agents never call Kanboard's JSON-RPC API and never receive Kanboard's API token. They call Marcus's MCP tools — in orchestrate mode just **`marcus_work`** (Marcus assigns, guides, and summarizes), or the individual tools (`get_work_context`, `get_project_description`, `post_ticket_progress`, `signal_ready_for_review`, …); Marcus alone holds `KANBOARD_API_TOKEN` and is the only thing that makes JSON-RPC calls to Kanboard, over the internal Docker network (`http://kanboard/jsonrpc.php`, not the host-published `:8080`). No tool response ever contains a Kanboard URL or credential. This is why gating Marcus's HTTP endpoint with a bearer token (see [Authenticating remote agents](deployment-guide.md#authenticating-remote-agents)) is sufficient to control ticket access: it's the *only* door.

`get_work_context` — the first call every agent makes — returns everything Marcus knows about a ticket: title, description, acceptance criteria, a ready-to-run `clone_url` (plus `repo_web_url`/`branch_web_url`), branch name, labels, dependency links (`depends_on`/`blocks`/`relates_to`), and its last 10 comments (see `prompts/Kanboard_Agent_Prompt.md` for the full field reference). `get_project_description` returns the project-wide tech stack and architecture notes when per-ticket context isn't enough.

Agents do talk to **Gitea** directly, but only to `git clone` the `clone_url` into their own directory and `fetch`/`push` on the one branch Marcus created for them — a different, narrower surface than the board itself. They never share Marcus's own clone, so parallel agents don't collide.
