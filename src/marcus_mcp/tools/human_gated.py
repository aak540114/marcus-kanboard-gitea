"""
MCP tool definitions for the human-gated AI workflow.

These tools let AI agents interact with the human-gated ticket lifecycle:
generate acceptance criteria, report progress, signal completion, start
dev environments, and read incoming human feedback.

All tools follow the Marcus MCP tool convention — they return a dict
with ``success`` (bool) and either ``result`` (success) or ``error``
(failure) keys.

Tool list
---------
``get_work_context``
    **Start here.** Returns everything a new AI agent needs to begin work
    on a ticket: title, description, acceptance criteria, branch name,
    local repo path, Gitea URL, and step-by-step instructions.
``generate_acceptance_criteria``
    Generate an AC checklist for a ticket and post it.
``post_ticket_progress``
    Post a progress update comment (percentage + message).
``signal_ready_for_review``
    Declare the AI agent's work done; moves ticket to WAITING_FOR_HUMAN
    and sets kanban column to "waiting for human".
``signal_waiting_for_human``
    Signal that the AI needs external human input; moves ticket to
    WAITING_FOR_HUMAN without declaring implementation complete.
``signal_blocked``
    Signal that the ticket is blocked by an unresolved dependency; moves
    kanban column to "blocked".
``get_ticket_lifecycle_state``
    Return the current lifecycle state and metadata for a ticket.
``start_ticket_dev_environment``
    Spin up a hot-reload dev environment for the ticket branch.
``get_ticket_dev_environment_url``
    Return the URL of the running dev environment, if any.
``get_pending_tickets``
    Return tickets in a given lifecycle state.
``get_project_description``
    Return the project-wide tech-stack/context document for a ticket's
    project — the same document a human edits at ``/project-description``.
``update_project_description``
    Update that document from what the agent learned (skipped if a human
    has already edited it — their version wins).
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Module-level singleton — set by HumanGatedWorkflow.register() at startup.
_WORKFLOW_INSTANCE: Optional[Any] = None


def register_workflow(workflow: Any) -> None:
    """Register the active HumanGatedWorkflow so MCP tools can reach it.

    Parameters
    ----------
    workflow : HumanGatedWorkflow
        The running workflow instance.
    """
    global _WORKFLOW_INSTANCE  # noqa: PLW0603
    _WORKFLOW_INSTANCE = workflow


def _workflow() -> Optional[Any]:
    """Return the current HumanGatedWorkflow singleton, or None."""
    return _WORKFLOW_INSTANCE


def _lifecycle() -> Optional[Any]:
    """Return the TicketLifecycleManager from the active workflow, or None."""
    wf = _WORKFLOW_INSTANCE
    return getattr(wf, "_lifecycle", None) if wf else None


def _effective_provider(caller_provider: str) -> str:
    """Return the provider records are actually keyed under.

    The workflow always stores lifecycle records and dev environments under
    its OWN configured provider (``wf._provider``), and the write tools
    already ignore the caller-supplied ``provider`` for that reason. The read
    tools must use the same key or they report ``not_tracked`` /
    ``running: false`` for tickets that are actually live. Falls back to the
    caller's value only when no workflow is registered.

    Parameters
    ----------
    caller_provider : str
        The ``provider`` argument the agent passed.

    Returns
    -------
    str
        The workflow's provider if available, else *caller_provider*.
    """
    wf = _WORKFLOW_INSTANCE
    return getattr(wf, "_provider", caller_provider) if wf else caller_provider


async def generate_acceptance_criteria(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate and post acceptance criteria for a ticket.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.
            ``title`` — Ticket title / summary.
        Optional:
            ``description`` — Ticket body / description.
            ``labels`` — List of label strings.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {ac_markdown, comment_posted}}`` or
        ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")
    title = arguments.get("title", ticket_id)
    description = arguments.get("description", "")
    labels = arguments.get("labels", [])

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        from src.core.acceptance_criteria import ACGenerator

        ac_gen = getattr(wf, "_ac_gen", ACGenerator())
        ac_markdown = await ac_gen.generate(
            title=title, description=description, labels=labels
        )

        from src.core.comment_protocol import CommentFormatter

        comment = CommentFormatter.ac_generated(
            ticket_id=ticket_id,
            ac_markdown=ac_markdown,
            was_human_created=True,
        )
        posted = await wf._post_comment(ticket_id, comment)

        return {
            "success": True,
            "result": {
                "ac_markdown": ac_markdown,
                "comment_posted": posted,
                "ticket_id": ticket_id,
                "provider": provider,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("generate_acceptance_criteria failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def post_ticket_progress(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Post a progress update comment on a ticket.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.
            ``percentage`` — Completion percentage (0–100).
            ``message`` — Progress description.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {comment_posted}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")
    message = arguments.get("message", "Work in progress.")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    # Coerce + clamp defensively: agents pass "50%", "about half", or null.
    # Doing this outside the try (as before) let a ValueError escape into the
    # generic handler, breaking the {success, result|error} contract; a
    # missing clamp let "250" reach the human-facing ticket verbatim.
    try:
        percentage = int(arguments.get("percentage", 0))
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "percentage must be an integer between 0 and 100",
        }
    percentage = max(0, min(100, percentage))

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        posted = await wf.report_progress(ticket_id, percentage, message)
        return {
            "success": bool(posted),
            "result": {
                "comment_posted": posted,
                "ticket_id": ticket_id,
                "percentage": percentage,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("post_ticket_progress failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def signal_ready_for_review(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Signal that the AI agent is done and the ticket is ready for human review.

    This transitions the ticket to WAITING_FOR_HUMAN, moves the kanban
    column to "waiting for human", and posts a "Ready for Review" comment
    with the branch name, AC checklist, and (optionally) a hot-reload dev
    environment URL.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {new_state, comment_posted}}`` or
        ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        # `ok` reflects whether the transition actually happened. Reporting a
        # hardcoded success:True hid real failures (Kanboard outage, or a
        # duplicate call on an already-reviewed ticket) from the agent, which
        # then never retried and left the ticket stuck IN_PROGRESS.
        ok = await wf.signal_ready_for_review(ticket_id)
        prov = _effective_provider(provider)
        lm = _lifecycle()
        state = (
            lm.get(ticket_id, prov).state.value
            if lm and lm.get(ticket_id, prov)
            else "unknown"
        )
        return {
            "success": bool(ok),
            "result": {
                "comment_posted": ok,
                "new_state": state,
                "ticket_id": ticket_id,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("signal_ready_for_review failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def signal_waiting_for_human(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Signal that the AI agent needs external human input to continue.

    Moves the ticket to WAITING_FOR_HUMAN and sets the kanban column to
    ``waiting for human``.  Use this when the AI is stuck on something it
    cannot resolve on its own (e.g. unclear requirement, missing credentials)
    rather than having finished the implementation.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.
        Optional:
            ``reason`` — Human-readable description of what input is needed.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {comment_posted}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")
    reason = arguments.get("reason", "AI agent requires human input to continue.")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        # set_waiting_for_human returns False when the ticket is not
        # IN_PROGRESS (or the comment post failed). Propagate that instead of
        # claiming success and a "waiting_for_human" state that never happened.
        ok = await wf.set_waiting_for_human(ticket_id, reason=reason)
        prov = _effective_provider(provider)
        lm = _lifecycle()
        rec = lm.get(ticket_id, prov) if lm else None
        return {
            "success": bool(ok),
            "result": {
                "comment_posted": ok,
                "ticket_id": ticket_id,
                "new_state": rec.state.value if rec else "unknown",
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("signal_waiting_for_human failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def signal_blocked(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Signal that the ticket is blocked by an unresolved dependency.

    Moves the ticket to BLOCKED and sets the kanban column to ``blocked``.
    The AI agent should call this when it discovers that another ticket or
    external resource must be completed first.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.
            ``blocked_by`` — Description of the blocking dependency.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {new_state}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")
    blocked_by = arguments.get("blocked_by", "unspecified dependency")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        ok = await wf.set_blocked(ticket_id, blocked_by=blocked_by)
        return {
            "success": ok,
            "result": {
                "ticket_id": ticket_id,
                "new_state": "blocked",
                "blocked_by": blocked_by,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("signal_blocked failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def get_ticket_lifecycle_state(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the current lifecycle state and metadata for a ticket.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {state, branch_name, assignee, ac_hash,
        dev_env_port, merged_at, created_at, updated_at, history_count,
        last_transition}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    lm = _lifecycle()
    if lm is None:
        return {"success": False, "error": "TicketLifecycleManager not available"}

    record = lm.get(ticket_id, _effective_provider(provider))
    if record is None:
        return {
            "success": True,
            "result": {
                "ticket_id": ticket_id,
                "provider": provider,
                "state": "not_tracked",
                "message": "Ticket is not being tracked by Marcus lifecycle manager",
            },
        }

    return {
        "success": True,
        "result": {
            "ticket_id": ticket_id,
            "provider": provider,
            "state": record.state.value,
            "branch_name": record.branch_name,
            "assignee": record.assignee,
            "ac_hash": record.ac_hash,
            "dev_env_port": record.dev_env_port,
            "merged_at": record.merged_at.isoformat() if record.merged_at else None,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "history_count": len(record.history),
            "last_transition": record.history[-1] if record.history else None,
        },
    }


async def start_ticket_dev_environment(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Start a hot-reload dev environment for a ticket's branch.

    The environment runs the application code from the ticket branch
    with live reloading.  A comment with the URL is posted to the ticket.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {url, port}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        url = await wf.start_dev_environment(ticket_id)
        if url is None:
            return {"success": False, "error": "Failed to start dev environment"}

        # start_dev_environment() always registers the environment under
        # the workflow's OWN configured provider (self._provider), not
        # whatever the caller passed here — look it up the same way, or a
        # caller-supplied `provider` that doesn't match silently returns
        # port: None for an environment that actually started fine.
        dev_info = wf._dev_env.get_info(ticket_id, wf._provider)
        return {
            "success": True,
            "result": {
                "url": url,
                "port": dev_info.port if dev_info else None,
                "ticket_id": ticket_id,
                "provider": wf._provider,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("start_ticket_dev_environment failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def get_ticket_dev_environment_url(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the URL of a ticket's running hot-reload dev environment.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier.
            ``provider`` — Kanban provider name.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {url, port, running}}`` or
        ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    # Environments are registered under the workflow's own provider, so look
    # them up the same way start_ticket_dev_environment stores them — else a
    # live environment reads back as running: false.
    prov = _effective_provider(provider)
    dev_info = wf._dev_env.get_info(ticket_id, prov)
    return {
        "success": True,
        "result": {
            "running": dev_info is not None,
            "url": dev_info.url if dev_info else None,
            "port": dev_info.port if dev_info else None,
            "ticket_id": ticket_id,
            "provider": prov,
        },
    }


async def get_pending_tickets(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return all tickets in a given lifecycle state.

    Useful for an AI agent to discover which tickets it needs to work on.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``state`` — Lifecycle state value: one of ``"todo"``,
            ``"ready"``, ``"in_progress"``, ``"waiting_for_human"``,
            ``"blocked"``, ``"done"``, ``"reopened"``.
        Optional:
            ``provider`` — Filter to a specific provider.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {tickets: [{ticket_id, provider, branch_name,
        assignee, state, updated_at}]}}`` or ``{success: False, error}``.
    """
    state_str = arguments.get("state", "")
    provider_filter = arguments.get("provider")

    if not state_str:
        return {"success": False, "error": "state is required"}

    lm = _lifecycle()
    if lm is None:
        return {"success": False, "error": "TicketLifecycleManager not available"}

    try:
        from src.core.ticket_lifecycle import TicketState

        target_state = TicketState(state_str)
    except ValueError:
        valid = [s.value for s in TicketState]
        return {
            "success": False,
            "error": f"Unknown state {state_str!r}. Valid values: {valid}",
        }

    records = lm.in_state(target_state)
    if provider_filter:
        records = [r for r in records if r.provider == provider_filter]

    return {
        "success": True,
        "result": {
            "state": state_str,
            "count": len(records),
            "tickets": [
                {
                    "ticket_id": r.ticket_id,
                    "provider": r.provider,
                    "branch_name": r.branch_name,
                    "assignee": r.assignee,
                    "state": r.state.value,
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in records
            ],
        },
    }


async def get_work_context(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return everything a new AI agent needs to start working on a ticket.

    This is the **first tool** any new AI agent should call after connecting
    to the Marcus MCP server.  A single call returns the full work context:
    ticket title, description, acceptance criteria, git branch name, local
    repository path, Gitea remote URL, and step-by-step instructions.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Ticket identifier (Kanboard task ID).
            ``provider``  — Kanban provider name (e.g. ``"kanboard"``).

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {ticket_id, provider, title, description,
        acceptance_criteria, branch_name, clone_url, repo_web_url,
        branch_web_url, local_repo_path, gitea_repo_url, state, assignee,
        labels, links, recent_comments, mcp_server_url, gate_mode,
        instructions}}`` or ``{success: False, error}``. ``links`` is
        ``{depends_on, blocks, relates_to}`` (each a list of
        ``{task_id, title, column}``); ``recent_comments`` is the last 10
        comments on the ticket, oldest first, each
        ``{content, author, date}`` — the only place a human's reply after
        ``signal_waiting_for_human`` is visible to the agent.

    Example
    -------
    An agent starting fresh calls::

        get_work_context({"ticket_id": "42", "provider": "kanboard"})

    and receives::

        {
          "ticket_id": "42",
          "title": "Add checkout button",
          "description": "Users need a checkout button ...",
          "acceptance_criteria": "- [ ] Button visible on cart page\\n- [ ] ...",
          "branch_name": "ticket/kanboard/42",
          "clone_url": "http://root:<token>@localhost:3000/root/my-app.git",
          "branch_web_url": "http://localhost:3000/root/my-app/src/branch/ticket/kanboard/42",
          "state": "in_progress",
          "labels": ["frontend"],
          "links": {"depends_on": [], "blocks": [], "relates_to": []},
          "recent_comments": [],
          "gate_mode": "human",
          "instructions": "1. git clone <clone_url> into your own dir ..."
        }
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        context = await wf.get_work_context(ticket_id)
        if context is None:
            return {
                "success": False,
                "error": (
                    f"Ticket {ticket_id!r} is not tracked by Marcus. "
                    "Ensure the ticket exists in Kanboard and has been seen "
                    "by the BoardWatcher at least once."
                ),
            }
        return {"success": True, "result": context}
    except Exception as exc:  # noqa: BLE001
        logger.error("get_work_context failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def get_project_description(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the project-wide tech-stack/context document for a ticket.

    Every Kanboard project has one project description — a markdown
    document covering language, framework, dev-server command, and
    architecture notes that apply to every ticket in it. Call this if
    ``get_work_context``'s per-ticket fields aren't enough context to
    start, e.g. you need the install/dev-server command or broader
    architecture notes.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Any ticket in the project (used to resolve
            which project's description to return).
            ``provider``  — Kanban provider name (e.g. ``"kanboard"``).

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {project_id, description, stack}}`` where
        ``stack`` is ``{language, framework, install_cmd, dev_cmd}`` or
        ``None`` if the description doesn't have enough structure to
        parse yet. ``{success: False, error}`` if the ticket/project
        can't be resolved.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        result = await wf.get_project_description(ticket_id)
        if result is None:
            return {
                "success": False,
                "error": (
                    f"Could not resolve a project for ticket {ticket_id!r} — "
                    "ensure the ticket exists in Kanboard and has been seen "
                    "by the BoardWatcher at least once."
                ),
            }
        return {"success": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("get_project_description failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def marcus_work(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Orchestration entry point — the ONLY tool a worker needs to call.

    Marcus is the manager; the caller is a worker. Call this with no
    arguments to get your first task, then call it again every ~10 seconds
    with the ``agent_id`` and ``ticket_id`` Marcus returned plus a short
    ``report`` of what you just did. Marcus assigns work, summarizes each
    report onto the ticket, guides you, and completes the ticket when you
    report ``DONE``.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Optional:
            ``agent_id`` — your worker id (echo back the one Marcus gave you).
            ``ticket_id`` — the ticket you're on (echo it back).
            ``report`` — one line on what you just did; prefix ``DONE`` when
            all acceptance criteria are met, or ``BLOCKED`` if stuck.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {status, agent_id, ticket_id?, context?,
        message}}``. Always read and follow ``message``.
    """
    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}
    try:
        result = await wf.orchestrate_work(
            agent_id=arguments.get("agent_id"),
            report=arguments.get("report"),
            ticket_id=arguments.get("ticket_id"),
        )
        return {"success": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("marcus_work failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def update_project_description(
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Update a project's description from what the agent learned.

    Lets a coding agent correct or enrich the project-wide tech-stack/context
    document (same markdown structure ``get_project_description`` returns).
    Marcus refuses the write if a human has already edited the description —
    the human's version is authoritative.

    Parameters
    ----------
    arguments : Dict[str, Any]
        Required:
            ``ticket_id`` — Any ticket in the project.
            ``provider``  — Kanban provider name (e.g. ``"kanboard"``).
            ``description`` — Full markdown description to store.

    Returns
    -------
    Dict[str, Any]
        ``{success, result: {updated, project_id, reason?}}`` or
        ``{success: False, error}``. ``updated`` is ``False`` (with a
        ``reason``) when a human edit blocks the overwrite.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")
    description = arguments.get("description", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}
    if not description or not str(description).strip():
        return {"success": False, "error": "description must be non-empty"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        result = await wf.apply_agent_project_description(
            ticket_id, str(description)
        )
        # success reflects whether the write actually happened (a human lock
        # or unresolved project makes updated=False).
        return {"success": bool(result.get("updated")), "result": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("update_project_description failed: %s", exc)
        return {"success": False, "error": str(exc)}
