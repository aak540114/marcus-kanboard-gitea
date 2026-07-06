"""
Unit tests for src/core/dev_environment.py
"""

import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.dev_environment import (
    DevEnvironmentConfig,
    DevEnvironmentInfo,
    DevEnvironmentManager,
    PortAllocator,
    STACK_CONFIGS,
    detect_project_type,
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


# ---------------------------------------------------------------------------
# detect_project_type
# ---------------------------------------------------------------------------


class TestDetectProjectType:
    """Project-type sniffing from well-known files."""

    def test_detect_nodejs(self, tmp_path: Path) -> None:
        """package.json → nodejs."""
        (tmp_path / "package.json").write_text('{"name":"app"}')
        assert detect_project_type(str(tmp_path)) == "nodejs"

    def test_detect_python_fastapi(self, tmp_path: Path) -> None:
        """requirements.txt with fastapi → python-fastapi."""
        (tmp_path / "requirements.txt").write_text("fastapi>=0.100\nuvicorn\n")
        assert detect_project_type(str(tmp_path)) == "python-fastapi"

    def test_detect_python_uvicorn_only(self, tmp_path: Path) -> None:
        """requirements.txt with uvicorn only → python-fastapi."""
        (tmp_path / "requirements.txt").write_text("uvicorn[standard]\nhttpx\n")
        assert detect_project_type(str(tmp_path)) == "python-fastapi"

    def test_detect_python_flask(self, tmp_path: Path) -> None:
        """requirements.txt with flask → python-flask."""
        (tmp_path / "requirements.txt").write_text("flask>=3.0\n")
        assert detect_project_type(str(tmp_path)) == "python-flask"

    def test_detect_python_django(self, tmp_path: Path) -> None:
        """manage.py + requirements.txt → python-django."""
        (tmp_path / "requirements.txt").write_text("Django>=4.2\n")
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        assert detect_project_type(str(tmp_path)) == "python-django"

    def test_detect_python_generic(self, tmp_path: Path) -> None:
        """requirements.txt with no known framework → python."""
        (tmp_path / "requirements.txt").write_text("requests\npydantic\n")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_detect_pyproject_toml(self, tmp_path: Path) -> None:
        """pyproject.toml alone → python."""
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname='app'\n")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_detect_rust(self, tmp_path: Path) -> None:
        """Cargo.toml → rust."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname="app"\n')
        assert detect_project_type(str(tmp_path)) == "rust"

    def test_detect_go(self, tmp_path: Path) -> None:
        """go.mod → go."""
        (tmp_path / "go.mod").write_text("module myapp\ngo 1.22\n")
        assert detect_project_type(str(tmp_path)) == "go"

    def test_detect_ruby(self, tmp_path: Path) -> None:
        """Gemfile → ruby."""
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
        assert detect_project_type(str(tmp_path)) == "ruby"

    def test_detect_java_maven(self, tmp_path: Path) -> None:
        """pom.xml → java."""
        (tmp_path / "pom.xml").write_text("<project/>")
        assert detect_project_type(str(tmp_path)) == "java"

    def test_detect_java_gradle(self, tmp_path: Path) -> None:
        """build.gradle → java."""
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }")
        assert detect_project_type(str(tmp_path)) == "java"

    def test_detect_java_gradle_kts(self, tmp_path: Path) -> None:
        """build.gradle.kts → java."""
        (tmp_path / "build.gradle.kts").write_text("plugins { java }")
        assert detect_project_type(str(tmp_path)) == "java"

    def test_detect_php(self, tmp_path: Path) -> None:
        """composer.json → php."""
        (tmp_path / "composer.json").write_text('{"require":{}}')
        assert detect_project_type(str(tmp_path)) == "php"

    def test_detect_static_fallback(self, tmp_path: Path) -> None:
        """No known file → static."""
        assert detect_project_type(str(tmp_path)) == "static"

    def test_nodejs_wins_over_python(self, tmp_path: Path) -> None:
        """package.json takes precedence even when requirements.txt exists."""
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "requirements.txt").write_text("flask\n")
        assert detect_project_type(str(tmp_path)) == "nodejs"


# ---------------------------------------------------------------------------
# DevEnvironmentManager._build_entrypoint
# ---------------------------------------------------------------------------


class TestBuildEntrypoint:
    """Shell command builder used inside Docker containers.

    _build_entrypoint now takes explicit params:
      (branch_name, install_cmd, start_cmd, use_hm_reload, extra_apt=None)
    """

    def _mgr(self) -> DevEnvironmentManager:
        return DevEnvironmentManager(DevEnvironmentConfig())

    def test_nodejs_uses_npm_no_inotifywait(self) -> None:
        """nodejs stack: npm install + npm run dev, no inotifywait wrapper."""
        cmd = self._mgr()._build_entrypoint(
            "ticket/k/1",
            install_cmd="npm install",
            start_cmd="npm run dev -- --port 3000",
            use_hm_reload=True,
        )
        assert "npm install" in cmd
        assert "npm run dev" in cmd
        assert "inotifywait" not in cmd

    def test_python_fastapi_uses_uvicorn_inotifywait(self) -> None:
        """python-fastapi uses inotifywait (no --reload flag to avoid double-watcher)."""
        cmd = self._mgr()._build_entrypoint(
            "ticket/k/2",
            install_cmd="pip install -r requirements.txt",
            start_cmd="uvicorn main:app --host 0.0.0.0 --port 3000",
            use_hm_reload=False,
        )
        assert "uvicorn" in cmd
        assert "--reload" not in cmd
        assert "inotifywait" in cmd

    def test_static_uses_inotifywait_wrapper(self) -> None:
        """Static stack wraps server with inotifywait restart loop."""
        cmd = self._mgr()._build_entrypoint(
            "ticket/k/3",
            install_cmd="",
            start_cmd="python -m http.server 3000",
            use_hm_reload=False,
        )
        assert "inotifywait" in cmd
        assert "APP_PID" in cmd
        assert "kill $APP_PID" in cmd

    def test_php_uses_inotifywait_wrapper(self) -> None:
        """PHP stack wraps built-in server with inotifywait."""
        cmd = self._mgr()._build_entrypoint(
            "ticket/k/4",
            install_cmd="",
            start_cmd="php -S 0.0.0.0:3000",
            use_hm_reload=False,
        )
        assert "inotifywait" in cmd
        assert "php -S" in cmd

    def test_branch_name_present_in_command(self) -> None:
        """Branch checkout appears in the generated shell command."""
        cmd = self._mgr()._build_entrypoint(
            "feature/my-branch",
            install_cmd="npm install",
            start_cmd="npm run dev",
            use_hm_reload=True,
        )
        assert "git checkout feature/my-branch" in cmd

    def test_all_native_stacks_have_no_inotifywait(self) -> None:
        """Every stack with hm=True must not wrap with inotifywait."""
        mgr = self._mgr()
        for stack, cfg in STACK_CONFIGS.items():
            if cfg["hm"]:
                cmd = mgr._build_entrypoint(
                    "b",
                    install_cmd=cfg.get("install_cmd", ""),
                    start_cmd=cfg.get("start_cmd", "echo ok"),
                    use_hm_reload=True,
                )
                assert "inotifywait" not in cmd, f"{stack!r} should not use inotifywait"

    def test_all_non_native_stacks_use_inotifywait(self) -> None:
        """Every stack with hm=False must be wrapped with inotifywait."""
        mgr = self._mgr()
        for stack, cfg in STACK_CONFIGS.items():
            if not cfg["hm"]:
                cmd = mgr._build_entrypoint(
                    "b",
                    install_cmd=cfg.get("install_cmd", ""),
                    start_cmd=cfg.get("start_cmd", "echo ok"),
                    use_hm_reload=False,
                )
                assert "inotifywait" in cmd, f"{stack!r} should use inotifywait"
