"""
Unit tests for the "relearn the project's Tech Stack after a ticket
merges" wiring in src/marcus_mcp/server.py: the _relearn_stack_on_done
subscriber registered inside _wire_human_gated_workflow, right after
_track_project_stats.

_relearn_stack_on_done itself is a closure (not independently importable
— same as every other subscriber _wire_human_gated_workflow registers,
e.g. _track_project_stats, _reconcile_project_columns), so it's exercised
the same way test_project_stats_wiring.py exercises _track_project_stats:
call _wire_human_gated_workflow with a mocked Events bus, capture the
SECOND handler registered for "ticket.status_changed" (the first is
_track_project_stats), and invoke it directly with fake Event objects.

The actual re-inference/persistence logic itself
(_relearn_stack_after_ticket_done) is unit-tested directly in
test_determine_dev_preview_stack.py; this file covers only the event
parsing and background-task dispatch around it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.marcus_mcp.server import _wire_human_gated_workflow


def _make_server(**kwargs):
    defaults = dict(events=MagicMock(), kanban_client=MagicMock(), provider="kanboard")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_event(ticket_id="42", new_status="done", project_id=7):
    data = {"ticket_id": ticket_id, "new_status": new_status}
    data["task"] = {"project_id": project_id} if project_id is not None else {}
    return SimpleNamespace(data=data, timestamp="ts")


async def _get_relearn_handler(server):
    """Run _wire_human_gated_workflow (Gitea/Kanboard env vars unset) and
    return the SECOND handler registered for ticket.status_changed —
    _track_project_stats is always subscribed first, this module's
    _relearn_stack_on_done second."""
    with (
        patch(
            "src.workflows.human_gated_workflow.HumanGatedWorkflow",
            return_value=AsyncMock(),
        ),
        patch("src.marcus_mcp.tools.human_gated.register_workflow"),
    ):
        await _wire_human_gated_workflow(server)

    matches = [
        call.args[1]
        for call in server.events.subscribe.call_args_list
        if call.args[0] == "ticket.status_changed"
    ]
    assert len(matches) == 2, f"expected 2 subscribers, got {len(matches)}"
    return matches[1]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GITEA_URL", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.delenv("KANBOARD_URL", raising=False)


class TestRelearnStackOnDoneSubscription:
    @pytest.mark.asyncio
    async def test_subscribes_a_second_handler_to_ticket_status_changed(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        assert handler is not None

    @pytest.mark.asyncio
    async def test_fires_relearn_in_the_background_on_a_done_move(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)

        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(),
        ) as fake_relearn:
            await handler(_make_event(ticket_id="42", new_status="done", project_id=7))
            await asyncio.sleep(0)  # let the background task run

        fake_relearn.assert_awaited_once_with(server, 7, "/repos/my-app", "42")

    @pytest.mark.asyncio
    async def test_does_not_fire_on_a_non_done_move(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(),
        ) as fake_relearn:
            await handler(_make_event(new_status="waiting_for_human", project_id=7))
            await asyncio.sleep(0)

        fake_relearn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_project_sync_configured(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        server._project_sync = None

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(),
        ) as fake_relearn:
            await handler(_make_event(new_status="done", project_id=7))
            await asyncio.sleep(0)

        fake_relearn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_project_has_no_repo_mapping(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(return_value=None)
        server._project_sync = project_sync

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(),
        ) as fake_relearn:
            await handler(_make_event(new_status="done", project_id=7))
            await asyncio.sleep(0)

        fake_relearn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_on_non_numeric_project_id(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(),
        ) as fake_relearn:
            await handler(_make_event(new_status="done", project_id="not-a-number"))
            await asyncio.sleep(0)

        fake_relearn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handler_returns_before_a_slow_relearn_completes(self):
        """The whole point of firing this as a background task: a slow
        LLM call must never stall the ticket.status_changed handler,
        which runs inside BoardWatcher's shared poll_lock (see
        test_project_stats_wiring.py's identical concern for
        refresh_loc_count)."""
        server = _make_server()
        handler = await _get_relearn_handler(server)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_relearn(*_args):
            started.set()
            await release.wait()

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(side_effect=slow_relearn),
        ):
            await asyncio.wait_for(
                handler(_make_event(new_status="done", project_id=7)), timeout=0.2
            )
            await asyncio.wait_for(started.wait(), timeout=0.2)
            assert not release.is_set()

        release.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_exception_in_relearn_does_not_propagate(self):
        server = _make_server()
        handler = await _get_relearn_handler(server)
        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        with patch(
            "src.marcus_mcp.server._relearn_stack_after_ticket_done",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ) as fake_relearn:
            await handler(_make_event(new_status="done", project_id=7))  # must not raise
            await asyncio.sleep(0)

        fake_relearn.assert_awaited_once()
