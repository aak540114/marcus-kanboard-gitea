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
import json
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

#: Single base image for all dev environments.  Bare ``alpine`` is ~7 MB and
#: starts near-instantly.  It deliberately ships **no** language runtime —
#: each stack installs exactly the languages, packages, and libraries it
#: needs at container start via ``apk`` (see :data:`_FALLBACK_STACKS` /
#: :meth:`ProjectStack.apk_packages`).  That keeps the image tiny and avoids
#: baking in a runtime a given project may not use.
_BASE_IMAGE = "alpine:3.20"

#: Alpine ``apk`` packages installed before project setup.  ``git`` is
#: required for the branch checkout; ``inotify-tools`` powers the file-watch
#: restart loop for non-hot-reload stacks; ``busybox-extras`` is what makes
#: :data:`_STATIC_FALLBACK` below actually work (see its docstring — the
#: base image's own BusyBox build does NOT include ``httpd``). Installation
#: is best-effort (``|| true``) so a transient network hiccup never stops
#: the container from serving.  Language runtimes are NOT here — they come
#: from the stack.
_BASE_APK = "git inotify-tools busybox-extras"

#: Port the application is expected to listen on **inside** the container.
#: The host-side port (allocated by :class:`PortAllocator`) is published to
#: this one via ``docker run -p <host>:<_APP_PORT>``.
_APP_PORT = 3000

#: Last-resort fallback server, run whenever a project's real dev command
#: exits (wrong/missing script, crash, etc.) — see :meth:`_build_entrypoint`.
#: ``alpine:3.20``'s BASE ``busybox`` package does NOT include the ``httpd``
#: applet at all (confirmed live: a container falling back to
#: ``busybox httpd`` failed with ``httpd: applet not found``, leaving the
#: preview completely unreachable instead of degrading to a static file
#: server as designed). ``busybox-extras`` (installed unconditionally by
#: :data:`_BASE_APK` for exactly this reason) provides it — but as its OWN
#: standalone ``/usr/bin/httpd`` binary, not as an applet added to the
#: original ``/bin/busybox`` multi-call binary (confirmed live a second
#: time: with busybox-extras installed, ``busybox httpd`` *still* failed
#: with the same "applet not found" — only invoking ``httpd`` directly,
#: without the ``busybox`` prefix, actually runs it). It serves the
#: checked-out branch's files (the container's ``/app`` working directory)
#: as a static website — perfect for the common case of letting a human
#: open a browser and *see* what an agent built, and as the last-resort
#: fallback when a project's real dev command can't start.
_STATIC_FALLBACK = f"httpd -f -p {_APP_PORT} -h /app"

# ---------------------------------------------------------------------------
# Fallback stack table — used when no ProjectStack is supplied and
# auto_detect=True sniffs well-known project files from the repo root.
# ---------------------------------------------------------------------------

#: Maps detected stack key → (install_cmd, dev_cmd, use_hm_reload, apk).
#: ``use_hm_reload`` is True only for stacks where killing the process on
#: each file save would break browser-side hot-module state (Node.js/Vite)
#: or interrupt an incremental compile cycle (cargo-watch, air).  ``apk`` is
#: the list of Alpine packages that install the language runtime the stack
#: needs on top of :data:`_BASE_IMAGE` (which already ships ``python3``);
#: an empty list means "the base image already has everything".
_FALLBACK_STACKS: Dict[str, Dict[str, Any]] = {
    "nodejs":         {"install": "npm install",
                       "start":   "npm run dev -- --port 3000",
                       "hm":      True,
                       "apk":     ["nodejs", "npm"]},
    "python-fastapi": {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "uvicorn main:app --host 0.0.0.0 --port 3000",
                       "hm":      False,
                       "apk":     ["python3", "py3-pip"]},
    "python-flask":   {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "flask run --host 0.0.0.0 --port 3000",
                       "hm":      False,
                       "apk":     ["python3", "py3-pip"]},
    "python-django":  {"install": "pip install --no-cache-dir -r requirements.txt",
                       "start":   "python manage.py runserver 0.0.0.0:3000 --noreload",
                       "hm":      False,
                       "apk":     ["python3", "py3-pip"]},
    "python":         {"install": "pip install --no-cache-dir -r requirements.txt 2>/dev/null || true",
                       "start":   "python3 -m http.server 3000",
                       "hm":      False,
                       "apk":     ["python3", "py3-pip"]},
    "rust":           {"install": "cargo install cargo-watch",
                       "start":   "cargo watch -x run",
                       "hm":      True,
                       "apk":     ["rust", "cargo"]},
    "go":             {"install": "go install github.com/air-verse/air@latest",
                       "start":   "$(go env GOPATH)/bin/air",
                       "hm":      True,
                       "apk":     ["go"]},
    "ruby":           {"install": "bundle install 2>/dev/null || true",
                       "start":   "bundle exec ruby app.rb -p 3000 2>/dev/null || ruby app.rb -p 3000",
                       "hm":      False,
                       "apk":     ["ruby"]},
    "java":           {"install": "mvn dependency:resolve -q 2>/dev/null || true",
                       "start":   "mvn spring-boot:run -Dspring-boot.run.jvmArguments='-Dserver.port=3000'",
                       "hm":      False,
                       "apk":     ["openjdk17", "maven"]},
    "php":            {"install": "composer install 2>/dev/null || true",
                       "start":   "php -S 0.0.0.0:3000",
                       "hm":      False,
                       "apk":     ["php"]},
    "static":         {"install": "",
                       "start":   "httpd -f -p 3000 -h /app",
                       "hm":      False,
                       "apk":     []},
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


#: npm script names tried, in priority order, when a Node.js project's
#: package.json doesn't define a "dev" script. "dev" first since it's the
#: convention Marcus's own default command already assumes (Vite, Next.js,
#: CRA, etc. all default to it); the rest cover projects that name their
#: dev-server script differently instead of not having one at all.
_NODEJS_SCRIPT_PRIORITY = ("dev", "start", "serve", "develop")


def _resolve_nodejs_dev_command(repo_path: str) -> str:
    """Pick the npm script to run as a Node.js project's dev server.

    ``detect_project_type`` returning ``"nodejs"`` only means a
    ``package.json`` exists — it says nothing about which script in it
    actually starts a dev server. Assuming "dev" unconditionally (Marcus's
    long-standing default) fails outright for any project that names it
    something else: ``npm error Missing script: "dev"`` exits immediately,
    and the human sees "Preview could not start" for a project that would
    have worked fine under its own ``start``/``serve``/``develop`` script.

    Reads ``package.json``'s ``"scripts"`` object and returns the first
    match from :data:`_NODEJS_SCRIPT_PRIORITY`, formatted the same way as
    the static fallback table's own "nodejs" entry. Falls back to that
    same default (still trying "dev") when the file is missing, unreadable,
    not valid JSON, or defines none of the candidate scripts — so a project
    a human hasn't configured behaves exactly as before this existed.

    Parameters
    ----------
    repo_path : str
        Root of the git repository to inspect.

    Returns
    -------
    str
        The ``npm run <script> -- --port {port}`` command to use as this
        stack's ``start`` command.
    """
    default: str = _FALLBACK_STACKS["nodejs"]["start"]
    package_json = Path(repo_path) / "package.json"
    try:
        data = json.loads(package_json.read_text())
    except (OSError, ValueError):
        return default

    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return default

    for name in _NODEJS_SCRIPT_PRIORITY:
        if name in scripts:
            return f"npm run {name} -- --port {_APP_PORT}"
    return default


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
        # key = f"{provider}:{ticket_id}" → the dev-server command Marcus
        # resolved (from the project's Tech Stack) and ran in the container.
        # Kept even after the env is pruned so the "Preview could not start"
        # page can show exactly what was run — the usual reason a container
        # exits is that this inferred command didn't match the app's layout.
        self._last_command: Dict[str, str] = {}
        # key = f"{provider}:{ticket_id}" → the last lines of a dead
        # container's logs, captured by prune_if_dead the moment it detects
        # the container exited (containers no longer run with --rm, so they
        # linger in 'exited' state long enough to read). Shown on the
        # "Preview could not start" page so the human can see WHY it exited.
        self._exit_logs: Dict[str, str] = {}

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
        the container via ``docker exec``. The container's ``/app`` is its
        OWN isolated clone (see the "ISOLATED CHECKOUT" comment in
        :meth:`_build_entrypoint`) — the host repo Marcus/GiteaManager use
        to manage the repo is bind-mounted read-only at ``/src`` and is
        never written to; ``/app``'s ``origin`` remote points at that
        ``/src`` mount, which is how this fetch reaches it with no network
        and no credentials. The container's own inotify-restart-loop / HMR
        watcher (see :meth:`_build_entrypoint`) picks up the file change in
        ``/app`` automatically — this method only needs to trigger the
        pull, not implement any new reload logic. Intended to be called
        from the Gitea push-webhook handler for instant refresh.

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

    def get_last_command(self, ticket_id: str, provider: str) -> Optional[str]:
        """Return the dev-server command last run for a ticket, or ``None``.

        This is the command Marcus resolved from the project's Tech Stack
        (e.g. ``flask run --host 0.0.0.0 --port 3000``) and ran in the
        container. Retained after the environment is pruned so the
        "Preview could not start" page can show what was attempted.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        Optional[str]
            The last resolved dev-server command, or ``None`` if none was
            ever started for this ticket.
        """
        return self._last_command.get(f"{provider}:{ticket_id}")

    def get_last_logs(self, ticket_id: str, provider: str) -> Optional[str]:
        """Return the exited container's captured logs for a ticket, or None.

        Populated by :meth:`prune_if_dead` when a preview container is found
        dead — the tail of its stdout/stderr, so the "Preview could not
        start" page can show why it exited.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        Optional[str]
            Captured log tail, or ``None`` if no dead container was recorded
            for this ticket (or its logs were empty/unreadable).
        """
        return self._exit_logs.get(f"{provider}:{ticket_id}") or None

    def is_serving(self, ticket_id: str, provider: str) -> bool:
        """Return ``True`` only once the app actually accepts connections.

        A container can be *registered and running* (``docker run -d`` has
        returned) yet not *serving* — its entrypoint may still be installing
        packages, checking out the branch, or starting the dev server. The
        ``/dev-env/view`` route uses this to decide whether the human's
        browser can be redirected to the preview yet: redirecting to a
        not-yet-listening port is exactly what produced the
        ``ERR_CONNECTION_REFUSED`` symptom this method exists to prevent.

        Readiness is checked where the server actually binds, so the answer
        is correct regardless of network topology:

        - **Docker envs** probe *inside* the container via ``docker exec``
          (see :meth:`_container_port_open`), reading its ``/proc/net/tcp``
          for a LISTEN socket on :data:`_APP_PORT`. A host-loopback probe
          from Marcus's own process would give a FALSE NEGATIVE when Marcus
          itself runs in a container (Docker-outside-of-Docker): the port is
          published into the *host* network namespace, not Marcus's
          container loopback, so ``127.0.0.1:<port>`` there answers nothing
          even though the preview is up.
        - **Local (non-Docker) envs** are a plain host process, so a TCP
          connect to ``127.0.0.1:<port>`` in Marcus's own namespace is
          correct and cheaper.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        bool
            ``True`` if an environment is registered for this ticket and its
            app is accepting connections; ``False`` otherwise.
        """
        info = self._envs.get(f"{provider}:{ticket_id}")
        if info is None:
            return False
        if not self.config.use_docker:
            return self._port_is_listening(info.port)
        return self._container_port_open(info.container_name)

    @staticmethod
    def _port_is_listening(port: int) -> bool:
        """Return ``True`` if something accepts TCP on ``127.0.0.1:port``."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _container_port_open(container_name: str) -> bool:
        """Return ``True`` if the app is LISTENing on :data:`_APP_PORT` inside
        the container.

        Reads ``/proc/net/tcp``/``tcp6`` via ``docker exec`` instead of a
        host-side TCP connect, so it works whether Marcus runs on the host or
        inside its own container (Docker-outside-of-Docker) — the check runs
        in the SAME network namespace the server binds in. Uses only ``sh``
        and ``awk`` (both in the base image's BusyBox), no extra packages.

        The awk test matches a row whose local address (field 2) ends with
        ``:<port-in-hex>`` and whose state (field 4) is ``0A`` (TCP_LISTEN),
        so an outbound/established connection that merely mentions the port
        as a *remote* endpoint never counts as "serving".

        Parameters
        ----------
        container_name : str
            Name of the running preview container.

        Returns
        -------
        bool
            ``True`` if the LISTEN socket is present; ``False`` on absence,
            a docker error, or a timeout.
        """
        port_hex = format(_APP_PORT, "04X")  # 3000 -> "0BB8"
        # Loop so a missing /proc/net/tcp6 (IPv6 disabled) can't error out the
        # whole probe; exit 0 as soon as either file shows a LISTEN row.
        probe = (
            "for f in /proc/net/tcp /proc/net/tcp6; do "
            '[ -f "$f" ] && awk -v p=":' + port_hex + '" '
            "'$2 ~ p\"$\" && $4==\"0A\"{f=1} END{exit !f}' \"$f\" "
            "&& exit 0; done; exit 1"
        )
        loop_cmd = ["docker", "exec", container_name, "sh", "-c", probe]
        try:
            result = subprocess.run(
                loop_cmd, capture_output=True, timeout=_DOCKER_CMD_TIMEOUT
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    def list_running(self) -> List[DevEnvironmentInfo]:
        """Return all currently running dev environments."""
        return list(self._envs.values())

    async def stop_all(self) -> None:
        """Stop all running dev environments (called on shutdown)."""
        keys = list(self._envs.keys())
        for key in keys:
            provider, ticket_id = key.split(":", 1)
            await self.stop(ticket_id, provider)

    async def refresh_by_branch(self, branch_name: str) -> bool:
        """Refresh the environment whose branch matches ``branch_name``.

        Used by the Gitea push webhook, which only knows the branch. The
        branch's ticket-id segment is sanitized and lowercased at
        branch-creation time, so parsing an id back OUT of the branch can
        never equal the registry's raw ticket id for non-lowercase or
        special-character ids (jira ``PROJ-42`` → branch
        ``ticket/jira/proj-42`` → parsed ``proj-42`` ≠ key
        ``jira:PROJ-42``) — the refresh silently missed forever for such
        providers. Matching against each env's *stored* branch name is
        exact by construction.

        Parameters
        ----------
        branch_name : str
            Full branch name from the push ref (e.g.
            ``ticket/kanboard/42``).

        Returns
        -------
        bool
            Result of :meth:`refresh` for the matching env; ``False``
            when no registered environment uses this branch.
        """
        for key, info in self._envs.items():
            if info.branch_name == branch_name:
                provider, ticket_id = key.split(":", 1)
                return await self.refresh(ticket_id, provider)
        logger.debug(
            "No dev environment registered for branch %r — nothing to refresh",
            branch_name,
        )
        return False

    async def prune_if_dead(self, ticket_id: str, provider: str) -> bool:
        """Purge a registered env whose container has actually died.

        Containers run with ``--rm`` and everything meaningful (dependency
        install, git checkout, dev server start) happens AFTER
        ``docker run -d`` returns — a failure seconds later removes the
        container entirely while the registry keeps reporting the env as
        running: ``get_info`` hands out a dead URL and the port/slot stay
        consumed against the parallel limit. Status queries call this
        first so a died environment is noticed at the first check and its
        slot freed.

        Parameters
        ----------
        ticket_id : str
            Provider ticket identifier.
        provider : str
            Kanban provider name.

        Returns
        -------
        bool
            ``True`` if a dead registration was pruned. ``False`` when the
            container is alive, nothing is registered, this is a
            non-Docker env, or the container's true state is unknown
            (docker error) — unknown state deliberately keeps the
            registration, mirroring ``stop()``'s bookkeeping rule.
        """
        key = f"{provider}:{ticket_id}"
        info = self._envs.get(key)
        if info is None or not self.config.use_docker:
            return False

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.Running}}",
                        info.container_name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - daemon down/binary missing
            logger.warning(
                "Dev env liveness check failed for %s: %s — keeping it tracked",
                key,
                exc,
            )
            return False

        if result.returncode == 0 and result.stdout.strip() == "true":
            return False

        # Dead: the container exited (Running=false) or is already gone
        # (inspect non-zero). Since it no longer runs with --rm, an exited
        # container lingers — grab its logs to explain WHY it exited, THEN
        # force-remove it so containers don't accumulate.
        self._exit_logs[key] = await self._capture_logs(info.container_name)
        await self._force_remove(info.container_name)
        # A concurrent call for the same key (e.g. two open browser tabs
        # both polling /api/dev-env-status) can win the race and remove
        # this entry first during the awaits above — pop rather than a
        # bare del so the second caller doesn't raise KeyError.
        if self._envs.pop(key, None) is None:
            return False
        self._allocator.release(info.port)
        logger.warning(
            "Dev env for %s is dead (container %s no longer running) — "
            "pruned; port %d released",
            key,
            info.container_name,
            info.port,
        )
        return True

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
            Additional ``apk add`` package names (language runtime) beyond the
            base set.

        Returns
        -------
        str
            A ``sh -c`` compatible shell command string.
        """
        apk_extras = " ".join(extra_apt) if extra_apt else ""
        # Best-effort (`|| true`): a transient package-index failure must
        # never stop the container from serving — the static fallback
        # (BusyBox httpd) is part of the base image regardless.
        apk_line = (
            f"apk add --no-cache {_BASE_APK}"
            f"{' ' + apk_extras if apk_extras else ''} 2>/dev/null || true"
        )

        # Both trees are read by git: /src is the read-only bind-mount of the
        # host repo (host-owned uid) and /app is the container-local clone —
        # mark both safe so git's "detected dubious ownership" guard can't
        # abort the clone/checkout.
        safe_dir = (
            "git config --global --add safe.directory /src 2>/dev/null || true; "
            "git config --global --add safe.directory /app 2>/dev/null || true"
        )

        # ISOLATED CHECKOUT. The source repo is mounted READ-ONLY at /src; we
        # clone it into the container's own writable /app so the preview gets
        # its own working tree and .git. This is what keeps a preview from
        # ever mutating the shared host repo (the old design bind-mounted the
        # live repo and ran `git checkout`/`git reset --hard` directly on it,
        # switching the host's branch and racing Marcus's own git operations;
        # two previews of different branches of the same project also fought
        # over one working tree). With --rm the /app clone is discarded when
        # the container stops.
        #
        # The clone's `origin` is left pointing at /src ON PURPOSE. /src is a
        # local path bind-mount, so refresh()'s `git fetch origin && git reset
        # --hard origin/<branch>` always reaches it — with no network and no
        # credentials. Re-pointing origin at the real Gitea URL would be
        # unreachable from the preview container: the host repo's origin is
        # `http://gitea:3000` (a compose service name that doesn't resolve on
        # Docker's default bridge) or `http://localhost:3000` (the container's
        # own loopback), so every fetch would fail. Fetching from /src instead
        # tracks whatever branch state Marcus's own repo has (which its
        # merge/diff flows keep updated from Gitea).
        clone_line = "git clone /src /app 2>/dev/null || true"

        # branch_name is interpreted as shell syntax by the `sh -c` this
        # string is eventually passed to inside the container — quote it so
        # shell metacharacters in an unsanitized caller's input can't break
        # out of `git checkout` into arbitrary command execution. Try a plain
        # checkout first (branch present from the clone), then create a local
        # branch tracking origin's (a branch that only exists on Gitea); both
        # `|| true` so we still serve whatever is in /app on failure.
        q_branch = shlex.quote(branch_name)
        q_origin = shlex.quote(f"origin/{branch_name}")
        checkout_line = (
            f"( git checkout {q_branch} 2>/dev/null || "
            f"git checkout -b {q_branch} {q_origin} 2>/dev/null ) || true"
        )

        # The `touch _READY_MARKER` immediately after checkout lets refresh()
        # (see _wait_until_ready) confirm the initial checkout has actually
        # happened before running its own git commands against /app — the apk
        # install and clone above can take a few seconds, and `docker run -d`
        # returns long before this script reaches here.
        steps = [
            apk_line,
            safe_dir,
            clone_line,
            checkout_line,
            f"touch {_READY_MARKER}",
        ]
        if install_cmd:
            # `|| true` so a failed dependency install still reaches the
            # start command (which itself falls back to the static server).
            steps.append(f"{install_cmd} || true")

        # Wrap the real dev command so the container ALWAYS keeps serving.
        # `;` (not `||`) between the command and the fallback is deliberate:
        # `||` only fires on a NON-ZERO exit, so a dev command that exits 0
        # (self-daemonizes, or is a launcher that returns success and detaches)
        # would skip the fallback, `served` would return 0, PID 1 would fall
        # off the end, and the --rm container would vanish from `docker ps`.
        # With `;`, the fallback runs whenever the dev command RETURNS at all
        # (any exit code) — and never runs while a normal foreground server is
        # still blocking. A long-running server therefore holds PID 1 open; a
        # crashed or exited one hands off to BusyBox httpd, which serves /app
        # forever. Either way the port is always answered.
        served = f"( {start_cmd}; {_STATIC_FALLBACK} )"

        if use_hm_reload:
            steps.append(served)
            return " && ".join(steps)

        # inotifywait restart loop for interpreted / non-HMR stacks.
        #
        # Setup steps are joined with ';' (not '&&') and each is already
        # '|| true', so a hiccup during setup never prevents the server from
        # starting.  The served command is backgrounded and its PID tracked
        # so a file change can restart it.
        #
        # The trailing `wait $APP_PID` is essential: it keeps the container's
        # PID 1 alive (and the port served) even when `inotifywait` is
        # missing or errors.  In that case the `while` condition fails on the
        # first evaluation and the loop exits immediately; without the final
        # wait the shell would fall off the end, PID 1 would exit, and the
        # `--rm` container would vanish from `docker ps` mid-serve — the very
        # failure this whole rework exists to eliminate.
        setup_part = "; ".join(steps)
        return (
            f"{setup_part}; "
            f"{served} & APP_PID=$!; "
            f"while inotifywait -e modify,create,delete,move -r /app "
            f"--exclude '\\.git' --quiet 2>/dev/null; do "
            f"echo '[marcus] File changed — restarting...'; "
            f"kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null; "
            f"{served} & APP_PID=$!; "
            f"done; "
            f"wait $APP_PID"
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
            # Primary path: stack from project description.  Prefer Alpine
            # package names (apk_packages) since _BASE_IMAGE is alpine-based;
            # fall back to the legacy apt_packages list for older callers.
            install_cmd: str = project_stack.install_cmd
            start_cmd: str = project_stack.dev_cmd
            use_hm_reload: bool = project_stack.use_hm_reload
            # Prefer Alpine package names (apk_packages) since _BASE_IMAGE is
            # alpine-based; fall back to the legacy apt_packages list ONLY for
            # older callers whose object has no apk_packages attribute. An
            # apk_packages that is present but empty (a stack needing no extra
            # runtime, e.g. static) is a valid answer and must NOT fall through
            # to the Debian-named apt_packages — those names would be handed to
            # `apk add`, which doesn't know them.
            apk_pkgs = getattr(project_stack, "apk_packages", None)
            extra_apt = (
                apk_pkgs
                if apk_pkgs is not None
                else getattr(project_stack, "apt_packages", [])
            )
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
            extra_apt = fb.get("apk", [])
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

        # Confirm the generic Node.js guess against the repo's own
        # package.json before running it. Both paths above can produce the
        # exact same unconfirmed "npm run dev -- --port 3000" default —
        # project_stack.dev_cmd when a Tech Stack section names Node.js but
        # gives no explicit "Dev server command" line, or the auto-detect
        # table for any repo with a package.json — and it fails outright
        # (`npm error Missing script: "dev"`) for any project that names
        # its dev-server script something else instead of not having one.
        # A human's own EXPLICIT "Dev server command" line is never this
        # exact generic string by coincidence in practice, so refining only
        # when it matches leaves a real override untouched.
        if start_cmd == _FALLBACK_STACKS["nodejs"]["start"]:
            start_cmd = _resolve_nodejs_dev_command(repo_path)

        # Remember the resolved dev-server command so the "Preview could not
        # start" page can show exactly what Marcus inferred from the Tech
        # Stack and ran (survives the env being pruned when the container
        # dies).
        self._last_command[f"{provider}:{ticket_id}"] = start_cmd

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
                # NB: no --rm. A crashed container must survive in 'exited'
                # state so prune_if_dead can read its logs and explain WHY the
                # preview never came up. It is force-removed after its logs are
                # captured (prune_if_dead / _stop_docker / reconcile_orphans),
                # and any lingering same-named container is cleared just below
                # before this run, so containers do not accumulate.
                "--name",
                container_name,
                "-p",
                f"{port}:3000",
                # Source repo mounted READ-ONLY at /src. The entrypoint clones
                # it into the container-local /app (see _build_entrypoint), so
                # the preview gets an isolated working tree and can never
                # mutate the shared host repo. /app is deliberately NOT a bind
                # mount — it lives in the container's writable layer and is
                # discarded when the container is removed.
                "-v",
                f"{_resolve_host_repo_path(repo_path)}:/src:ro",
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

        # Without --rm, a previous container of the same name (e.g. one left
        # 'exited' after its logs were captured) would fail this run with a
        # name collision — clear it first (best-effort).
        await self._force_remove(container_name)

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
            # The client-side call timed out, but the daemon may have
            # created (or even started) the container anyway — it was
            # never registered into self._envs, so nothing else will ever
            # clean it up. Best-effort remove it before raising.
            await self._force_remove(container_name)
            raise RuntimeError(
                f"Docker container start timed out after {_DOCKER_CMD_TIMEOUT}s "
                "(Docker daemon unresponsive?)"
            ) from None
        if result.returncode != 0:
            self._allocator.release(port)
            # Same leak risk as above: a non-zero exit can still mean the
            # container was created before it failed (e.g. crashed on
            # entrypoint) — clear it rather than leaving it orphaned.
            await self._force_remove(container_name)
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
        except subprocess.TimeoutExpired:
            logger.warning(
                "docker stop timed out for %s after %ds (Docker daemon "
                "unresponsive?) — container may still be running",
                container_name,
                _DOCKER_CMD_TIMEOUT,
            )
            return False
        # Containers no longer run with --rm, so a graceful `docker stop`
        # leaves them in 'exited' state — remove explicitly so they don't
        # accumulate.
        await self._force_remove(container_name)
        return True

    async def _capture_logs(self, container_name: str, tail: int = 40) -> str:
        """Return the last *tail* lines of a container's logs (best-effort).

        Used to explain WHY a preview container exited. Combines the
        container's stdout and stderr (a crashing dev server usually prints
        to stderr) and caps the size for the JSON response / page. Returns an
        empty string if the container is gone or Docker errors.

        Parameters
        ----------
        container_name : str
            Name of the (exited) container to read.
        tail : int
            Maximum number of trailing log lines to fetch.

        Returns
        -------
        str
            The captured log tail, or ``""`` on any failure.
        """
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "logs", "--tail", str(tail), container_name],
                    capture_output=True,
                    text=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - daemon down/binary missing
            logger.warning(
                "Could not capture logs for %s: %s", container_name, exc
            )
            return ""
        combined = (result.stdout or "") + (result.stderr or "")
        return combined.strip()[-4000:]

    async def _force_remove(self, container_name: str) -> None:
        """Force-remove a container by name (best-effort, ignores errors).

        Parameters
        ----------
        container_name : str
            Name of the container to ``docker rm -f``.
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=_DOCKER_CMD_TIMEOUT,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug(
                "docker rm -f %s failed (ignored): %s", container_name, exc
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
