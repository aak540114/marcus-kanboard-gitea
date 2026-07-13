"""
Claude CLI (subscription) LLM Provider for Marcus AI.

Routes Marcus's own internal LLM calls (task decomposition, dependency
inference, effort estimation, blocker analysis) through a locally-installed
``claude`` CLI in non-interactive print mode, instead of a metered
Anthropic API key. Whatever auth the CLI is already logged into — most
commonly a Claude Pro/Max subscription via ``claude login`` — is what gets
used; this provider never reads or sets ``ANTHROPIC_API_KEY`` /
``CLAUDE_API_KEY``, matching the same principle already documented for the
Anthropic provider in ``llm_abstraction.py`` (writing that env var would
force ``claude`` subprocesses to bill an API key instead of using the
subscription).

Classes
-------
ClaudeCliProvider
    Subscription-backed provider that shells out to ``claude -p`` instead
    of calling the Anthropic API directly.

Notes
-----
Requires the ``claude`` CLI installed and authenticated (``claude login``)
on the same machine Marcus itself runs on. This works for any deployment
topology where Marcus's *own* process has that login — Kanboard and Gitea
can live on entirely separate hosts, since Marcus already talks to them
over plain HTTP regardless of where its own LLM calls come from.

Each call spawns a full ``claude`` CLI process (observed latency: several
seconds to tens of seconds per call, versus typical sub-second direct API
latency), because it goes through the full agent harness for a single
completion. Subscription usage limits apply and are shared with any
interactive Claude Code sessions on the same account.

Examples
--------
>>> provider = ClaudeCliProvider(model="sonnet")
>>> analysis = await provider.analyze_task(task, context)
"""

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any, Dict, Optional

from src.core.error_framework import AIProviderError
from src.cost_tracking.cost_recorder import get_recorder

from .local_provider import LocalLLMProvider, _strip_reasoning_blocks

logger = logging.getLogger(__name__)

_KILL_GRACE_PERIOD_SECONDS = 2.0


async def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill ``proc``'s entire process group, tolerating one that already exited.

    ``claude -p`` runs the full agent harness and may spawn its own child
    processes; signaling only the leader PID (a bare ``proc.kill()``) can
    leave those children running past the timeout. Requires the process to
    have been started with ``start_new_session=True`` so it leads its own
    group. Mirrors the pattern in
    ``src.integrations.product_smoke._kill_process``, written there to fix
    a reproduced process-leak bug (Codex P1 on PR #352): sending
    ``terminate()``/``kill()`` to just the leader PID leaves shell-spawned
    children running and holding resources (e.g. ports).

    Parameters
    ----------
    proc : asyncio.subprocess.Process
        Process spawned with ``start_new_session=True``. No-op if the
        process group is already gone.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_PERIOD_SECONDS)
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


class ClaudeCliProvider(LocalLLMProvider):
    """Subscription-backed LLM provider that shells out to the ``claude`` CLI.

    Inherits all semantic-analysis business logic (prompt building, JSON
    response parsing, fallback behavior on failure) from
    ``LocalLLMProvider`` unchanged — only the transport hook
    (``_call_local_llm``) is overridden, following the same pattern
    ``CloudLLMProvider`` uses to swap in a different backend.

    Parameters
    ----------
    model : Optional[str]
        Model alias to pass via ``claude --model`` (e.g. ``"sonnet"``,
        ``"opus"``, ``"haiku"``). When ``None`` (default), no ``--model``
        flag is passed and the CLI uses its own session default. Not
        wired to ``config.ai.model`` automatically — that field's default
        is an old dated Anthropic API model string (e.g.
        ``"claude-3-haiku-20240307"``), not a valid CLI alias, and passing
        it through would break the CLI invocation for anyone using this
        provider's defaults.
    timeout : float
        Seconds to wait for one ``claude`` CLI invocation before killing
        the subprocess and raising. Default 120s — each call is a full
        CLI process spawn, not a lightweight API request.

    Raises
    ------
    (none at construction — no credentials to validate; a missing or
    unauthenticated ``claude`` binary surfaces as a runtime error on the
    first actual call, handled the same way every other provider's
    transport failures are: caught per-business-method and turned into a
    safe fallback response.)
    """

    def __init__(self, model: Optional[str] = None, timeout: float = 120.0) -> None:
        """Initialize the Claude CLI provider (no HTTP client, no API key)."""
        # Deliberately skip LocalLLMProvider.__init__ — it builds an httpx
        # client pointed at a local server URL, none of which applies here.
        from src.config.marcus_config import get_config

        config = get_config()

        # LocalLLMProvider.model is typed `str` (always required there) —
        # normalize None to "" here to match that type exactly rather than
        # widen it (an empty string is falsy, so `if self.model:` below
        # behaves identically to the Optional[str] version would have).
        self.model: str = model or ""
        self.max_tokens = config.ai.max_tokens
        self.temperature = config.ai.temperature
        self.timeout = timeout

        logger.info(
            "Claude CLI (subscription) provider initialized%s",
            f" with model={model}" if model else " (using CLI session default model)",
        )

    def _cost_provider_name(self) -> str:
        """Return ``'claude_subscription'`` as the cost-event provider tag."""
        return "claude_subscription"

    # Override the internal dispatch every inherited business method
    # (analyze_task, infer_dependencies, estimate_effort, ...) routes
    # through, so they transparently use the CLI instead of an HTTP call.
    async def _call_local_llm(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> str:
        """Delegate to ``_call_claude_cli``.

        ``max_tokens``/``temperature`` are accepted for interface
        compatibility with the parent class but not forwarded to the CLI —
        ``claude -p`` has no sampling-parameter flags (confirmed via
        ``claude --help``); Claude Code manages its own generation
        parameters internally. Logged (not silently dropped) so a caller
        that relies on ``max_tokens`` to bound cost/latency for this
        provider can tell the cap had no effect.
        """
        if max_tokens is not None:
            logger.debug(
                "ClaudeCliProvider ignores max_tokens=%s — the `claude` CLI "
                "has no output-token-limiting flag.",
                max_tokens,
            )
        return await self._call_claude_cli(prompt)

    async def _call_claude_cli(self, prompt: str) -> str:
        """Run one non-interactive ``claude -p`` invocation and return its text.

        Parameters
        ----------
        prompt : str
            Prompt to send.

        Returns
        -------
        str
            The CLI's response text, with any leading ``<think>...</think>``
            reasoning-block prefix stripped (reusing the same helper the
            local/cloud providers use).

        Raises
        ------
        AIProviderError
            On subprocess timeout, non-zero exit, non-JSON or non-object
            stdout, or an ``is_error`` result in the CLI's JSON envelope.
        """
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            # `--tools ""` is the CLI's documented way to disable all tools
            # ("Use \"\" to disable all tools" per `claude --help`) — unlike
            # `--allowedTools`, whose own help text doesn't document empty-
            # string semantics. No tool access is needed or wanted here:
            # unlike the coding agents Runner mode spawns, these are pure
            # analysis calls with no file/Bash access.
            "--tools",
            "",
            # Belt-and-suspenders against any interactive prompt (workspace
            # trust, settings-validation dialog, etc.) hanging this fully
            # non-interactive call: `-p` already skips the trust dialog
            # when stdout isn't a TTY (documented in `claude --help`), and
            # `--tools ""` leaves nothing for a permission prompt to ask
            # about, but this closes the gap for any other prompt class.
            "--dangerously-skip-permissions",
        ]
        if self.model:
            cmd += ["--model", self.model]

        # No env= kwarg: the subprocess inherits this process's environment
        # unmodified. Never inject ANTHROPIC_API_KEY/CLAUDE_API_KEY here —
        # doing so would force this (and any other) claude CLI subprocess
        # to bill a metered API key instead of using whatever the CLI is
        # already logged into.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Explicitly closed rather than inherited: guarantees this call
            # can never block on an interactive read, and reinforces the
            # CLI's own non-TTY-stdout trust-dialog-skip detection.
            stdin=asyncio.subprocess.DEVNULL,
            # New session/process group so a timeout/cancellation can kill
            # the whole descendant tree via _kill_process_group, not just
            # the leader PID.
            start_new_session=True,
        )

        start = time.monotonic()
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=TimeoutError(f"claude CLI timed out after {self.timeout}s"),
                retryable=True,
            ) from None
        except BaseException:
            # Any other failure while awaiting communicate() — task
            # cancellation, a broken pipe, etc. — must still reap the
            # child so it doesn't outlive this call.
            await _kill_process_group(proc)
            raise

        if stderr:
            # Not necessarily an error (a zero exit can still write
            # diagnostics/warnings) — logged so it's available for
            # debugging instead of silently discarded.
            logger.debug(
                "claude CLI stderr (exit=%s): %s",
                proc.returncode,
                stderr.decode(errors="replace")[:500],
            )

        if proc.returncode != 0:
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=RuntimeError(
                    f"claude CLI exited {proc.returncode}: "
                    f"{stderr.decode(errors='replace')[:500]}"
                ),
                retryable=True,
            )

        try:
            parsed: Any = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=exc,
                retryable=False,
            ) from exc

        if not isinstance(parsed, dict):
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=TypeError(
                    f"claude CLI returned non-object JSON: {type(parsed).__name__}"
                ),
                retryable=False,
            )
        envelope: Dict[str, Any] = parsed

        if envelope.get("is_error"):
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=RuntimeError(
                    f"claude CLI reported an error: {envelope.get('result')}"
                ),
                retryable=False,
            )

        result = envelope.get("result", "")
        if not isinstance(result, str):
            raise AIProviderError(
                provider_name=self._cost_provider_name(),
                operation="cli_call",
                cause=TypeError(f"Unexpected claude CLI result type: {type(result)}"),
                retryable=False,
            )

        usage = envelope.get("usage") or {}

        def _usage_int(key: str) -> int:
            # `.get(key, 0)` alone only substitutes 0 when the key is
            # ABSENT — a present-but-null value (`"input_tokens": null`)
            # would still reach int() and raise. `or 0` catches both.
            return int(usage.get(key) or 0)

        get_recorder().record_planner_call(
            operation="analyze",
            provider=self._cost_provider_name(),
            model=self.model or envelope.get("model") or "default",
            input_tokens=_usage_int("input_tokens"),
            output_tokens=_usage_int("output_tokens"),
            cache_creation_tokens=_usage_int("cache_creation_input_tokens"),
            cache_read_tokens=_usage_int("cache_read_input_tokens"),
            latency_ms=int((time.monotonic() - start) * 1000),
            request_id=envelope.get("session_id"),
        )

        return _strip_reasoning_blocks(result)
