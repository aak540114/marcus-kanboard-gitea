"""
Unit tests for src/marcus_mcp/tools/auth.py — role-based access control.

Regression coverage for a confirmed access-control bug: the "observer"
role is documented (authenticate()'s own docstring) as read-only, used by
analytics/monitoring clients like the dashboard, but its ROLE_TOOLS entry included
"remove_project" — a destructive operation. Any client authenticating as
"observer" could permanently delete a live project.
"""

from types import SimpleNamespace

import pytest

from src.marcus_mcp.tools.auth import ROLE_TOOLS, get_client_tools


class TestObserverRoleIsReadOnly:
    """The observer role must never grant destructive/mutating tools."""

    def test_observer_does_not_have_remove_project(self):
        """remove_project must not be reachable via the observer role."""
        assert "remove_project" not in ROLE_TOOLS["observer"]

    def test_observer_has_no_destructive_or_mutating_tools(self):
        """No tool whose name implies a write/delete/create action.

        A coarse but useful regression guard: observer's docstring
        ("Read-only access for monitoring/analytics") should mean no tool
        name starting with these verbs ever lands in its list again.
        """
        destructive_prefixes = ("remove_", "delete_", "create_", "add_", "update_")
        offenders = [
            tool
            for tool in ROLE_TOOLS["observer"]
            if tool.startswith(destructive_prefixes)
        ]
        assert offenders == [], f"observer role has non-read-only tools: {offenders}"

    def test_get_client_tools_for_observer_excludes_remove_project(self):
        """End-to-end: a registered observer client can't reach remove_project."""
        state = SimpleNamespace(
            _registered_clients={
                "dashboard-001": {"client_type": "observer"},
            }
        )
        tools = get_client_tools("dashboard-001", state)
        assert "remove_project" not in tools


class TestGetClientTools:
    """get_client_tools() resolution behavior."""

    def test_unregistered_client_gets_default_tools(self):
        """No client_id -> DEFAULT_TOOLS (ping/authenticate only)."""
        state = SimpleNamespace(_registered_clients={})
        assert get_client_tools(None, state) == ["ping", "authenticate"]

    def test_unknown_client_id_gets_default_tools(self):
        """A client_id not present in the registry falls back to defaults."""
        state = SimpleNamespace(_registered_clients={})
        assert get_client_tools("ghost-client", state) == ["ping", "authenticate"]

    def test_admin_gets_all_registered_tool_names(self):
        """Admin's "*" wildcard resolves to the full tool registry."""
        state = SimpleNamespace(
            _registered_clients={"root-001": {"client_type": "admin"}}
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "src.marcus_mcp.handlers.get_all_tool_names",
                lambda: ["a", "b", "c"],
            )
            tools = get_client_tools("root-001", state)
        assert tools == ["a", "b", "c"]

    def test_developer_role_tools_match_registry(self):
        """Sanity check the registry itself: developer is not read-only-only."""
        assert "create_project" in ROLE_TOOLS["developer"]
        assert "remove_project" not in ROLE_TOOLS["developer"]


class TestAgentRoleHasHumanGatedTools:
    """Coding agents must be able to see and call the Kanboard workflow tools.

    Regression: the human-gated tools (get_work_context, signal_ready_for_review,
    ...) were in the tool catalog but missing from ROLE_TOOLS["agent"], so an
    agent connecting to Marcus saw only the classic request_next_task surface
    and could never reach get_work_context.
    """

    def test_agent_role_includes_get_work_context(self):
        """The core entry point every coding agent needs is allowed."""
        assert "get_work_context" in ROLE_TOOLS["agent"]

    def test_agent_role_includes_all_human_gated_tools(self):
        """Every human-gated workflow tool is in the agent allowlist."""
        from src.marcus_mcp.tools.auth import HUMAN_GATED_AGENT_TOOLS

        for tool in HUMAN_GATED_AGENT_TOOLS:
            assert tool in ROLE_TOOLS["agent"], tool

    def test_registered_agent_resolves_to_human_gated_tools(self):
        """A client registered as type 'agent' gets the tools resolved."""
        state = SimpleNamespace(
            _registered_clients={
                "agent-1": {"client_type": "agent", "role": "coder"}
            }
        )
        tools = get_client_tools("agent-1", state)
        assert "get_work_context" in tools
        assert "signal_ready_for_review" in tools

    def test_get_work_context_definition_reaches_agent_client(self):
        """The catalog filter actually returns the get_work_context Tool."""
        from src.marcus_mcp.tools.auth import get_tool_definitions_for_client

        state = SimpleNamespace(
            _registered_clients={
                "agent-1": {"client_type": "agent", "role": "coder"}
            }
        )
        names = {t.name for t in get_tool_definitions_for_client("agent-1", state)}
        assert "get_work_context" in names
