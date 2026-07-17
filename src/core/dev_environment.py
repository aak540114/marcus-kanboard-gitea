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
import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.dev_env_settings import DevEnvSettingsManager

logger = logging.getLogger(__name__)

_DEFAULT_PORT_RANGE = (9100, 9900)
_DEFAULT_IDLE_TIMEOUT = 4 * 3600  # 4 hours

#: Hard ceiling on every `docker run`/`docker exec`/`docker stop` CLI call.
#: Without this, an unresponsive Docker daemon (docker.sock permission
#: wedge, daemon restarting) would block the asyncio executor thread
#: handling the call indefinitely — including calls triggered by an
#: incoming Gitea webhook POST, which would then hang that HTTP request
#: (and, once the shared thread pool is exhausted, degrade unrelated
#: run_in_executor work elsewhere in Marcus) rather than failing fast.
_DOCKER_CMD_TIMEOUT = 60

#: Marker file the entrypoint script touches immediately after its own
#: initial `git checkout` succeeds (see `_build_entrypoint`). `refresh()`
#: polls for this before running its own `git fetch`/`reset --hard`
#: against the same container — `docker run -d` returns as soon as the
#: container starts, not once that entrypoint has actually finished
#: installing `git` (via apt-get) and checking out the branch, so a
#: webhook-triggered refresh() arriving in that window could otherwise
#: race the entrypoint's own git operations against the same working tree.
_READY_MARKER = "/tmp/.marcus-ready"
_READY_POLL_INTERVAL = 2.0
_READY_POLL_MAX_ATTEMPTS = 5

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


def _resolve_host_repo_path(repo_path: str) -> str:
    """Translate a Marcus-container repo path to the Docker HOST path.

    Marcus itself may run inside a container (Docker-outside-of-Docker):
    its ``docker run -v`` calls reach the HOST's Docker daemon through a
    mounted ``/var/run/docker.sock``, so a bind-mount *source* path must
    be a real host filesystem path — a path inside Marcus's own container
    namespace (e.g. ``/app/data/repos/x``, from Marcus's own
    ``./data:/app/data`` bind mount) does not exist there.

    ``MARCUS_HOST_PROJECT_ROOT`` is set by ``docker-compose.yml`` to
    ``${PWD}`` at ``docker compose up`` time — the host directory
    Marcus's own ``./data`` is mounted from. When set, this translates
    ``/app/data/...`` or ``./data/...`` (relative to Marcus's own CWD,
    which is ``/app``) to ``{MARCUS_HOST_PROJECT_ROOT}/data/...``. Left
    as-is when unset (local/non-Docker ``./marcus start``, or tests).

    Parameters
    ----------
    repo_path : str
        Repo path as Marcus's own process sees it.

    Returns
    -------
    str
        The equivalent path on the Docker host.
    """
    host_root = os.environ.get("MARCUS_HOST_PROJECT_ROOT")
    if not host_root:
        return repo_path

    normalized = repo_path
    if normalized.startswith("/app/"):
        normalized = normalized[len("/app/") :]
    elif normalized.startswith("./"):
        normalized = normalized[2:]
    elif os.path.isabs(normalized):
        # Absolute path outside /app — nothing we know how to translate;
        # use as-is (matches pre-DooD behaviour, may not exist on host).
        return repo_path

    return os.path.join(host_root, normalized)


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
    settings_manager : Optional[DevEnvSettingsManager]
        Source of the global max-parallel-containers limit; uses defaults
        (unlimited unless configured via the Kanboard UI) if not provided.
    """

    def __init__(
        self,
        config: Optional[DevEnvironmentConfig] = None,
        settings_manager: Optional[DevEnvSettingsManager] = None,
    ) -> None:
        """Initialise the manager."""
        self.config = config or DevEnvironmentConfig()
        self._allocator = PortAllocator(self.config.port_range)
        self._settings = settings_manager or DevEnvSettingsManager()
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
        repo_path: Optional[str] = None,
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
        repo_path : Optional[str]
            Repo path for THIS ticket, overriding ``self.config.repo_path``
            for this call only.  Required for correctness when one manager
            instance serves many tickets across different projects/repos
            (``self.config.repo_path`` is fixed at construction and shared).

        Returns
        -------
        DevEnvironmentInfo
            Info about the running environment.

        Raises
        ------
        RuntimeError
            If the configured max-parallel-containers limit has been
            reached and no environment is already running for this ticket.
        """
        key = f"{provider}:{ticket_id}"
        if key in self._envs:
            logger.info(
                "Dev env for %s already running on port %d", key, self._envs[key].port
            )
            return self._envs[key]

        limit = self._settings.get_max_parallel_containers()
        if limit is not None and len(self._envs) >= limit:
            raise RuntimeError(
                f"Max parallel dev environments ({limit}) reached — stop an "
                "existing one before starting a new one."
            )

        port = self._allocator.allocate()
        container_name = f"marcus-dev-{provider}-{ticket_id.lower().replace('/', '-')}"
        url = f"http://{self.config.host}:{port}"
        effective_repo_path = repo_path or self.config.repo_path

        if self.config.use_docker:
            info = await self._start_docker(
                ticket_id, provider, branch_name, port, container_name, url,
                project_stack=project_stack, repo_path=effective_repo_path,
            )
        else:
            info = await self._start_local(
                ticket_id, provider, branch_name, port, container_name, url,
                repo_path=effective_repo_path,
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
            ``False`` if nothing was running, OR if a running environment
            could not be confirmed stopped (e.g. a `docker stop` timeout)
            — bookkeeping is deliberately left intact in that case so a
            retried ``stop()`` (or the next status check) can still find
            it, rather than silently forgetting about a container that
            may still be running and freeing its port/slot for reuse,
            which would collide with the real container's name on the
            next ``start()`` for the same ticket.
        """
        key = f"{provider}:{ticket_id}"
        info = self._envs.get(key)
        if info is None:
            return False

        if self.config.use_docker:
            stopped = await self._stop_docker(info.container_name)
        else:
            await self._stop_local(info)
            stopped = True

        if not stopped:
            logger.warning(
                "Dev env stop unconfirmed for %s — leaving it tracked as "
                "running so a retry can be attempted",
                key,
            )
            return False

        del self._envs[key]
        self._allocator.release(info.port)
        logger.info("Dev env stopped for %s", key)
        return True

    async def _wait_until_ready(self, container_name: str) -> bool:
        """Poll for the entrypoint's post-checkout readiness marker.

        Guards against a race between :meth:`refresh`'s own git commands
        and the container's initial ``git checkout`` (see
        :meth:`_build_entrypoint`) — ``docker run -d`` returns as soon as
        the container starts, not once that entrypoint script has
        actually finished installing ``git`` and checking out the
        branch. A webhook-triggered refresh arriving in that window could
        otherwise run ``git fetch``/``reset --hard`` concurrently with
        (or before) the entrypoint's own checkout against the same
        working tree.

        Parameters
        ----------
        container_name : str
            Name of the running container to check.

        Returns
        -------
        bool
            ``True`` once the marker is found. ``False`` if it never
            appears within the poll budget, or a check itself times out.
        """
        check_cmd = [
            "docker",
            "exec",
            container_name,
            "sh",
            "-c",
            f"test -f {_READY_MARKER}",
        ]
        loop = asyncio.get_event_loop()
        for attempt in range(_READY_POLL_MAX_ATTEMPTS):
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        check_cmd, capture_output=True, timeout=_DOCKER_CMD_TIMEOUT
                    ),
                )
            except subprocess.TimeoutExpired:
                return False
            if result.returncode == 0:
                return True
            if attempt < _READY_POLL_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_READY_POLL_INTERVAL)
        return False

    async def refresh(self, ticket_id: str, provider: str) -> bool:
        """Pull the latest branch commit into a running dev-environment container.

        Runs ``git fetch origin && git reset --hard origin/<branch>`` inside
        the container via ``docker exec``. The container's ``/app`` is
        bind-mounted from the same host path Marcus/GiteaManager use to
        manage the repo, and the container's own inotify-restart-loop / HMR
        watcher (see :meth:`_build_entrypoint`) picks up the file change
        automatically — this method only needs to trigger the pull, not
        implement any new reload logic. Intended to be called from the
        Gitea push-webhook handler for instant refresh.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        bool
            ``True`` if a running Docker environment was found and
            refreshed. ``False`` if no environment is running for this
            ticket, the environment is a local (non-Docker) process, or
            the git commands failed inside the container.
        """
        key = f"{provider}:{ticket_id}"
        info = self._envs.get(key)
        if info is None:
            return False

        if not self.config.use_docker:
            logger.debug("refresh() is a no-op for non-Docker dev environments")
            return False

        if not await self._wait_until_ready(info.container_name):
            logger.warning(
                "Dev env refresh skipped for %s: container not ready yet "
                "(still installing dependencies / checking out its branch)",
                key,
            )
            return False

        ref = shlex.quote(f"origin/{info.branch_name}")
        cmd = [
            "docker",
            "exec",
            info.container_name,
            "sh",
            "-c",
            f"git fetch origin && git reset --hard {ref}",
        ]
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True, timeout=_DOCKER_CMD_TIMEOUT
                ),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Dev env refresh timed out for %s after %ds (Docker daemon "
                "unresponsive?)",
                key,
                _DOCKER_CMD_TIMEOUT,
            )
            return False
        if result.returncode != 0:
            logger.warning("Dev env refresh failed for %s: %s", key, result.stderr[:400])
            return False

        logger.info("Dev env refreshed for %s (branch %s)", key, info.branch_name)
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

    async def reconcile_orphans(self) -> int:
        """Remove ``marcus-dev-*`` containers left over from a dead run.

        The environment registry (``_envs``) is in-memory only: after a
        Marcus crash or restart, containers started by the previous
        process are orphaned — no idle timeout reaps them, their ports
        stay held, and the next ``start()`` for the same ticket dies on
        a docker name collision with a misleading "check that Docker is
        running" error comment. Called at workflow startup (when the
        registry is empty, so every matching container is by definition
        an orphan); containers belonging to a currently registered env
        are left alone, making the call safe at any time.

        Returns
        -------
        int
            Number of containers removed. ``0`` on any docker failure —
            reconciliation is best-effort and must never block startup.
        """
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "ps", "-aq", "--filter", "name=marcus-dev-"],
                    capture_output=True,
                    text=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - daemon down, binary missing
            logger.warning("Dev-env orphan scan failed: %s", exc)
            return 0
        if result.returncode != 0 or not result.stdout.strip():
            return 0

        container_ids = [
            line.strip() for line in result.stdout.splitlines() if line.strip()
        ]
        known_names = {
            info.container_name for info in self._envs.values()
        }
        if known_names:
            # Resolve ids → names so registered (live) envs are spared.
            try:
                inspect = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        ["docker", "inspect", "--format", "{{.Name}}"]
                        + container_ids,
                        capture_output=True,
                        text=True,
                        timeout=_DOCKER_CMD_TIMEOUT,
                    ),
                )
                names = [
                    n.strip().lstrip("/")
                    for n in inspect.stdout.splitlines()
                    if n.strip()
                ]
                container_ids = [
                    cid
                    for cid, name in zip(container_ids, names)
                    if name not in known_names
                ]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Dev-env orphan inspect failed: %s", exc)
                return 0
        if not container_ids:
            return 0

        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "rm", "-f"] + container_ids,
                    capture_output=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dev-env orphan removal failed: %s", exc)
            return 0
        logger.info(
            "Removed %d orphaned dev-environment container(s): %s",
            len(container_ids),
            ", ".join(container_ids),
        )
        return len(container_ids)

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

        # branch_name is interpreted as shell syntax by the `sh -c` this
        # string is eventually passed to inside the container — quote it
        # so shell metacharacters in an unsanitized caller's input can't
        # break out of `git checkout` into arbitrary command execution.
        #
        # The `touch _READY_MARKER` immediately after checkout lets
        # refresh() (see _wait_until_ready) confirm the initial checkout
        # has actually happened before running its own git commands
        # against /app — apt-get above can take tens of seconds, and
        # `docker run -d` returns long before this script reaches here.
        steps = [
            apt_line,
            f"git checkout {shlex.quote(branch_name)}",
            f"touch {_READY_MARKER}",
        ]
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
        repo_path: str,
        project_stack: "Optional[Any]" = None,
    ) -> DevEnvironmentInfo:
        """Launch a Docker container for the ticket branch.

        Parameters
        ----------
        repo_path : str
            Repo path as Marcus's own process sees it (used for stack
            auto-detection). Translated to a HOST path via
            :func:`_resolve_host_repo_path` for the ``docker run -v``
            mount source, since ``docker run`` is executed against the
            HOST's Docker daemon (Docker-outside-of-Docker), not a path
            inside Marcus's own container.
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
            stack_key = detect_project_type(repo_path)
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
                f"{_resolve_host_repo_path(repo_path)}:/app",
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
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True, timeout=_DOCKER_CMD_TIMEOUT
                ),
            )
        except subprocess.TimeoutExpired:
            self._allocator.release(port)
            raise RuntimeError(
                f"Docker container start timed out after {_DOCKER_CMD_TIMEOUT}s "
                "(Docker daemon unresponsive?)"
            ) from None
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

    async def _stop_docker(self, container_name: str) -> bool:
        """Stop and remove a Docker container (best-effort).

        Returns
        -------
        bool
            ``True`` if the stop command completed (regardless of exit
            code — a container that was already gone still means nothing
            is left running). ``False`` only on timeout, meaning the
            container's true state is unknown — the caller must NOT
            assume it's actually stopped.
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "stop", container_name],
                    capture_output=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                "docker stop timed out for %s after %ds (Docker daemon "
                "unresponsive?) — container may still be running",
                container_name,
                _DOCKER_CMD_TIMEOUT,
            )
            return False

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
        repo_path: str,
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
                    cwd=repo_path,
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
