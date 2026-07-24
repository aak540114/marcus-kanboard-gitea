"""
Guard: the MarcusDevEnv board-header template must HTML-escape agent-controlled
values (agent id, self-reported usage) before injecting them into innerHTML.

These fields come straight from an AI agent (the marcus_work `usage` payload
and the agent's chosen `agent_id`), so an unescaped interpolation into the
board's tooltip would be a stored-XSS vector. This is a cheap static regression
guard for that fix — there is no JS test harness for the plugin.
"""

from pathlib import Path

HEADER = (
    Path(__file__).resolve().parents[3]
    / "kanboard/plugins/MarcusDevEnv/Template/board/header.php"
)


def test_header_defines_escape_helper():
    src = HEADER.read_text()
    assert "function mEsc(" in src


def test_agent_and_usage_fields_are_escaped():
    src = HEADER.read_text()
    # The interpolations that build the tooltip must go through mEsc().
    assert "mEsc(a.agent_id)" in src
    assert "mEsc(a.ticket_id)" in src
    assert "mEsc(u.unit)" in src
    assert "mEsc(u.used)" in src
    assert "mEsc(u.limit)" in src


def test_no_unescaped_agent_id_interpolation():
    src = HEADER.read_text()
    # The old, vulnerable form concatenated a.agent_id raw into innerHTML.
    assert "+ a.agent_id +" not in src
    assert "+ a.agent_id;" not in src
