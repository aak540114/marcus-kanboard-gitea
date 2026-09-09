# Feature Reference

Detailed reference for MKG's user-facing features: the Kanboard plugin's UI, the full ticket lifecycle, AI Verify, project cloning, and project stats. See the [README](../README.md) for the one-line summary of each in **Core Components**.

---

## MarcusDevEnv Kanboard Plugin

The plugin ships in `kanboard/plugins/MarcusDevEnv/` and is automatically active in all supported deployment paths. It adds these panels to every board and task:

### Board header
| Widget | What it does |
|---|---|
| **Marcus ON/OFF toggle** | The master switch for this project — see [Scoping Marcus to specific projects](deployment-guide.md#scoping-marcus-to-specific-projects). Off by default; nothing else in this table does anything until you turn it on. |
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
| 🔓 **Marcus: ON** | Marcus is enabled for this project — see [Scoping Marcus to specific projects](deployment-guide.md#scoping-marcus-to-specific-projects). |
| 🔒 **Marcus: OFF** | Marcus is not enabled for this project. Open its board to turn it on. |
| ⚠ **Marcus: unknown** | The badge couldn't reach Marcus to check (e.g. Marcus is down). Not the same as OFF. |

This means you can tell which of your projects Marcus is working without opening each one individually. Each badge starts as "⏳ Marcus" (checking) and resolves a moment later: the page collects every listed project's id, then makes one `GET /api/project-enabled?project_id=<id>` call per project (the same endpoint the board-header ON/OFF toggle uses) and fills in the badge from the response.

---

## Full ticket lifecycle

> Everything below assumes the ticket's project has been [enabled for Marcus](deployment-guide.md#scoping-marcus-to-specific-projects) — a new project starts disabled, and none of this happens until you flip that toggle.

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

Triggered via `POST /api/clone-project` (`{"baseline_project_id": int, "new_name": str}` → `{"job_id": str}`) and polled via `GET /api/clone-project-status?job_id=<id>` — see [HTTP endpoints](api-reference.md#http-endpoints).

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

Backed by `GET /api/project-stats?project_id=<id>` (see [HTTP endpoints](api-reference.md#http-endpoints)) and `src/core/project_stats.py`.
