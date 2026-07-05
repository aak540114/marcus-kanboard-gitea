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
    local repo path, GitLab URL, and step-by-step instructions.
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
    percentage = int(arguments.get("percentage", 0))
    message = arguments.get("message", "Work in progress.")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    wf = _workflow()
    if wf is None:
        return {"success": False, "error": "HumanGatedWorkflow not initialised"}

    try:
        posted = await wf.report_progress(ticket_id, percentage, message)
        return {
            "success": True,
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
        posted = await wf.signal_ready_for_review(ticket_id)
        lm = _lifecycle()
        state = (
            lm.get(ticket_id, provider).state.value
            if lm and lm.get(ticket_id, provider)
            else "unknown"
        )
        return {
            "success": True,
            "result": {
                "comment_posted": posted,
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
        posted = await wf.set_waiting_for_human(ticket_id, reason=reason)
        return {
            "success": True,
            "result": {
                "comment_posted": posted,
                "ticket_id": ticket_id,
                "new_state": "waiting_for_human",
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
        dev_env_port, merged_at, history}}`` or ``{success: False, error}``.
    """
    ticket_id = arguments.get("ticket_id", "")
    provider = arguments.get("provider", "")

    if not ticket_id or not provider:
        return {"success": False, "error": "ticket_id and provider are required"}

    lm = _lifecycle()
    if lm is None:
        return {"success": False, "error": "TicketLifecycleManager not available"}

    record = lm.get(ticket_id, provider)
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

        dev_info = wf._dev_env.get_info(ticket_id, provider)
        return {
            "success": True,
            "result": {
                "url": url,
                "port": dev_info.port if dev_info else None,
                "ticket_id": ticket_id,
                "provider": provider,
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

    dev_info = wf._dev_env.get_info(ticket_id, provider)
    return {
        "success": True,
        "result": {
            "running": dev_info is not None,
            "url": dev_info.url if dev_info else None,
            "port": dev_info.port if dev_info else None,
            "ticket_id": ticket_id,
            "provider": provider,
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
            ``state`` — Lifecycle state value (e.g. ``"in_progress"``,
            ``"revision_requested"``, ``"awaiting_acceptance"``).
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
    repository path, GitLab remote URL, and step-by-step instructions.

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
        acceptance_criteria, branch_name, local_repo_path, gitlab_repo_url,
        state, assignee, mcp_server_url, instructions}}``
        or ``{success: False, error}``.

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
          "local_repo_path": "./repos/my-app",
          "gitlab_repo_url": "http://localhost:8929/root/my-app.git",
          "state": "in_progress",
          "mcp_server_url": "http://localhost:4298/mcp",
          "instructions": "1. cd into local_repo_path ..."
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
