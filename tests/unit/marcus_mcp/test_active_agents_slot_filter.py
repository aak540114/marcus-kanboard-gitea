"""
Unit tests for the internal-slot filter behind ``/api/active-agents``.

HumanGatedWorkflow's human-gated auto-start can pre-claim several
READY+assigned tickets into internal ``marcus-<slot>`` reservations, up to
its configured parallel-agent capacity, before any real agent has actually
polled ``marcus_work`` for work. With capacity 3 and one real connected
agent, up to 3 tickets can carry a claim while only 1 is genuinely being
worked. ``/api/active-agents`` must exclude those reservations from the
``agents`` list it returns — otherwise the board's golden-ring highlight and
the badge tooltip's per-ticket rows both light up every pre-staged
reservation identically to a real agent's ticket.
"""

from src.marcus_mcp.server import _is_internal_agent_slot


class TestIsInternalAgentSlot:
    """The predicate active_agents() uses to filter its `agents` list."""

    def test_marcus_prefixed_id_is_internal(self):
        """A 'marcus-<slot>' claim is an internal reservation, not a real
        agent."""
        assert _is_internal_agent_slot("marcus-0") is True
        assert _is_internal_agent_slot("marcus-slot-2") is True

    def test_real_agent_id_is_not_internal(self):
        """A real worker's agent_id (whatever shape it takes) is not
        filtered out."""
        assert _is_internal_agent_slot("worker-abc123") is False
        assert _is_internal_agent_slot("claude-code-session-9") is False

    def test_none_is_not_internal(self):
        """An absent agent_id is not (mis)classified as an internal slot —
        callers already gate on ai_agent_id is not None separately."""
        assert _is_internal_agent_slot(None) is False

    def test_empty_string_is_not_internal(self):
        """An empty agent_id string is not (mis)classified either."""
        assert _is_internal_agent_slot("") is False
