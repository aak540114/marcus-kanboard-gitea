"""
Unit tests for src/core/dev_environment.py
"""

import socket
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.dev_env_settings import DevEnvSettingsManager
from src.core.dev_environment import (
    DevEnvironmentConfig,
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

    def test_touches_ready_marker_after_checkout_before_install(self) -> None:
        """The readiness marker is touched right after git checkout and
        before install_cmd — refresh()'s _wait_until_ready polls for it
        to avoid racing the entrypoint's own initial checkout."""
        cmd = self._mgr()._build_entrypoint(
            "ticket/k/5",
            install_cmd="npm install",
            start_cmd="npm run dev",
            use_hm_reload=True,
        )
        checkout_idx = cmd.index("git checkout")
        marker_idx = cmd.index("touch /tmp/.marcus-ready")
        install_idx = cmd.index("npm install")
        assert checkout_idx < marker_idx < install_idx

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


# ---------------------------------------------------------------------------
# Per-call repo_path override + Docker-outside-of-Docker host path
# translation (docker run -v <host_path>:/app must be a HOST path when
# Marcus itself runs inside a container talking to the host's Docker
# daemon over a mounted docker.sock).
# ---------------------------------------------------------------------------


class TestStartDockerRepoPath:
    """start() docker path: per-call repo_path override + host path translation."""

    @pytest.fixture
    def docker_config(self, tmp_path):
        return DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=True,
            auto_detect=False,
            dev_command="npm run dev -- --port {port}",
            port_range=(19700, 19750),
        )

    @pytest.fixture
    def docker_manager(self, docker_config, tmp_path):
        return DevEnvironmentManager(
            config=docker_config,
            settings_manager=DevEnvSettingsManager(data_dir=tmp_path),
        )

    @pytest.mark.asyncio
    async def test_uses_per_call_repo_path_override(self, docker_manager, tmp_path):
        """An explicit repo_path passed to start() overrides self.config.repo_path."""
        override_path = str(tmp_path / "other-repo")
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start(
                "T-10", "kanboard", "ticket/kanboard/t-10", repo_path=override_path
            )
        cmd = mock_run.call_args[0][0]
        assert f"{override_path}:/src:ro" in cmd

    @pytest.mark.asyncio
    async def test_falls_back_to_config_repo_path(self, docker_manager, tmp_path):
        """No repo_path override → self.config.repo_path is used, as before."""
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start("T-11", "kanboard", "ticket/kanboard/t-11")
        cmd = mock_run.call_args[0][0]
        assert f"{tmp_path!s}:/src:ro" in cmd

    @pytest.mark.asyncio
    async def test_source_is_mounted_read_only(self, docker_manager, tmp_path):
        """The source repo is mounted read-only so a preview can't mutate it."""
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start("T-15", "kanboard", "ticket/kanboard/t-15")
        cmd = mock_run.call_args[0][0]
        # The mount value ends with ':ro' and there is no writable :/app mount.
        v_index = cmd.index("-v")
        assert cmd[v_index + 1].endswith(":/src:ro")
        assert not any(
            isinstance(p, str) and p.endswith(":/app") for p in cmd
        )

    @pytest.mark.asyncio
    async def test_translates_to_host_path_when_dood_configured(
        self, docker_manager, monkeypatch
    ):
        """MARCUS_HOST_PROJECT_ROOT set → /app/... repo_path becomes a host path."""
        monkeypatch.setenv("MARCUS_HOST_PROJECT_ROOT", "/home/user/marcus")
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start(
                "T-12",
                "kanboard",
                "ticket/kanboard/t-12",
                repo_path="/app/data/repos/x",
            )
        cmd = mock_run.call_args[0][0]
        assert "/home/user/marcus/data/repos/x:/src:ro" in cmd
        assert "/app/data/repos/x:/src:ro" not in cmd

    @pytest.mark.asyncio
    async def test_translates_relative_repo_path_when_dood_configured(
        self, docker_manager, monkeypatch
    ):
        """A ./data/... relative repo_path is also translated."""
        monkeypatch.setenv("MARCUS_HOST_PROJECT_ROOT", "/srv/marcus")
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start(
                "T-13",
                "kanboard",
                "ticket/kanboard/t-13",
                repo_path="./data/repos/y",
            )
        cmd = mock_run.call_args[0][0]
        assert "/srv/marcus/data/repos/y:/src:ro" in cmd

    @pytest.mark.asyncio
    async def test_no_translation_when_host_root_unset(
        self, docker_manager, monkeypatch
    ):
        """MARCUS_HOST_PROJECT_ROOT unset (e.g. local/non-Docker) → path used as-is."""
        monkeypatch.delenv("MARCUS_HOST_PROJECT_ROOT", raising=False)
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start(
                "T-14",
                "kanboard",
                "ticket/kanboard/t-14",
                repo_path="/app/data/repos/z",
            )
        cmd = mock_run.call_args[0][0]
        assert "/app/data/repos/z:/src:ro" in cmd


# ---------------------------------------------------------------------------
# max_parallel_containers enforcement
# ---------------------------------------------------------------------------


class TestMaxParallelContainers:
    """DevEnvironmentManager.start() honours DevEnvSettingsManager's limit."""

    @pytest.fixture
    def limited_manager(self, tmp_path):
        settings = DevEnvSettingsManager(data_dir=tmp_path)
        settings.set_max_parallel_containers(1)
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=False,
            dev_command="echo dev-server --port {port}",
            port_range=(19750, 19800),
        )
        return DevEnvironmentManager(config=config, settings_manager=settings)

    @pytest.mark.asyncio
    async def test_raises_when_limit_reached_for_new_ticket(self, limited_manager):
        """A second, different ticket is refused once the limit is hit."""
        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_popen):
            await limited_manager.start("T-20", "kanboard", "b1")
            with pytest.raises(RuntimeError, match="[Mm]ax parallel"):
                await limited_manager.start("T-21", "kanboard", "b2")

    @pytest.mark.asyncio
    async def test_existing_ticket_not_blocked_by_its_own_env(self, limited_manager):
        """Re-requesting the SAME ticket's already-running env doesn't count as new."""
        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_popen):
            info1 = await limited_manager.start("T-22", "kanboard", "b1")
            info2 = await limited_manager.start("T-22", "kanboard", "b1")
        assert info1.port == info2.port

    @pytest.mark.asyncio
    async def test_stopping_frees_a_slot(self, limited_manager):
        """Stopping the running env allows a new ticket's env to start."""
        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_popen):
            await limited_manager.start("T-23", "kanboard", "b1")
            await limited_manager.stop("T-23", "kanboard")
            info = await limited_manager.start("T-24", "kanboard", "b2")
        assert info.ticket_id == "T-24"

    @pytest.mark.asyncio
    async def test_no_limit_when_unset(self, tmp_path):
        """No configured limit (None) → unlimited, matching pre-existing behaviour."""
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=False,
            dev_command="echo dev-server --port {port}",
            port_range=(19800, 19850),
        )
        mgr = DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )
        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_popen):
            await mgr.start("T-25", "kanboard", "b1")
            await mgr.start("T-26", "kanboard", "b2")
        assert len(mgr.list_running()) == 2


# ---------------------------------------------------------------------------
# _wait_until_ready() — guards refresh() against racing the container's
# own initial `git checkout` (see _build_entrypoint's readiness marker).
# ---------------------------------------------------------------------------


class TestWaitUntilReady:
    @pytest.fixture
    def manager(self, tmp_path):
        config = DevEnvironmentConfig(repo_path=str(tmp_path), use_docker=True)
        return DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )

    @pytest.mark.asyncio
    async def test_ready_on_first_check_returns_true_immediately(self, manager):
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0)
        ) as mock_run, patch("asyncio.sleep") as mock_sleep:
            result = await manager._wait_until_ready("c1")
        assert result is True
        mock_run.assert_called_once()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_polls_until_marker_appears(self, manager):
        results = [MagicMock(returncode=1), MagicMock(returncode=1), MagicMock(returncode=0)]
        with patch("subprocess.run", side_effect=results) as mock_run, patch(
            "asyncio.sleep", new=AsyncMock()
        ) as mock_sleep:
            result = await manager._wait_until_ready("c1")
        assert result is True
        assert mock_run.call_count == 3
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_returns_false_after_exhausting_poll_budget(self, manager):
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=1)
        ) as mock_run, patch("asyncio.sleep", new=AsyncMock()):
            result = await manager._wait_until_ready("c1")
        assert result is False
        assert mock_run.call_count == 5  # _READY_POLL_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_timeout_returns_false_immediately_without_retry(self, manager):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=60),
        ) as mock_run, patch("asyncio.sleep") as mock_sleep:
            result = await manager._wait_until_ready("c1")
        assert result is False
        mock_run.assert_called_once()
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# refresh() — instant webhook-driven reload trigger
# ---------------------------------------------------------------------------


class TestRefresh:
    """refresh() pulls the latest branch commit into a running container."""

    @pytest.fixture
    def docker_manager(self, tmp_path):
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=True,
            auto_detect=False,
            dev_command="npm run dev -- --port {port}",
            port_range=(19850, 19900),
        )
        return DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_not_running(self, docker_manager):
        """No environment running for the ticket → False, no docker call."""
        with patch("subprocess.run") as mock_run:
            result = await docker_manager.refresh("T-30", "kanboard")
        assert result is False
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_git_fetch_reset_via_docker_exec(self, docker_manager):
        """refresh() execs git fetch + hard reset to the branch inside the container."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            info = await docker_manager.start("T-31", "kanboard", "feature/x")

        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            ok = await docker_manager.refresh("T-31", "kanboard")

        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["docker", "exec", info.container_name]
        assert "git fetch origin" in cmd[-1]
        assert "origin/feature/x" in cmd[-1]

    @pytest.mark.asyncio
    async def test_returns_false_on_git_failure(self, docker_manager):
        """A non-zero exit from the docker exec command → False, not raised."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await docker_manager.start("T-32", "kanboard", "feature/y")

        def _side_effect(cmd, **kwargs):
            if "test -f" in cmd[-1]:
                return MagicMock(returncode=0, stderr="")  # ready — skip past the poll
            return MagicMock(returncode=1, stderr="fatal: not a repo")

        with patch("subprocess.run", side_effect=_side_effect):
            ok = await docker_manager.refresh("T-32", "kanboard")
        assert ok is False

    @pytest.mark.asyncio
    async def test_skips_git_command_when_container_not_ready(self, docker_manager):
        """refresh() must not run git fetch/reset before the entrypoint's
        own initial checkout has finished (see _wait_until_ready) — a
        push arriving while the container is still installing
        dependencies must not race that checkout."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await docker_manager.start("T-45", "kanboard", "feature/z")

        with patch(
            "subprocess.run", return_value=MagicMock(returncode=1)
        ) as mock_run, patch("asyncio.sleep", new=AsyncMock()):
            ok = await docker_manager.refresh("T-45", "kanboard")

        assert ok is False
        # Only the readiness-check command should have run — never fetch/reset.
        for call in mock_run.call_args_list:
            assert "git fetch" not in call.args[0][-1]

    @pytest.mark.asyncio
    async def test_returns_false_for_local_non_docker_env(self, tmp_path):
        """use_docker=False environments have no container to exec into."""
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=False,
            dev_command="echo dev --port {port}",
            port_range=(19900, 19950),
        )
        mgr = DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )
        mock_popen = MagicMock(spec=subprocess.Popen)
        mock_popen.poll.return_value = None
        with patch("subprocess.Popen", return_value=mock_popen):
            await mgr.start("T-33", "kanboard", "b1")
        assert await mgr.refresh("T-33", "kanboard") is False


# ---------------------------------------------------------------------------
# Docker CLI call timeouts — an unresponsive daemon must fail fast, not hang
# the calling coroutine (and the HTTP request/executor thread behind it).
# ---------------------------------------------------------------------------


class TestDockerCommandTimeouts:
    @pytest.fixture
    def docker_manager(self, tmp_path):
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path),
            use_docker=True,
            auto_detect=False,
            dev_command="npm run dev -- --port {port}",
            port_range=(19950, 20000),
        )
        return DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )

    @pytest.mark.asyncio
    async def test_start_docker_passes_a_timeout(self, docker_manager):
        """docker run is called with an explicit timeout, not left unbounded."""
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await docker_manager.start("T-40", "kanboard", "ticket/kanboard/t-40")
        assert mock_run.call_args.kwargs.get("timeout") is not None

    @pytest.mark.asyncio
    async def test_start_docker_timeout_raises_and_releases_port(self, docker_manager):
        """A hung `docker run` raises RuntimeError instead of hanging forever,
        and does not leak the port it had already allocated."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=60),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                await docker_manager.start("T-41", "kanboard", "ticket/kanboard/t-41")

        assert docker_manager.get_info("T-41", "kanboard") is None
        # The port must be free again — not leaked by the failed start.
        alloc = docker_manager._allocator
        port = alloc.allocate()
        assert 19950 <= port <= 20000
        alloc.release(port)

    @pytest.mark.asyncio
    async def test_stop_docker_timeout_does_not_raise(self, docker_manager):
        """A hung `docker stop` doesn't raise, but must NOT report success —
        the container's real state is unknown, so bookkeeping (and the
        allocated port) stays intact rather than being freed for reuse and
        colliding with a container that may still actually be running."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            info = await docker_manager.start("T-42", "kanboard", "ticket/kanboard/t-42")

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "stop"], timeout=60),
        ):
            stopped = await docker_manager.stop("T-42", "kanboard")

        assert stopped is False
        assert docker_manager.get_info("T-42", "kanboard") is not None
        assert info.port in docker_manager._allocator._in_use

    @pytest.mark.asyncio
    async def test_stop_retry_succeeds_after_a_timed_out_attempt(self, docker_manager):
        """A subsequent stop() call can still find and successfully stop
        the environment a prior timed-out attempt left tracked."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await docker_manager.start("T-44", "kanboard", "ticket/kanboard/t-44")

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "stop"], timeout=60),
        ):
            await docker_manager.stop("T-44", "kanboard")

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            stopped = await docker_manager.stop("T-44", "kanboard")

        assert stopped is True
        assert docker_manager.get_info("T-44", "kanboard") is None

    @pytest.mark.asyncio
    async def test_refresh_timeout_returns_false(self, docker_manager):
        """A hung `docker exec` during refresh returns False, not hangs."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            await docker_manager.start("T-43", "kanboard", "feature/x")

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=60),
        ):
            ok = await docker_manager.refresh("T-43", "kanboard")

        assert ok is False


class TestReconcileOrphans:
    """Startup reconciliation of marcus-dev-* containers from a dead run.

    _envs is in-memory only: after a Marcus crash/restart, containers
    started by the previous process are orphaned forever — no idle
    timeout reaps them, their ports stay held, and the next start() for
    the same ticket dies on a docker name conflict with a misleading
    "check that Docker is running" comment. reconcile_orphans() removes
    every marcus-dev-* container not currently registered; called at
    workflow startup, when the registry is empty and any such container
    is by definition an orphan.
    """

    @pytest.fixture
    def docker_manager(self, tmp_path):
        from src.core.dev_environment import DevEnvironmentConfig

        return DevEnvironmentManager(
            config=DevEnvironmentConfig(
                repo_path=str(tmp_path), use_docker=True
            )
        )

    @pytest.mark.asyncio
    async def test_removes_unregistered_containers(self, docker_manager):
        """Orphaned marcus-dev-* containers are force-removed."""

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            if cmd[:2] == ["docker", "ps"]:
                result.stdout = "abc123\ndef456\n"
            else:
                result.stdout = ""
            return result

        with patch(
            "src.core.dev_environment.subprocess.run", side_effect=fake_run
        ):
            removed = await docker_manager.reconcile_orphans()

        assert removed == 2
        rm_call = next(c for c in calls if c[:2] == ["docker", "rm"])
        assert "-f" in rm_call
        assert "abc123" in rm_call and "def456" in rm_call

    @pytest.mark.asyncio
    async def test_no_orphans_is_a_noop(self, docker_manager):
        """No matching containers → nothing removed, no rm call."""

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        with patch(
            "src.core.dev_environment.subprocess.run", side_effect=fake_run
        ):
            removed = await docker_manager.reconcile_orphans()

        assert removed == 0
        assert not any(c[:2] == ["docker", "rm"] for c in calls)

    @pytest.mark.asyncio
    async def test_docker_failure_degrades_to_noop(self, docker_manager):
        """A docker error (daemon down) returns 0 instead of raising."""
        with patch(
            "src.core.dev_environment.subprocess.run",
            side_effect=OSError("docker not found"),
        ):
            removed = await docker_manager.reconcile_orphans()
        assert removed == 0


class TestPruneIfDead:
    """A registered env whose container died must stop reading as running.

    Containers run with --rm and their app (dependency install, git
    checkout, dev server) starts AFTER `docker run -d` returns — a
    failure seconds later deletes the container entirely, while the
    registry still reports the env as running, get_info hands out a
    dead URL, and the port/slot stay consumed. prune_if_dead() checks
    the container's actual state and purges dead registrations.
    """

    @pytest.fixture
    def docker_manager(self, tmp_path):
        from src.core.dev_environment import DevEnvironmentConfig

        return DevEnvironmentManager(
            config=DevEnvironmentConfig(repo_path=str(tmp_path), use_docker=True)
        )

    def _register_env(self, manager, tid="T-1", provider="kanboard", port=19650):
        from src.core.dev_environment import DevEnvironmentInfo

        info = DevEnvironmentInfo(
            ticket_id=tid,
            provider=provider,
            branch_name=f"ticket/{provider}/{tid.lower()}",
            port=port,
            container_name=f"marcus-dev-{provider}-{tid.lower()}",
            url=f"http://localhost:{port}",
        )
        manager._envs[f"{provider}:{tid}"] = info
        return info

    @pytest.mark.asyncio
    async def test_dead_container_is_pruned(self, docker_manager):
        """Container gone (--rm removed it) → registration purged."""
        self._register_env(docker_manager)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1  # docker inspect: no such container
            result.stdout = ""
            return result

        with patch(
            "src.core.dev_environment.subprocess.run", side_effect=fake_run
        ):
            pruned = await docker_manager.prune_if_dead("T-1", "kanboard")

        assert pruned is True
        assert docker_manager.get_info("T-1", "kanboard") is None

    @pytest.mark.asyncio
    async def test_live_container_is_kept(self, docker_manager):
        """Running container → registration untouched."""
        self._register_env(docker_manager)

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "true\n"
            return result

        with patch(
            "src.core.dev_environment.subprocess.run", side_effect=fake_run
        ):
            pruned = await docker_manager.prune_if_dead("T-1", "kanboard")

        assert pruned is False
        assert docker_manager.get_info("T-1", "kanboard") is not None

    @pytest.mark.asyncio
    async def test_docker_error_keeps_registration(self, docker_manager):
        """Daemon unreachable → true state unknown → do not prune."""
        self._register_env(docker_manager)

        with patch(
            "src.core.dev_environment.subprocess.run",
            side_effect=OSError("daemon down"),
        ):
            pruned = await docker_manager.prune_if_dead("T-1", "kanboard")

        assert pruned is False
        assert docker_manager.get_info("T-1", "kanboard") is not None

    @pytest.mark.asyncio
    async def test_unregistered_ticket_is_noop(self, docker_manager):
        """No env registered → nothing to prune."""
        assert await docker_manager.prune_if_dead("T-9", "kanboard") is False


class TestRefreshByBranch:
    """refresh_by_branch matches an env by its exact branch name.

    The Gitea push webhook only knows the branch name — and the branch's
    ticket-id segment is sanitized and lowercased at branch-creation
    time, so parsing it back can never equal the registry's raw ticket
    id for non-lowercase ids (jira PROJ-42 → branch ticket/jira/proj-42
    → parsed "proj-42" ≠ key "jira:PROJ-42"). Matching by the stored
    branch name is exact by construction.
    """

    @pytest.fixture
    def docker_manager(self, tmp_path):
        from src.core.dev_environment import DevEnvironmentConfig

        return DevEnvironmentManager(
            config=DevEnvironmentConfig(repo_path=str(tmp_path), use_docker=True)
        )

    def _register(self, manager, tid, provider, branch):
        from src.core.dev_environment import DevEnvironmentInfo

        manager._envs[f"{provider}:{tid}"] = DevEnvironmentInfo(
            ticket_id=tid,
            provider=provider,
            branch_name=branch,
            port=19660,
            container_name=f"marcus-dev-{provider}-x",
            url="http://localhost:19660",
        )

    @pytest.mark.asyncio
    async def test_matches_uppercase_ticket_id_env(self, docker_manager):
        """PROJ-42 env is found via its lowercased branch name."""
        self._register(docker_manager, "PROJ-42", "jira", "ticket/jira/proj-42")
        docker_manager.refresh = AsyncMock(return_value=True)

        result = await docker_manager.refresh_by_branch("ticket/jira/proj-42")

        assert result is True
        docker_manager.refresh.assert_awaited_once_with("PROJ-42", "jira")

    @pytest.mark.asyncio
    async def test_no_matching_branch_returns_false(self, docker_manager):
        """No env for this branch → False, refresh never called."""
        self._register(docker_manager, "1", "kanboard", "ticket/kanboard/1")
        docker_manager.refresh = AsyncMock()

        result = await docker_manager.refresh_by_branch("ticket/kanboard/2")

        assert result is False
        docker_manager.refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Resilient entrypoint: alpine base + apk + guaranteed static fallback.
#
# The container previously ran on debian:bookworm-slim and installed every
# runtime at start via apt-get; a failure left the --rm container dead and
# invisible in `docker ps` while the browser got ERR_CONNECTION_REFUSED. The
# new entrypoint installs via apk (best-effort) and always falls back to
# `python3 -m http.server` (python3 ships in the base image), so the port is
# always answered and the container always stays alive/inspectable.
# ---------------------------------------------------------------------------


class TestEntrypointResilience:
    """The generated entrypoint must never leave the port unanswered."""

    def _mgr(self) -> DevEnvironmentManager:
        return DevEnvironmentManager(DevEnvironmentConfig())

    def test_base_image_is_alpine(self) -> None:
        """The single base image is a lightweight alpine image."""
        from src.core.dev_environment import _BASE_IMAGE

        assert "alpine" in _BASE_IMAGE

    def test_uses_apk_not_apt(self) -> None:
        """Package install uses Alpine's apk, never Debian apt-get."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="npm install", start_cmd="npm run dev", use_hm_reload=True
        )
        assert "apk add" in cmd
        assert "apt-get" not in cmd

    def test_marks_app_dir_git_safe(self) -> None:
        """/src and /app are both marked safe git directories before checkout."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="", start_cmd="busybox httpd -f -p 3000 -h /app",
            use_hm_reload=False,
        )
        assert "safe.directory /src" in cmd
        assert "safe.directory /app" in cmd

    def test_clones_source_into_isolated_app(self) -> None:
        """The entrypoint clones the read-only /src into a container-local
        /app, so the preview never mutates the shared source repo."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="", start_cmd="busybox httpd -f -p 3000 -h /app",
            use_hm_reload=False,
        )
        assert "git clone /src /app" in cmd
        # The clone must happen before the checkout.
        assert cmd.index("git clone /src /app") < cmd.index("git checkout")

    def test_origin_left_pointing_at_local_src(self) -> None:
        """The clone's origin is left as /src (a reachable local path) — NOT
        re-pointed at the Gitea URL, which is unreachable from the preview
        container's default-bridge network. refresh() fetches from /src."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="", start_cmd="busybox httpd -f -p 3000 -h /app",
            use_hm_reload=False,
        )
        assert "remote set-url origin" not in cmd

    def test_hm_stack_stays_alive_if_dev_cmd_exits_zero(self) -> None:
        """The served wrapper uses ';' not '||' so the fallback runs whenever
        the dev command RETURNS (even exit 0) — otherwise a self-daemonizing
        dev command would let PID 1 exit and the --rm container vanish."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="npm install",
            start_cmd="npm run dev -- --port 3000", use_hm_reload=True,
        )
        assert "npm run dev -- --port 3000; busybox httpd" in cmd

    def test_checkout_falls_back_to_origin_tracking_branch(self) -> None:
        """A branch that exists only on origin is checked out as a new
        tracking branch when a plain checkout can't find it locally."""
        cmd = self._mgr()._build_entrypoint(
            "feature/x", install_cmd="", start_cmd="busybox httpd -f -p 3000 -h /app",
            use_hm_reload=False,
        )
        assert "git checkout feature/x" in cmd
        assert "git checkout -b feature/x origin/feature/x" in cmd

    def test_static_fallback_present_for_hm_stack(self) -> None:
        """HMR stacks fall back to the zero-install busybox httpd server."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="npm install",
            start_cmd="npm run dev -- --port 3000", use_hm_reload=True,
        )
        assert "busybox httpd" in cmd
        assert "npm run dev" in cmd

    def test_static_fallback_present_for_non_hm_stack(self) -> None:
        """Non-HMR stacks also fall back to busybox httpd on failure."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="",
            start_cmd="flask run --host 0.0.0.0 --port 3000", use_hm_reload=False,
        )
        assert "busybox httpd" in cmd
        assert "flask run" in cmd

    def test_fallback_needs_no_language_runtime(self) -> None:
        """The fallback server is BusyBox (always in alpine), not python —
        so it works even when no language runtime has been installed."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="", start_cmd="npm run dev", use_hm_reload=True,
        )
        assert "python3 -m http.server" not in cmd

    def test_install_failure_is_non_fatal(self) -> None:
        """A failed dependency install must not abort the entrypoint."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="npm install", start_cmd="npm run dev",
            use_hm_reload=True,
        )
        assert "npm install || true" in cmd

    def test_non_hm_keeps_pid1_alive_when_watcher_exits(self) -> None:
        """A non-HMR entrypoint waits on the server so PID 1 (and the
        container) never exits early if inotifywait is missing/errors —
        otherwise the --rm container would vanish from `docker ps` mid-serve."""
        cmd = self._mgr()._build_entrypoint(
            "b", install_cmd="",
            start_cmd="python -m http.server 3000", use_hm_reload=False,
        )
        # The loop is followed by a final `wait $APP_PID`.
        done_idx = cmd.rindex("done")
        wait_idx = cmd.rindex("wait $APP_PID")
        assert wait_idx > done_idx, "final `wait $APP_PID` must follow the watch loop"

    @pytest.mark.asyncio
    async def test_docker_run_uses_alpine_base(self, tmp_path) -> None:
        """`docker run` is invoked with the alpine base image."""
        config = DevEnvironmentConfig(
            repo_path=str(tmp_path), use_docker=True, auto_detect=False,
            dev_command="npm run dev -- --port {port}", port_range=(20050, 20100),
        )
        mgr = DevEnvironmentManager(
            config=config, settings_manager=DevEnvSettingsManager(data_dir=tmp_path)
        )
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=0, stderr="")
        ) as mock_run:
            await mgr.start("T-50", "kanboard", "ticket/kanboard/t-50")
        cmd = mock_run.call_args[0][0]
        assert any("alpine" in str(part) for part in cmd)


# ---------------------------------------------------------------------------
# is_serving(): a container can be registered/running yet not yet answering
# on its port. The /dev-env/view route uses this to decide when it is safe to
# redirect the browser (avoids ERR_CONNECTION_REFUSED).
# ---------------------------------------------------------------------------


class TestIsServing:
    """DevEnvironmentManager.is_serving reflects real port readiness."""

    @pytest.fixture
    def docker_manager(self, tmp_path):
        return DevEnvironmentManager(
            config=DevEnvironmentConfig(repo_path=str(tmp_path), use_docker=True),
            settings_manager=DevEnvSettingsManager(data_dir=tmp_path),
        )

    def _register(self, manager, port):
        from src.core.dev_environment import DevEnvironmentInfo

        manager._envs["kanboard:T-1"] = DevEnvironmentInfo(
            ticket_id="T-1", provider="kanboard", branch_name="b",
            port=port, container_name="c", url=f"http://localhost:{port}",
        )

    def test_port_is_listening_true_for_bound_socket(self) -> None:
        """_port_is_listening detects a real listening socket."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert DevEnvironmentManager._port_is_listening(port) is True

    def test_is_serving_false_when_not_registered(self, docker_manager) -> None:
        """No env registered → not serving."""
        assert docker_manager.is_serving("T-9", "kanboard") is False

    def test_docker_is_serving_probes_inside_container(self, docker_manager) -> None:
        """For a Docker env, is_serving probes INSIDE the container (works
        under Docker-outside-of-Docker), NOT the host loopback."""
        self._register(docker_manager, 12345)
        with patch.object(
            DevEnvironmentManager, "_container_port_open", return_value=True
        ) as probe, patch.object(
            DevEnvironmentManager, "_port_is_listening"
        ) as host_probe:
            assert docker_manager.is_serving("T-1", "kanboard") is True
        probe.assert_called_once_with("c")  # container_name from _register
        host_probe.assert_not_called()  # never falls back to host loopback

    def test_docker_is_serving_false_when_container_not_listening(
        self, docker_manager
    ) -> None:
        """Container up but app not bound yet (still building) → not serving."""
        self._register(docker_manager, 12345)
        with patch.object(
            DevEnvironmentManager, "_container_port_open", return_value=False
        ):
            assert docker_manager.is_serving("T-1", "kanboard") is False

    def test_local_env_is_serving_uses_host_loopback(self, tmp_path) -> None:
        """A local (non-Docker) process is reachable on the host loopback, so
        is_serving uses the cheaper TCP probe there — not docker exec."""
        mgr = DevEnvironmentManager(
            config=DevEnvironmentConfig(repo_path=str(tmp_path), use_docker=False),
            settings_manager=DevEnvSettingsManager(data_dir=tmp_path),
        )
        self._register(mgr, 12345)
        with patch.object(
            DevEnvironmentManager, "_port_is_listening", return_value=True
        ) as host_probe, patch.object(
            DevEnvironmentManager, "_container_port_open"
        ) as probe:
            assert mgr.is_serving("T-1", "kanboard") is True
        host_probe.assert_called_once_with(12345)
        probe.assert_not_called()

    def test_container_port_open_true_on_zero_exit(self, docker_manager) -> None:
        """_container_port_open: docker exec probe returncode 0 → listening."""
        with patch(
            "src.core.dev_environment.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as mock_run:
            assert DevEnvironmentManager._container_port_open("c") is True
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["docker", "exec", "c"]
        # Probes the LISTEN state (0A) on port 3000 (hex 0BB8) via /proc/net/tcp.
        assert "0BB8" in cmd[-1]
        assert "/proc/net/tcp" in cmd[-1]

    def test_container_port_open_false_on_nonzero_exit(self, docker_manager) -> None:
        """No LISTEN row (still building) → returncode 1 → not serving."""
        with patch(
            "src.core.dev_environment.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            assert DevEnvironmentManager._container_port_open("c") is False

    def test_container_port_open_false_on_timeout(self, docker_manager) -> None:
        """A hung docker exec must fail closed, not raise."""
        with patch(
            "src.core.dev_environment.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=60),
        ):
            assert DevEnvironmentManager._container_port_open("c") is False

    def test_container_port_open_false_on_docker_error(self, docker_manager) -> None:
        """docker binary missing / daemon down → fail closed."""
        with patch(
            "src.core.dev_environment.subprocess.run",
            side_effect=OSError("docker not found"),
        ):
            assert DevEnvironmentManager._container_port_open("c") is False


class TestLastCommand:
    """Tests for get_last_command() — the resolved dev-server command shown
    on the 'Preview could not start' page."""

    def test_none_by_default(self) -> None:
        """No command is recorded until a dev environment is started."""
        mgr = DevEnvironmentManager(DevEnvironmentConfig())
        assert mgr.get_last_command("1", "kanboard") is None

    def test_returns_stored_command(self) -> None:
        """The last resolved command is returned per provider:ticket key, and
        survives the env being pruned (it lives in its own map)."""
        mgr = DevEnvironmentManager(DevEnvironmentConfig())
        mgr._last_command["kanboard:7"] = "flask run --host 0.0.0.0 --port 3000"
        assert (
            mgr.get_last_command("7", "kanboard")
            == "flask run --host 0.0.0.0 --port 3000"
        )
        # Different ticket → still None.
        assert mgr.get_last_command("8", "kanboard") is None
