"""System Health and Diagnostics Tools for Marcus MCP.

This module contains tools for system monitoring and health checks:
- ping: Check Marcus connectivity and status
- check_assignment_health: Monitor assignment system health
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src.logging.agent_events import log_agent_event
from src.logging.conversation_logger import conversation_logger, log_thinking
from src.monitoring.assignment_monitor import AssignmentHealthChecker


#: client_type values (see auth.ROLE_TOOLS) allowed to invoke ping's
#: destructive "cleanup"/"reset" commands. "ping" itself is deliberately
#: in every role's tool list — including DEFAULT_TOOLS for unregistered
#: clients and the "observer" role, which authenticate()'s own docstring
#: defines as read-only — so the tool-name-only access-control gate in
#: handlers.handle_tool_call cannot distinguish ping(echo="pong") from
#: ping(echo="reset"). This module-level check closes that gap.
_PING_MUTATION_ALLOWED_CLIENT_TYPES = {"agent", "developer", "admin"}


def _ping_client_may_mutate(state: Any) -> bool:
    """Whether the currently-calling client may run ping's mutating commands.

    Unregistered clients (using DEFAULT_TOOLS) and clients registered as
    "observer" are read-only by design and must be rejected; only
    "agent"/"developer"/"admin" client types may proceed.
    """
    client_id = getattr(state, "_current_client_id", None)
    registered = getattr(state, "_registered_clients", None)
    if not client_id or not registered:
        return False
    client_info = registered.get(client_id) or {}
    return client_info.get("client_type") in _PING_MUTATION_ALLOWED_CLIENT_TYPES


async def ping(echo: str, state: Any, confirm: bool = False) -> Dict[str, Any]:
    """
    Check Marcus status and connectivity with enhanced health diagnostics.

    Extended health check endpoint that verifies the Marcus system
    is online and responsive. Can echo back a message and provide
    detailed system health information.

    Special echo commands:
    - "health": Return detailed health information
    - "cleanup": Force cleanup of stuck task assignments (requires a
      developer/agent/admin client)
    - "reset": Clear all pending assignments (requires a
      developer/agent/admin client AND confirm=True)

    Parameters
    ----------
    echo : str
        Optional message to echo back or special command
    state : Any
        Marcus server state instance
    confirm : bool
        Must be True to actually perform "reset" — mirrors
        remove_project's confirm=True pattern. Without it, "reset"
        returns a dry-run response describing what would be cleared
        and makes no changes.

    Returns
    -------
    Dict[str, Any]
        Dict with status, provider info, timestamp, and optional health data
    """
    # Easter egg: ping(echo='quokka') → quokka greeting (before any logging).
    if echo and echo.strip().lower() == "quokka":
        from src.marcus_mcp.tools.easter_eggs import easter_egg_quokka_ping

        response = easter_egg_quokka_ping()
        state.log_event("ping_response", {"echo": echo, "easter_egg": "quokka"})
        return response

    # Determine client type from echo
    client_type = "unknown"
    if echo:
        echo_lower = echo.lower()
        if "seneca" in echo_lower:
            client_type = "seneca"
        elif "claude" in echo_lower or "desktop" in echo_lower:
            client_type = "claude_desktop"

    # Log the ping request with client identification
    state.log_event(
        "ping_request",
        {
            "echo": echo,
            "source": "mcp_client",
            "client_type": client_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Log conversation event for visualization
    log_agent_event(
        "ping_request",
        {"echo": echo, "source": "mcp_client", "client_type": client_type},
    )

    # Log thinking with client identification
    log_thinking("marcus", f"Received ping from {client_type} client with echo: {echo}")

    # Base response
    response = {
        "success": True,
        "status": "online",
        "provider": state.provider,
        "echo": echo or "pong",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "client_type_detected": client_type,
        "server_info": {
            "instance_id": getattr(state, "instance_id", "unknown"),
            "log_dir": (
                str(Path(state.realtime_log.name).parent)
                if hasattr(state, "realtime_log")
                and hasattr(state.realtime_log, "name")
                else None
            ),
        },
    }

    # Handle special commands
    if echo:
        echo_lower = echo.lower().strip()

        if echo_lower == "health":
            # Add detailed health information
            response["health"] = {
                "tasks_being_assigned": list(state.tasks_being_assigned),
                "active_agents": len(state.agent_status),
                "assigned_tasks": len(state.agent_tasks),
                "shutdown_pending": getattr(state, "_shutdown_event", None)
                and state._shutdown_event.is_set(),
                "active_operations": len(getattr(state, "_active_operations", set())),
            }

            # Add assignment health check
            try:
                health_checker = AssignmentHealthChecker(
                    state.assignment_persistence,
                    state.kanban_client,
                    state.assignment_monitor,
                )
                assignment_health = await health_checker.check_assignment_health()
                response["health"]["assignment_system"] = assignment_health
            except Exception as e:
                response["health"]["assignment_system"] = {"error": str(e)}

            # Add lease statistics if lease manager is available
            try:
                if hasattr(state, "lease_manager") and state.lease_manager:
                    lease_stats = state.lease_manager.get_lease_statistics()
                    response["health"]["lease_statistics"] = lease_stats
                else:
                    response["health"]["lease_statistics"] = {
                        "status": "not_initialized",
                        "message": "Lease manager not yet initialized",
                    }
            except Exception as e:
                response["health"]["lease_statistics"] = {"error": str(e)}

        elif echo_lower in ("cleanup", "reset") and not _ping_client_may_mutate(
            state
        ):
            response[echo_lower] = {
                "success": False,
                "error": (
                    f"ping(echo='{echo_lower}') requires an authenticated "
                    "developer/agent/admin client — observer and "
                    "unregistered clients have read-only access. Call "
                    "authenticate(client_type=...) first."
                ),
            }

        elif echo_lower == "cleanup":
            # Force cleanup of stuck assignments
            cleanup_count = 0

            if state.tasks_being_assigned:
                cleanup_count = len(state.tasks_being_assigned)
                state.tasks_being_assigned.clear()
                log_thinking(
                    "marcus",
                    f"Forced cleanup of {cleanup_count} stuck task assignments",
                )

            # Clear active operations tracking
            if hasattr(state, "_active_operations"):
                op_count = len(state._active_operations)
                state._active_operations.clear()
                cleanup_count += op_count

            response["cleanup"] = {
                "success": True,
                "cleaned_up": cleanup_count,
                "message": f"Cleaned up {cleanup_count} stuck assignments/operations",
            }

        elif echo_lower == "reset":
            # Complete reset of assignment state (use with caution)
            reset_info = {
                "tasks_cleared": len(state.tasks_being_assigned),
                "agents_cleared": len(state.agent_tasks),
                "operations_cleared": len(getattr(state, "_active_operations", set())),
            }

            if not confirm:
                # Mirrors remove_project's confirm=True requirement —
                # a full assignment-state wipe has no undo. Report what
                # WOULD be cleared without touching anything.
                response["reset"] = {
                    "success": False,
                    "error": "Set confirm=true to perform a full assignment state reset.",
                    "would_clear": reset_info,
                }
            else:
                # Clear all assignment state
                state.tasks_being_assigned.clear()
                state.agent_tasks.clear()
                if hasattr(state, "_active_operations"):
                    state._active_operations.clear()

                # Reset assignment monitor tracking
                if state.assignment_monitor:
                    # Clear reversion tracking
                    if hasattr(state.assignment_monitor, "_reversion_count"):
                        state.assignment_monitor._reversion_count.clear()
                    if hasattr(state.assignment_monitor, "_last_known_states"):
                        state.assignment_monitor._last_known_states.clear()

                log_thinking("marcus", "Performed full assignment state reset")

                response["reset"] = {
                    "success": True,
                    "reset_info": reset_info,
                    "warning": "All assignment state has been cleared!",
                }

    # Log the response immediately
    state.log_event("ping_response", response)

    # Log connection establishment
    conversation_logger.log_kanban_interaction(
        action="client_connection",
        direction="established",
        data={
            "client_type": client_type,
            "echo": echo,
            "timestamp": response["timestamp"],
        },
    )

    # Log for monitoring
    if client_type != "unknown":
        log_thinking(
            "marcus", f"Client connection established: {client_type} -> Marcus"
        )

    return response


async def check_assignment_health(state: Any) -> Dict[str, Any]:
    """
    Check the health of the assignment tracking system.

    Performs comprehensive health checks on:
    - Assignment persistence layer
    - Kanban client connectivity
    - Assignment monitor status
    - In-memory state consistency

    Parameters
    ----------
    state : Any
        Marcus server state instance

    Returns
    -------
    Dict[str, Any]
        Dict with detailed health status and metrics
    """
    try:
        # Initialize health checker
        health_checker = AssignmentHealthChecker(
            state.assignment_persistence, state.kanban_client, state.assignment_monitor
        )

        # Run health check
        health_status = await health_checker.check_assignment_health()

        # Add current in-memory state info
        health_status["in_memory_state"] = {
            "agent_tasks": len(state.agent_tasks),
            "tasks_being_assigned": len(state.tasks_being_assigned),
            "monitor_running": (
                state.assignment_monitor._running if state.assignment_monitor else False
            ),
        }

        return {"success": True, **health_status}

    except Exception as e:
        return {"success": False, "error": str(e)}
