"""
Unit tests for MarcusServer's periodic Context-retention sweep.

Covers `MarcusServer._start_context_retention_sweep`,
`MarcusServer._context_retention_sweep_loop`, and the sweep-cancellation
block inside `MarcusServer._cleanup_on_shutdown` — the background job that
periodically calls `sweep_context_retention` (src/core/context.py) so
decisions/implementations don't accumulate forever in every Context the
server holds.

Tests call the target methods unbound (`MarcusServer._method(fake, ...)`)
against a lightweight `SimpleNamespace` stand-in rather than constructing a
full `MarcusServer`, matching the pattern used by
tests/unit/marcus_mcp/test_lease_recovery_state_sync.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.marcus_mcp.server import MarcusServer


class TestStartContextRetentionSweep:
    """Test suite for MarcusServer._start_context_retention_sweep"""

    @pytest.mark.asyncio
    async def test_creates_a_background_task(self):
        fake = SimpleNamespace(_context_retention_sweep_running=False)
        fake._context_retention_sweep_loop = AsyncMock(return_value=None)

        MarcusServer._start_context_retention_sweep(fake)

        try:
            assert fake._context_retention_sweep_running is True
            assert isinstance(fake._context_retention_task, asyncio.Task)
            await asyncio.sleep(0)  # let the task body actually run
            fake._context_retention_sweep_loop.assert_called_once()
        finally:
            fake._context_retention_task.cancel()
            try:
                await fake._context_retention_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_is_a_noop_when_already_running(self):
        """Calling start twice (e.g. a re-entrant initialize()) must not
        orphan the first sweep task by replacing it with a second one."""
        fake = SimpleNamespace(_context_retention_sweep_running=True)

        MarcusServer._start_context_retention_sweep(fake)

        assert not hasattr(fake, "_context_retention_task")


class TestContextRetentionSweepLoop:
    """Test suite for MarcusServer._context_retention_sweep_loop"""

    @pytest.mark.asyncio
    async def test_sweeps_immediately_using_env_configured_retention_days(
        self, monkeypatch
    ):
        monkeypatch.setenv("CONTEXT_RETENTION_SWEEP_INTERVAL", "9999")
        monkeypatch.setenv("CONTEXT_RETENTION_DAYS", "7")
        fake = SimpleNamespace(_context_retention_sweep_running=True)
        calls = []

        async def fake_sweep(server, days):
            calls.append((server, days))
            fake._context_retention_sweep_running = False  # stop after one pass
            return 2

        with patch("src.marcus_mcp.server.sweep_context_retention", fake_sweep):
            await MarcusServer._context_retention_sweep_loop(fake)

        assert calls == [(fake, 7)]

    @pytest.mark.asyncio
    async def test_invalid_env_vars_fall_back_to_documented_defaults(
        self, monkeypatch
    ):
        monkeypatch.setenv("CONTEXT_RETENTION_SWEEP_INTERVAL", "not-a-number")
        monkeypatch.setenv("CONTEXT_RETENTION_DAYS", "also-not-a-number")
        fake = SimpleNamespace(_context_retention_sweep_running=True)
        calls = []

        async def fake_sweep(server, days):
            calls.append(days)
            fake._context_retention_sweep_running = False
            return 0

        with patch("src.marcus_mcp.server.sweep_context_retention", fake_sweep):
            await MarcusServer._context_retention_sweep_loop(fake)

        assert calls == [30]

    @pytest.mark.asyncio
    async def test_missing_env_vars_use_documented_defaults(self, monkeypatch):
        monkeypatch.delenv("CONTEXT_RETENTION_SWEEP_INTERVAL", raising=False)
        monkeypatch.delenv("CONTEXT_RETENTION_DAYS", raising=False)
        fake = SimpleNamespace(_context_retention_sweep_running=True)
        calls = []

        async def fake_sweep(server, days):
            calls.append(days)
            fake._context_retention_sweep_running = False
            return 0

        with patch("src.marcus_mcp.server.sweep_context_retention", fake_sweep):
            await MarcusServer._context_retention_sweep_loop(fake)

        assert calls == [30]

    @pytest.mark.asyncio
    async def test_a_failed_sweep_does_not_kill_the_loop(self, monkeypatch):
        """One sweep raising must not stop future sweeps from running."""
        monkeypatch.setenv("CONTEXT_RETENTION_SWEEP_INTERVAL", "0")
        fake = SimpleNamespace(_context_retention_sweep_running=True)
        state = {"calls": 0}

        async def fake_sweep(server, days):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("boom")
            fake._context_retention_sweep_running = False
            return 1

        with patch("src.marcus_mcp.server.sweep_context_retention", fake_sweep):
            await MarcusServer._context_retention_sweep_loop(fake)

        assert state["calls"] == 2

    @pytest.mark.asyncio
    async def test_stops_promptly_when_running_flag_is_cleared_mid_sleep(
        self, monkeypatch
    ):
        """The chunked sleep must notice _context_retention_sweep_running
        flip to False without waiting out the full interval."""
        monkeypatch.setenv("CONTEXT_RETENTION_SWEEP_INTERVAL", "9999")
        fake = SimpleNamespace(_context_retention_sweep_running=True)
        state = {"calls": 0}

        async def fake_sweep(server, days):
            state["calls"] += 1
            return 1

        async def fake_sleep(seconds):
            # First sweep just happened; stop the loop instead of actually
            # sleeping out a 9999s interval.
            fake._context_retention_sweep_running = False

        with patch("src.marcus_mcp.server.sweep_context_retention", fake_sweep):
            with patch("asyncio.sleep", fake_sleep):
                await MarcusServer._context_retention_sweep_loop(fake)

        assert state["calls"] == 1


class TestCleanupCancelsContextRetentionSweep:
    """Test suite for the sweep-cancellation block in
    MarcusServer._cleanup_on_shutdown."""

    @pytest.mark.asyncio
    async def test_cancels_a_running_sweep_task(self):
        async def never_ending():
            await asyncio.sleep(1000)

        fake = SimpleNamespace(
            _cleanup_done=False,
            tasks_being_assigned=set(),
            _active_operations=set(),
            assignment_monitor=None,
            lease_monitor=None,
            assignment_persistence=None,
            _context_retention_sweep_running=True,
            _context_retention_task=asyncio.create_task(never_ending()),
        )

        with patch("src.marcus_mcp.server.os._exit"):
            await MarcusServer._cleanup_on_shutdown(fake)

        assert fake._context_retention_sweep_running is False
        assert fake._context_retention_task.cancelled()

    @pytest.mark.asyncio
    async def test_tolerates_no_sweep_task_having_been_started(self):
        """A server that never got past _start_context_retention_sweep
        (e.g. shutdown during startup) must not raise on cleanup."""
        fake = SimpleNamespace(
            _cleanup_done=False,
            tasks_being_assigned=set(),
            _active_operations=set(),
            assignment_monitor=None,
            lease_monitor=None,
            assignment_persistence=None,
        )

        with patch("src.marcus_mcp.server.os._exit"):
            await MarcusServer._cleanup_on_shutdown(fake)

        assert fake._context_retention_sweep_running is False

    @pytest.mark.asyncio
    async def test_tolerates_an_already_finished_sweep_task(self):
        async def already_done():
            return None

        task = asyncio.create_task(already_done())
        await asyncio.sleep(0)  # let it finish
        fake = SimpleNamespace(
            _cleanup_done=False,
            tasks_being_assigned=set(),
            _active_operations=set(),
            assignment_monitor=None,
            lease_monitor=None,
            assignment_persistence=None,
            _context_retention_sweep_running=True,
            _context_retention_task=task,
        )

        with patch("src.marcus_mcp.server.os._exit"):
            await MarcusServer._cleanup_on_shutdown(fake)

        assert fake._context_retention_sweep_running is False
