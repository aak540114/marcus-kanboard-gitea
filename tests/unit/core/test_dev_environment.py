"""
Unit tests for src/core/dev_environment.py
"""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.dev_environment import (
    DevEnvironmentConfig,
    DevEnvironmentInfo,
    DevEnvironmentManager,
    PortAllocator,
)


class TestPortAllocator:
    """Tests for PortAllocator."""

    def test_allocate_returns_free_port(self):
        """allocate() returns a port within the configured range."""
        alloc = PortAllocator(port_range=(19100, 19200))
        port = alloc.allocate()
        assert 19100 <= port <= 19200

    def test_allocate_marks_port_in_use(self):
        """Allocated port is tracked as in-use."""
        alloc = PortAllocator(port_range=(19200, 19300))
        port = alloc.allocate()
        assert port in alloc._in_use

    def test_allocate_different_ports(self):
        """Two consecutive allocations do not return the same port."""
        alloc = PortAllocator(port_range=(19300, 19400))
        p1 = alloc.allocate()
        p2 = alloc.allocate()
        assert p1 != p2

    def test_release_removes_from_in_use(self):
        """release() removes the port from the in-use set."""
        alloc = PortAllocator(port_range=(19400, 19500))
        port = alloc.allocate()
        alloc.release(port)
        assert port not in alloc._in_use

    def test_release_is_idempotent(self):
        """Releasing a port not in-use does not raise."""
        alloc = PortAllocator(port_range=(19500, 19600))
        alloc.release(99999)  # not allocated

    def test_is_free_returns_false_for_listening_port(self):
        """_is_free returns False for a port that is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert PortAllocator._is_free(port) is False


class TestDevEnvironmentManager:
    """Tests for DevEnvironmentManager."""

    @pytest.fixture
    def config(self, tmp_path):
        return DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=False,
            dev_command="echo dev-server --port {port}",
            port_range=(19600, 19700),
        )

    @pytest.fixture
    def manager(self, config):
        return DevEnvironmentManager(config=config)

    def test_init_no_running_envs(self, manager):
        """Freshly created manager has no running environments."""
        assert manager.list_running() == []

    def test_get_info_returns_none_when_not_running(self, manager):
        """get_info returns None for a ticket with no running env."""
        assert manager.get_info("T-1", "jira") is None

    @pytest.mark.asyncio
    async def test_start_local_creates_env_info(self, manager):
        """start() in local mode creates a DevEnvironmentInfo entry."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            info = await manager.start("T-2", "jira", "ticket/jira/t-2")

        assert info.ticket_id == "T-2"
        assert info.provider == "jira"
        assert info.branch_name == "ticket/jira/t-2"
        assert info.port is not None
        assert info.url.startswith("http://")

    @pytest.mark.asyncio
    async def test_start_returns_existing_env_if_running(self, manager):
        """start() returns the existing env without creating a new one."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            info1 = await manager.start("T-3", "jira", "branch-a")
            info2 = await manager.start("T-3", "jira", "branch-b")

        assert info1.port == info2.port  # same env
        assert info1.branch_name == info2.branch_name

    @pytest.mark.asyncio
    async def test_stop_removes_env(self, manager):
        """stop() removes the running environment."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            await manager.start("T-4", "jira", "branch")

        stopped = await manager.stop("T-4", "jira")
        assert stopped is True
        assert manager.get_info("T-4", "jira") is None

    @pytest.mark.asyncio
    async def test_stop_returns_false_when_not_running(self, manager):
        """stop() returns False when no env is running for that ticket."""
        stopped = await manager.stop("T-99", "jira")
        assert stopped is False

    @pytest.mark.asyncio
    async def test_stop_releases_port(self, manager):
        """stop() releases the allocated port back to the pool."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            info = await manager.start("T-5", "jira", "branch")

        port = info.port
        await manager.stop("T-5", "jira")
        assert port not in manager._allocator._in_use

    @pytest.mark.asyncio
    async def test_list_running_shows_all_envs(self, manager):
        """list_running returns all active environments."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            await manager.start("T-6", "jira", "b1")
            await manager.start("T-7", "github", "b2")

        running = manager.list_running()
        assert len(running) == 2

    @pytest.mark.asyncio
    async def test_stop_all_clears_all_envs(self, manager):
        """stop_all() stops every running environment."""
        import subprocess

        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_popen):
            await manager.start("T-8", "jira", "b1")
            await manager.start("T-9", "jira", "b2")

        await manager.stop_all()
        assert manager.list_running() == []
