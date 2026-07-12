"""
Unit tests for ClaudeCliProvider.

Tests provider initialization, subprocess invocation, envelope parsing,
error handling, and LLMAbstraction integration — all without spawning a
real `claude` CLI process (asyncio.create_subprocess_exec is mocked
throughout).

ClaudeCliProvider lets Marcus's own internal LLM calls (task decomposition,
dependency inference, effort estimation, blocker analysis) run through a
locally-installed `claude` CLI instead of a metered Anthropic API key —
using whatever auth the CLI is already logged into (a Claude Pro/Max
subscription, most commonly). It deliberately never reads or sets
ANTHROPIC_API_KEY/CLAUDE_API_KEY, matching the same "don't disturb Claude
Code's subscription auth" principle already documented in
llm_abstraction.py for the Anthropic provider.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.models import Priority, Task, TaskStatus


def _make_task(name: str = "Test task") -> Task:
    return Task(
        id="t1",
        name=name,
        description="A test task",
        status=TaskStatus.TODO,
        priority=Priority.MEDIUM,
        assigned_to=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        due_date=None,
        estimated_hours=2.0,
    )


def _mock_config(max_tokens: int = 4096, temperature: float = 0.1) -> Mock:
    cfg = Mock()
    cfg.ai.max_tokens = max_tokens
    cfg.ai.temperature = temperature
    return cfg


def _envelope(
    result: str = "OK",
    is_error: bool = False,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> bytes:
    """Build a fake `claude -p --output-format json` stdout payload."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success" if not is_error else "error",
            "is_error": is_error,
            "result": result,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    ).encode()


class _FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.mark.unit
class TestClaudeCliProviderInit:
    """Provider construction — no subprocess, no API key required."""

    def test_constructs_without_any_api_key(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        assert provider.model == ""

    def test_reads_max_tokens_and_temperature_from_config(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config(max_tokens=2048, temperature=0.3)
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        assert provider.max_tokens == 2048
        assert provider.temperature == 0.3

    def test_explicit_model_is_stored(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider(model="sonnet")

        assert provider.model == "sonnet"

    def test_does_not_create_an_http_client(self) -> None:
        """No httpx.AsyncClient — this provider has no HTTP transport at all."""
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        assert not hasattr(provider, "client")


@pytest.mark.unit
class TestCallClaudeCli:
    """_call_claude_cli() — the single subprocess-invocation hook."""

    @pytest.mark.asyncio
    async def test_returns_result_text_on_success(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(_envelope(result="hello world"))
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            result = await provider._call_claude_cli("say hi")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_builds_command_with_print_and_no_tools(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(_envelope())
        create_mock = AsyncMock(return_value=fake_proc)
        with patch("asyncio.create_subprocess_exec", create_mock):
            await provider._call_claude_cli("analyze this")

        args = create_mock.call_args.args
        assert args[0] == "claude"
        assert "-p" in args
        assert "analyze this" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--allowedTools" in args

    @pytest.mark.asyncio
    async def test_passes_model_flag_when_configured(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider(model="opus")

        fake_proc = _FakeProcess(_envelope())
        create_mock = AsyncMock(return_value=fake_proc)
        with patch("asyncio.create_subprocess_exec", create_mock):
            await provider._call_claude_cli("analyze this")

        args = create_mock.call_args.args
        assert "--model" in args
        assert "opus" in args

    @pytest.mark.asyncio
    async def test_omits_model_flag_when_not_configured(self) -> None:
        """No --model passed when unset — let the CLI use its session default."""
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(_envelope())
        create_mock = AsyncMock(return_value=fake_proc)
        with patch("asyncio.create_subprocess_exec", create_mock):
            await provider._call_claude_cli("analyze this")

        args = create_mock.call_args.args
        assert "--model" not in args

    @pytest.mark.asyncio
    async def test_never_sets_anthropic_or_claude_api_key_env(self) -> None:
        """Regression guard: must not disturb subscription auth via env vars.

        Matches the documented principle in llm_abstraction.py's Anthropic
        provider block: writing ANTHROPIC_API_KEY/CLAUDE_API_KEY into the
        environment would force claude CLI subprocesses to bill an API key
        instead of using the logged-in subscription.
        """
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(_envelope())
        create_mock = AsyncMock(return_value=fake_proc)
        with patch("asyncio.create_subprocess_exec", create_mock):
            await provider._call_claude_cli("analyze this")

        # No env= kwarg at all means the subprocess inherits this process's
        # environment unmodified — provider must not inject/override it.
        assert "env" not in create_mock.call_args.kwargs

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_exit(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(b"", stderr=b"claude: command failed", returncode=1)
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            with pytest.raises(RuntimeError, match="exited"):
                await provider._call_claude_cli("analyze this")

    @pytest.mark.asyncio
    async def test_raises_on_is_error_envelope(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(_envelope(result="rate limited", is_error=True))
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                await provider._call_claude_cli("analyze this")

    @pytest.mark.asyncio
    async def test_raises_on_non_json_stdout(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(b"not json at all")
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            with pytest.raises(RuntimeError, match="non-JSON"):
                await provider._call_claude_cli("analyze this")

    @pytest.mark.asyncio
    async def test_raises_and_kills_process_on_timeout(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider(timeout=0.01)

        fake_proc = _FakeProcess(_envelope())

        async def _hang(*a, **k):
            await asyncio.sleep(10)
            return b"", b""

        fake_proc.communicate = _hang  # type: ignore[method-assign]

        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                await provider._call_claude_cli("analyze this")

        assert fake_proc.killed is True

    @pytest.mark.asyncio
    async def test_strips_leading_think_block(self) -> None:
        """Reuses LocalLLMProvider's reasoning-block stripping."""
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        fake_proc = _FakeProcess(
            _envelope(result="<think>reasoning...</think>\n{\"ok\": true}")
        )
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            result = await provider._call_claude_cli("analyze this")

        assert result == '{"ok": true}'


@pytest.mark.unit
class TestBusinessMethodsReuseInheritedLogic:
    """analyze_task etc. are inherited from LocalLLMProvider unchanged —
    only the transport hook (_call_local_llm) is overridden."""

    @pytest.mark.asyncio
    async def test_analyze_task_parses_json_from_cli_response(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        analysis_json = json.dumps(
            {
                "task_intent": "Build auth",
                "semantic_dependencies": [],
                "risk_factors": ["security"],
                "suggestions": ["add rate limiting"],
                "confidence": 0.8,
                "reasoning": "clear scope",
                "risk_assessment": {"security": "high"},
            }
        )
        fake_proc = _FakeProcess(_envelope(result=analysis_json))
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
        ):
            result = await provider.analyze_task(_make_task(), {})

        assert result.task_intent == "Build auth"
        assert result.confidence == 0.8

    @pytest.mark.asyncio
    async def test_analyze_task_falls_back_gracefully_on_cli_failure(self) -> None:
        """Same fail-open behavior as every other provider on transport error."""
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("claude: not found")),
        ):
            result = await provider.analyze_task(_make_task(), {})

        assert result.confidence < 0.5
        assert "local_llm_analysis_failed" in result.risk_factors


@pytest.mark.unit
class TestCostProviderName:
    def test_cost_provider_name_is_claude_subscription(self) -> None:
        from src.ai.providers.claude_cli_provider import ClaudeCliProvider

        cfg = _mock_config()
        with patch("src.config.marcus_config.get_config", return_value=cfg):
            provider = ClaudeCliProvider()

        assert provider._cost_provider_name() == "claude_subscription"
