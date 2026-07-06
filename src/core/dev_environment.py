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

Stack selection order
---------------------
1. Caller supplies a :class:`~src.core.project_description.ProjectStack`
   (derived from the project's description document) — preferred path.
2. ``auto_detect=True`` (the default) falls back to sniffing the repo root
   for well-known project files (``package.json``, ``requirements.txt``, …).
3. ``auto_detect=False`` with explicit ``docker_image`` / ``dev_command``
   overrides everything.

All stacks use **``debian:bookworm-slim``** as the base Docker image so a
single fast image covers any language.  Runtime tools (Python, Node.js, Go,
…) are installed by the generated entrypoint script using ``apt-get``.

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

#: Single base image for all dev environments.  ``debian:bookworm-slim``
#: starts in under a second, ships ``apt-get`` so any runtime can be
#: installed, and is ~75 MB — faster than any language-specific full image.
_BASE_IMAGE = "debian:bookworm-slim"

#: apt packages always installed in the base layer before the project setup.
_BASE_APT = "git curl inotify-tools build-essential ca-certificates"

# ---------------------------------------------------------------------------
# Fallback stack table — used when no ProjectStack is supplied and
# auto_detect=True sniffs well-known project files from the repo root.
# ---------------------------------------------------------------------------

#: Maps detected stack key → (install_cmd, dev_cmd, use_hm_reload).
#: ``use_hm_reload`` is True only for stacks where killing the process on
#: each file save would break browser-side hot-module state (Node.js/Vite)
#: or interrupt an incremental compile cycle (cargo-watch, air).
_FALLBACK_STACKS: Dict[str, Dict[str, Any]] = {
    "nodejs":         {"install": "npm install",
                       "start":   "npm run dev -- --port 3000",
                       "hm":      True},
    "python-fastapi": {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "uvicorn main:app --host 0.0.0.0 --port 3000",
                       "hm":      False},
    "python-flask":   {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "flask run --host 0.0.0.0 --port 3000",
                       "hm":      False},
    "python-django":  {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "python manage.py runserver 0.0.0.0:3000 --noreload",
                       "hm":      False},
    "python":         {"install": "pip install --no-cache-dir -r requirements.txt 2>/dev/null || true",
                       "start":   "python3 -m http.server 3000",
                       "hm":      False},
    "rust":           {"install": "cargo install cargo-watch",
                       "start":   "cargo watch -x run",
                       "hm":      True},
    "go":             {"install": "go install github.com/air-verse/air@latest",
                       "start":   "$(go env GOPATH)/bin/air",
                       "hm":      True},
    "ruby":           {"install": "bundle install 2>/dev/null || true",
                       "start":   "bundle exec ruby app.rb -p 3000 2>/dev/null || ruby app.rb -p 3000",
                       "hm":      False},
    "java":           {"install": "mvn dependency:resolve -q 2>/dev/null || true",
                       "start":   "mvn spring-boot:run -Dspring-boot.run.jvmArguments='-Dserver.port=3000'",
                       "hm":      False},
    "php":            {"install": "composer install 2>/dev/null || true",
                       "start":   "php -S 0.0.0.0:3000",
                       "hm":      False},
    "static":         {"install": "",
                       "start":   "python3 -m http.server 3000",
                       "hm":      False},
}

# Keep public alias so existing imports don't break while we migrate callers.
STACK_CONFIGS = _FALLBACK_STACKS


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
        project_stack: "Optional[Any]" = None,
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
        project_stack : Optional[ProjectStack]
            Tech-stack parsed from the project description.  Overrides
            file-based detection when supplied.

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
                ticket_id, provider, branch_name, port, container_name, url,
                project_stack=project_stack,
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

    def _build_entrypoint(
        self,
        branch_name: str,
        install_cmd: str,
        start_cmd: str,
        use_hm_reload: bool,
        extra_apt: Optional[List[str]] = None,
    ) -> str:
        """Build the shell command run inside the Docker container.

        Parameters
        ----------
        branch_name : str
            Git branch to check out before starting.
        install_cmd : str
            Command that installs project dependencies (may be empty).
        start_cmd : str
            Command that starts the dev server on port 3000.
        use_hm_reload : bool
            When ``True`` the start command handles its own hot-reload
            (Node.js/Vite, cargo-watch, air) and must NOT be killed on
            file changes.  When ``False`` an ``inotifywait`` restart loop
            is used.
        extra_apt : Optional[List[str]]
            Additional ``apt-get install`` package names beyond the base set.

        Returns
        -------
        str
            A ``sh -c`` compatible shell command string.
        """
        apt_extras = " ".join(extra_apt) if extra_apt else ""
        apt_line = (
            f"apt-get update -qq && apt-get install -y --no-install-recommends "
            f"{_BASE_APT}{' ' + apt_extras if apt_extras else ''}"
        )

        steps = [apt_line, f"git checkout {branch_name}"]
        if install_cmd:
            steps.append(install_cmd)

        if use_hm_reload:
            steps.append(start_cmd)
            return " && ".join(steps)

        # inotifywait restart loop for interpreted / non-HMR stacks.
        setup_part = " && ".join(steps)
        return (
            f"{setup_part} && "
            f"{start_cmd} & APP_PID=$! && "
            f"while inotifywait -e modify,create,delete,move -r /app "
            f"--exclude '\\.git' --quiet 2>/dev/null; do "
            f"echo '[marcus] File changed — restarting...'; "
            f"kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null; "
            f"{start_cmd} & APP_PID=$!; "
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
        project_stack: "Optional[Any]" = None,
    ) -> DevEnvironmentInfo:
        """Launch a Docker container for the ticket branch.

        Parameters
        ----------
        project_stack : Optional[ProjectStack]
            Tech-stack parsed from the project description.  When supplied
            this takes priority over file-based detection.
        """
        # ── Resolve install/start commands ──────────────────────────────
        extra_apt: List[str] = []
        if project_stack is not None:
            # Primary path: stack from project description
            install_cmd: str = project_stack.install_cmd
            start_cmd: str = project_stack.dev_cmd
            use_hm_reload: bool = project_stack.use_hm_reload
            extra_apt = getattr(project_stack, "apt_packages", [])
            logger.info(
                "Using project-description stack %r for %s",
                project_stack.language,
                branch_name,
            )
        elif self.config.auto_detect:
            # Fallback: sniff repo root for well-known files
            stack_key = detect_project_type(self.config.repo_path)
            fb = _FALLBACK_STACKS[stack_key]
            install_cmd = fb["install"]
            start_cmd = fb["start"]
            use_hm_reload = fb["hm"]
            logger.info(
                "Auto-detected stack %r for %s (no project description)",
                stack_key,
                branch_name,
            )
        else:
            # Manual override via config
            install_cmd = ""
            start_cmd = self.config.dev_command.format(port=3000)
            use_hm_reload = False

        entrypoint = self._build_entrypoint(
            branch_name, install_cmd, start_cmd, use_hm_reload, extra_apt
        )

        env_args: List[str] = []
        for k, v in self.config.env_vars.items():
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
                _BASE_IMAGE,
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
