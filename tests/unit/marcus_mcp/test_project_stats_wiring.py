"""
Unit tests for the project-stats wiring in src/marcus_mcp/server.py:
_get_project_stats_mgr and the _track_project_stats subscriber registered
inside _wire_human_gated_workflow.

_track_project_stats itself is a closure (not independently importable —
same as every other subscriber _wire_human_gated_workflow registers, e.g.
_reconcile_project_columns), so it's exercised by calling
_wire_human_gated_workflow with a mocked Events bus, capturing the handler
passed to events.subscribe("ticket.status_changed", ...), and invoking it
directly with fake Event objects.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.marcus_mcp.server import (
    _get_project_stats_mgr,
    _wire_human_gated_workflow,
)


def _make_server(**kwargs):
    defaults = dict(events=MagicMock(), kanban_client=MagicMock(), provider="kanboard")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_event(ticket_id="42", new_status="done", project_id=7, timestamp="ts"):
    data = {"ticket_id": ticket_id, "new_status": new_status}
    if project_id is not None:
        data["task"] = {"project_id": project_id}
    else:
        data["task"] = {}
    return SimpleNamespace(data=data, timestamp=timestamp)


async def _get_subscribed_handler(server, monkeypatch=None):
    """Run _wire_human_gated_workflow (Gitea/Kanboard env vars unset) and
    return the handler it registered for ticket.status_changed."""
    with (
        patch(
            "src.workflows.human_gated_workflow.HumanGatedWorkflow",
            return_value=AsyncMock(),
        ),
        patch("src.marcus_mcp.tools.human_gated.register_workflow"),
    ):
        await _wire_human_gated_workflow(server)

    for call in server.events.subscribe.call_args_list:
        if call.args[0] == "ticket.status_changed":
            return call.args[1]
    raise AssertionError("ticket.status_changed handler was never subscribed")


class TestGetProjectStatsMgr:
    """Must return one shared instance per server — see the docstring in
    server.py for why two instances would reopen a lost-update race."""

    def test_constructs_once_and_caches_on_server(self):
        server = SimpleNamespace()
        with patch("src.core.project_stats.ProjectStatsManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            first = _get_project_stats_mgr(server)
            second = _get_project_stats_mgr(server)

        mgr_cls.assert_called_once()
        assert first is second
        assert server._project_stats_mgr is first

    def test_reuses_a_preexisting_instance(self):
        server = SimpleNamespace()
        existing = MagicMock()
        server._project_stats_mgr = existing

        with patch("src.core.project_stats.ProjectStatsManager") as mgr_cls:
            result = _get_project_stats_mgr(server)

        mgr_cls.assert_not_called()
        assert result is existing


class TestTrackProjectStatsSubscription:
    @pytest.mark.asyncio
    async def test_subscribes_to_ticket_status_changed(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)
        assert handler is not None

    @pytest.mark.asyncio
    async def test_calls_record_status_change_with_resolved_fields(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        server._project_stats_mgr = stats_mgr

        event = _make_event(ticket_id="42", new_status="done", project_id=7)
        await handler(event)

        stats_mgr.record_status_change.assert_awaited_once_with(
            7, "42", "done", "ts"
        )

    @pytest.mark.asyncio
    async def test_skips_when_project_id_missing(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        server._project_stats_mgr = stats_mgr

        event = _make_event(project_id=None)
        await handler(event)

        stats_mgr.record_status_change.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_ticket_id_missing(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        server._project_stats_mgr = stats_mgr

        event = _make_event(ticket_id="")
        await handler(event)

        stats_mgr.record_status_change.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_new_status_missing(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        server._project_stats_mgr = stats_mgr

        event = _make_event(new_status=None)
        await handler(event)

        stats_mgr.record_status_change.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_on_non_numeric_project_id(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()

        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        server._project_stats_mgr = stats_mgr

        event = _make_event(project_id="not-a-number")
        await handler(event)

        stats_mgr.record_status_change.assert_not_awaited()


class TestLocRefreshOnDone:
    """Every genuinely-counted move to Done must refresh the project's
    line-of-code count — per the explicit "always up to date" requirement
    — but a duplicate delivery or a non-Done move must not trigger a git
    fetch at all."""

    async def _wired(self, monkeypatch):
        monkeypatch.delenv("GITEA_URL", raising=False)
        monkeypatch.delenv("GITEA_TOKEN", raising=False)
        monkeypatch.delenv("KANBOARD_URL", raising=False)
        server = _make_server()
        handler = await _get_subscribed_handler(server)

        stats_mgr = MagicMock()
        stats_mgr.record_status_change = AsyncMock(return_value=True)
        stats_mgr.refresh_loc_count = AsyncMock(return_value=123)
        server._project_stats_mgr = stats_mgr

        project_sync = MagicMock()
        project_sync.get_repo_for_project = MagicMock(
            return_value={"local_repo_path": "/repos/my-app"}
        )
        server._project_sync = project_sync

        return server, handler, stats_mgr, project_sync

    @pytest.mark.asyncio
    async def test_refreshes_loc_on_a_fresh_done_move(self, monkeypatch):
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)

        await handler(_make_event(new_status="done", project_id=7))

        project_sync.get_repo_for_project.assert_called_once_with(7)
        stats_mgr.refresh_loc_count.assert_awaited_once_with(7, "/repos/my-app")

    @pytest.mark.asyncio
    async def test_does_not_refresh_loc_on_duplicate_done_delivery(self, monkeypatch):
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)
        stats_mgr.record_status_change = AsyncMock(return_value=False)  # dedup'd

        await handler(_make_event(new_status="done", project_id=7))

        stats_mgr.refresh_loc_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_refresh_loc_on_waiting_for_human_move(self, monkeypatch):
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)

        await handler(_make_event(new_status="waiting_for_human", project_id=7))

        stats_mgr.refresh_loc_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_refresh_when_no_project_sync_configured(self, monkeypatch):
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)
        server._project_sync = None

        await handler(_make_event(new_status="done", project_id=7))

        stats_mgr.refresh_loc_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_refresh_when_project_has_no_repo_mapping(self, monkeypatch):
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)
        project_sync.get_repo_for_project = MagicMock(return_value=None)

        await handler(_make_event(new_status="done", project_id=7))

        stats_mgr.refresh_loc_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_loc_refresh_failure_does_not_propagate(self, monkeypatch):
        """A git failure inside refresh_loc_count must not crash the
        status-changed handler (which would otherwise be caught by
        Events.publish's isolation, but should never even need it)."""
        server, handler, stats_mgr, project_sync = await self._wired(monkeypatch)
        stats_mgr.refresh_loc_count = AsyncMock(side_effect=RuntimeError("git boom"))

        await handler(_make_event(new_status="done", project_id=7))  # must not raise
