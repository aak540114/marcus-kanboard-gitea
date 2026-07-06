"""
Per-ticket hot-reload development environment management.

When a human wants to see live changes for a ticket branch before
accepting it, they can trigger a dev environment.  This module starts
a Docker container (or a local process) that runs the application from
the ticket branch with hot-reload, and returns the URL.

The environment shuts down automatically when:
- The ticket is accepted (branch merged to main).
- The environment has been idle (no HTTP requests) for *idle_timeout* seconds.
- ``stop()`` is called explicitly (e.g. human clicks "Stop Preview").

A ``PortAllocator`` picks free TCP ports so multiple ticket envs can
run concurrently without collisions.

Project-type detection
----------------------
When ``DevEnvironmentConfig.auto_detect`` is ``True`` (the default), the
manager inspects the repo root for well-known project files (``package.json``,
``requirements.txt``, ``Cargo.toml``, etc.) and picks the matching Docker
image + start command automatically.  For stacks that have native hot-reload
(Vite/webpack, uvicorn ``--reload``, Django runserver, cargo-watch, …) the
start command handles file watching itself.  For stacks without it an
``inotifywait`` wrapper restarts the process on every file change.

Set ``auto_detect=False`` and supply ``docker_image`` + ``dev_command``
manually to override everything.

Classes
-------
PortAllocator
    Allocates and tracks ephemeral TCP ports.
DevEnvironmentConfig
    Configuration for the manager.
DevEnvironmentInfo
    Runtime info for one running environment.
DevEnvironmentManager
    Starts, stops, and tracks per-ticket dev environments.
"""

import asyncio
import logging
import os
import random
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_PORT_RANGE = (9100, 9900)
_DEFAULT_IDLE_TIMEOUT = 4 * 3600  # 4 hours

# ---------------------------------------------------------------------------
# Project-type detection
# ---------------------------------------------------------------------------

#: Maps stack name → Docker image, setup command, start command, and whether
#: the start command handles hot-reload itself.  All stacks expose the app on
#: container port **3000**; the host port is allocated by PortAllocator.
STACK_CONFIGS: Dict[str, Dict[str, Any]] = {
    "nodejs": {
        "image": "node:lts-alpine",
        "setup": "npm install",
        "start": "npm run dev -- --port 3000",
        "native_reload": True,  # Vite / webpack HMR
    },
    "python-fastapi": {
        "image": "python:3.12-slim",
        "setup": "pip install -r requirements.txt",
        "start": "uvicorn main:app --reload --host 0.0.0.0 --port 3000",
        "native_reload": True,  # uvicorn --reload
    },
    "python-flask": {
        "image": "python:3.12-slim",
        "setup": "pip install -r requirements.txt",
        "start": "flask run --host 0.0.0.0 --port 3000",
        "native_reload": True,  # FLASK_DEBUG=1 enables auto-reload
        "env": {"FLASK_DEBUG": "1", "FLASK_APP": "app.py"},
    },
    "python-django": {
        "image": "python:3.12-slim",
        "setup": "pip install -r requirements.txt",
        "start": "python manage.py runserver 0.0.0.0:3000",
        "native_reload": True,  # Django runserver auto-reloads
    },
    "python": {
        "image": "python:3.12-slim",
        "setup": "pip install -r requirements.txt 2>/dev/null || true",
        "start": "python -m http.server 3000",
        "native_reload": False,  # wrapped with inotifywait
    },
    "rust": {
        "image": "rust:latest",
        "setup": "cargo install cargo-watch",
        "start": "cargo watch -x run",
        "native_reload": True,  # cargo-watch
    },
    "go": {
        "image": "golang:latest",
        "setup": "go install github.com/air-verse/air@latest",
        "start": "$(go env GOPATH)/bin/air",
        "native_reload": True,  # air
    },
    "ruby": {
        "image": "ruby:latest",
        "setup": "gem install rerun && bundle install 2>/dev/null || true",
        "start": "rerun --pattern '**/*.rb' -- bundle exec ruby app.rb -p 3000",
        "native_reload": True,  # rerun wraps restart
    },
    "java": {
        "image": "maven:latest",
        "setup": "mvn dependency:resolve -q 2>/dev/null || true",
        "start": "mvn spring-boot:run -Dspring-boot.run.jvmArguments='-Dserver.port=3000'",
        "native_reload": False,
    },
    "php": {
        "image": "php:cli",
        "setup": "composer install 2>/dev/null || true",
        "start": "php -S 0.0.0.0:3000",
        "native_reload": False,  # wrapped with inotifywait
    },
    "static": {
        "image": "python:3.12-slim",
        "setup": "",
        "start": "python -m http.server 3000",
        "native_reload": False,
    },
}

#: Shell snippet that installs inotify-tools on Alpine or Debian-family images.
_INOTIFY_INSTALL = (
    "apk add --no-cache inotify-tools 2>/dev/null || "
    "apt-get install -y --no-install-recommends inotify-tools 2>/dev/null || "
    "true"
)


def detect_project_type(repo_path: str) -> str:
    """Detect the language/framework from well-known project files.

    Parameters
    ----------
    repo_path : str
        Root of the git repository to inspect.

    Returns
    -------
    str
        A key from :data:`STACK_CONFIGS`.  Falls back to ``"static"`` when no
        known project file is found.
    """
    root = Path(repo_path)

    if (root / "package.json").exists():
        return "nodejs"

    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        req_text = ""
        req_file = root / "requirements.txt"
        if req_file.exists():
            try:
                req_text = req_file.read_text(errors="replace").lower()
            except OSError:
                pass
        if "fastapi" in req_text or "uvicorn" in req_text:
            return "python-fastapi"
        if "flask" in req_text:
            return "python-flask"
        if (root / "manage.py").exists():
            return "python-django"
        return "python"

    if (root / "Cargo.toml").exists():
        return "rust"

    if (root / "go.mod").exists():
        return "go"

    if (root / "Gemfile").exists():
        return "ruby"

    if (
        (root / "pom.xml").exists()
        or (root / "build.gradle").exists()
        or (root / "build.gradle.kts").exists()
    ):
        return "java"

    if (root / "composer.json").exists():
        return "php"

    return "static"


class PortAllocator:
    """Finds and reserves available TCP ports.

    Parameters
    ----------
    port_range : tuple[int, int]
        Inclusive (low, high) range of candidate ports.
    """

    def __init__(self, port_range: Tuple[int, int] = _DEFAULT_PORT_RANGE) -> None:
        """Initialise with a port range."""
        self._low, self._high = port_range
        self._in_use: Set[int] = set()

    def allocate(self) -> int:
        """Return a free port and mark it as in-use.

        Returns
        -------
        int
            A TCP port that is currently not listening.

        Raises
        ------
        RuntimeError
            If no free port is available in the configured range.
        """
        candidates = list(range(self._low, self._high + 1))
        random.shuffle(candidates)
        for port in candidates:
            if port in self._in_use:
                continue
            if self._is_free(port):
                self._in_use.add(port)
                return port
        raise RuntimeError(f"No free port available in range {self._low}–{self._high}")

    def release(self, port: int) -> None:
        """Release a previously allocated port.

        Parameters
        ----------
        port : int
            Port to release.
        """
        self._in_use.discard(port)

    @staticmethod
    def _is_free(port: int) -> bool:
        """Return True if *port* is not listening on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            return sock.connect_ex(("127.0.0.1", port)) != 0


@dataclass
class DevEnvironmentConfig:
    """Configuration for DevEnvironmentManager.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository.
    auto_detect : bool
        When ``True`` (default) inspect ``repo_path`` for well-known project
        files and choose the Docker image + start command automatically.
        Set to ``False`` and supply ``docker_image`` / ``dev_command`` to
        override.
    docker_image : str
        Docker image used when ``auto_detect=False``.
    host : str
        Bind address for the dev server.  Defaults to ``"localhost"``.
    idle_timeout : int
        Seconds of inactivity before the container is stopped automatically.
    port_range : tuple
        Candidate port range for ``PortAllocator``.
    use_docker : bool
        When ``True`` (default) use Docker.  When ``False`` use a local
        process (useful for CI).
    dev_command : str
        Shell command used when ``auto_detect=False`` and ``use_docker=True``,
        or always when ``use_docker=False``.  The placeholder ``{port}`` is
        replaced with the allocated port number.
    env_vars : Dict[str, str]
        Extra environment variables injected into the container / process.
        When ``auto_detect=True`` these are merged with the stack's own env.
    """

    repo_path: str = field(default_factory=os.getcwd)
    auto_detect: bool = True
    docker_image: str = "node:lts-alpine"
    host: str = "localhost"
    idle_timeout: int = _DEFAULT_IDLE_TIMEOUT
    port_range: Tuple[int, int] = _DEFAULT_PORT_RANGE
    use_docker: bool = True
    dev_command: str = "npm run dev -- --port {port}"
    env_vars: Dict[str, str] = field(default_factory=dict)


@dataclass
class DevEnvironmentInfo:
    """Runtime information about a running dev environment.

    Parameters
    ----------
    ticket_id : str
        Provider ticket identifier.
    provider : str
        Kanban provider name.
    branch_name : str
        Git branch the environment is running.
    port : int
        TCP port.
    url : str
        Full URL to access the environment.
    container_name : str
        Docker container name (or process label).
    started_at : datetime
        When the environment was started.
    process : Optional[subprocess.Popen]
        The running process (only set when ``use_docker=False``).
    """

    ticket_id: str
    provider: str
    branch_name: str
    port: int
    url: str
    container_name: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    process: Optional[subprocess.Popen] = None  # type: ignore[type-arg]


class DevEnvironmentManager:
    """Manages per-ticket hot-reload development environments.

    Parameters
    ----------
    config : Optional[DevEnvironmentConfig]
        Configuration; uses defaults if not provided.
    """

    def __init__(self, config: Optional[DevEnvironmentConfig] = None) -> None:
        """Initialise the manager."""
        self.config = config or DevEnvironmentConfig()
        self._allocator = PortAllocator(self.config.port_range)
        self._envs: Dict[str, DevEnvironmentInfo] = (
            {}
        )  # key = f"{provider}:{ticket_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(
        self,
        ticket_id: str,
        provider: str,
        branch_name: str,
    ) -> DevEnvironmentInfo:
        """Start a dev environment for *branch_name*.

        If an environment is already running for this ticket, the
        existing one is returned without starting a new one.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.
        branch_name : str
            Git branch to run.

        Returns
        -------
        DevEnvironmentInfo
            Info about the running environment.
        """
        key = f"{provider}:{ticket_id}"
        if key in self._envs:
            logger.info(
                "Dev env for %s already running on port %d", key, self._envs[key].port
            )
            return self._envs[key]

        port = self._allocator.allocate()
        container_name = f"marcus-dev-{provider}-{ticket_id.lower().replace('/', '-')}"
        url = f"http://{self.config.host}:{port}"

        if self.config.use_docker:
            info = await self._start_docker(
                ticket_id, provider, branch_name, port, container_name, url
            )
        else:
            info = await self._start_local(
                ticket_id, provider, branch_name, port, container_name, url
            )

        self._envs[key] = info
        logger.info("Dev env started for %s at %s", key, url)
        return info

    async def stop(self, ticket_id: str, provider: str) -> bool:
        """Stop the dev environment for a ticket.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        bool
            ``True`` if an environment was running and was stopped.
        """
        key = f"{provider}:{ticket_id}"
        info = self._envs.pop(key, None)
        if info is None:
            return False

        if self.config.use_docker:
            await self._stop_docker(info.container_name)
        else:
            await self._stop_local(info)

        self._allocator.release(info.port)
        logger.info("Dev env stopped for %s", key)
        return True

    def get_info(self, ticket_id: str, provider: str) -> Optional[DevEnvironmentInfo]:
        """Return info about a running dev environment, or ``None``.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        Optional[DevEnvironmentInfo]
            Running environment info, or ``None``.
        """
        return self._envs.get(f"{provider}:{ticket_id}")

    def list_running(self) -> List[DevEnvironmentInfo]:
        """Return all currently running dev environments."""
        return list(self._envs.values())

    async def stop_all(self) -> None:
        """Stop all running dev environments (called on shutdown)."""
        keys = list(self._envs.keys())
        for key in keys:
            provider, ticket_id = key.split(":", 1)
            await self.stop(ticket_id, provider)

    # ------------------------------------------------------------------
    # Docker implementation
    # ------------------------------------------------------------------

    def _build_entrypoint(self, branch_name: str, stack: str) -> str:
        """Build the shell command run inside the Docker container.

        Parameters
        ----------
        branch_name : str
            Git branch to check out before starting.
        stack : str
            Key from :data:`STACK_CONFIGS`.

        Returns
        -------
        str
            A ``sh -c`` compatible shell command string.
        """
        cfg = STACK_CONFIGS[stack]
        setup: str = cfg["setup"]
        start: str = cfg["start"]

        steps = [f"git checkout {branch_name}"]
        if setup:
            steps.append(setup)

        if cfg["native_reload"]:
            steps.append(start)
            return " && ".join(steps)

        # Wrap with inotifywait so any file change triggers a process restart.
        setup_part = " && ".join(steps)
        return (
            f"{setup_part} && "
            f"{_INOTIFY_INSTALL} && "
            f"{start} & APP_PID=$! && "
            f"while inotifywait -e modify,create,delete,move -r /app "
            f"--exclude '\\.git' --quiet 2>/dev/null; do "
            f"echo '[marcus] File changed — restarting...'; "
            f"kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null; "
            f"{start} & APP_PID=$!; "
            f"done"
        )

    async def _start_docker(
        self,
        ticket_id: str,
        provider: str,
        branch_name: str,
        port: int,
        container_name: str,
        url: str,
    ) -> DevEnvironmentInfo:
        """Launch a Docker container for the ticket branch."""
        if self.config.auto_detect:
            stack = detect_project_type(self.config.repo_path)
            stack_cfg = STACK_CONFIGS[stack]
            image = stack_cfg["image"]
            entrypoint = self._build_entrypoint(branch_name, stack)
            stack_env: Dict[str, str] = stack_cfg.get("env", {})
            logger.info(
                "Detected project type %r for %s; using image %s", stack, branch_name, image
            )
        else:
            image = self.config.docker_image
            entrypoint = (
                f"git checkout {branch_name} && "
                f"{self.config.dev_command.format(port=3000)}"
            )
            stack_env = {}

        all_env = {**stack_env, **self.config.env_vars}
        env_args: List[str] = []
        for k, v in all_env.items():
            env_args += ["-e", f"{k}={v}"]

        cmd = (
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                container_name,
                "-p",
                f"{port}:3000",
                "-v",
                f"{self.config.repo_path}:/app",
                "-w",
                "/app",
            ]
            + env_args
            + [
                image,
                "sh",
                "-c",
                entrypoint,
            ]
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True),
        )
        if result.returncode != 0:
            self._allocator.release(port)
            raise RuntimeError(f"Docker container start failed: {result.stderr[:400]}")

        return DevEnvironmentInfo(
            ticket_id=ticket_id,
            provider=provider,
            branch_name=branch_name,
            port=port,
            url=url,
            container_name=container_name,
        )

    async def _stop_docker(self, container_name: str) -> None:
        """Stop and remove a Docker container (best-effort)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True,
            ),
        )

    # ------------------------------------------------------------------
    # Local process implementation
    # ------------------------------------------------------------------

    async def _start_local(
        self,
        ticket_id: str,
        provider: str,
        branch_name: str,
        port: int,
        container_name: str,
        url: str,
    ) -> DevEnvironmentInfo:
        """Start a local dev process for the ticket branch."""
        cmd_str = self.config.dev_command.format(port=port)
        env = dict(os.environ, PORT=str(port), **self.config.env_vars)

        loop = asyncio.get_event_loop()

        async def _spawn() -> subprocess.Popen:  # type: ignore[type-arg]
            return await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd_str,
                    shell=True,  # nosec B602
                    cwd=self.config.repo_path,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
            )

        process = await _spawn()

        return DevEnvironmentInfo(
            ticket_id=ticket_id,
            provider=provider,
            branch_name=branch_name,
            port=port,
            url=url,
            container_name=container_name,
            process=process,
        )

    async def _stop_local(self, info: DevEnvironmentInfo) -> None:
        """Terminate a local dev process."""
        if info.process and info.process.poll() is None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, info.process.terminate)
