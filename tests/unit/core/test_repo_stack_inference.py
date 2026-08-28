"""
Unit tests for src/core/repo_stack_inference.py's infer_stack_with_ai and
its snapshot-building helpers.

infer_stack_with_ai is the AI-based fallback used ONLY when deterministic
file-sniffing (detect_project_type) finds nothing recognizable at all —
these tests exercise it directly against real temporary repo directories
(this codebase's established "empirically verify against the real
implementation" testing style for anything involving file content), with
a fake async generate_text standing in for Marcus's own AI provider.
"""

import json

import pytest

from src.core.repo_stack_inference import (
    _build_repo_snapshot,
    infer_stack_with_ai,
)


def _fake_llm(response_text):
    async def generate_text(prompt):
        return response_text

    return generate_text


class TestBuildRepoSnapshot:
    def test_includes_top_level_listing(self, tmp_path):
        (tmp_path / "server.js").write_text("console.log('hi')")
        (tmp_path / "weird_lockfile.custom").write_text("x")

        snapshot = _build_repo_snapshot(str(tmp_path))

        assert "server.js" in snapshot
        assert "weird_lockfile.custom" in snapshot

    def test_includes_readme_content(self, tmp_path):
        (tmp_path / "README.md").write_text("# My Custom App\n\nRuns on Deno.")

        snapshot = _build_repo_snapshot(str(tmp_path))

        assert "My Custom App" in snapshot
        assert "Deno" in snapshot

    def test_includes_entrypoint_file_content(self, tmp_path):
        (tmp_path / "main.py").write_text("import bottle\napp = bottle.Bottle()")

        snapshot = _build_repo_snapshot(str(tmp_path))

        assert "bottle" in snapshot

    def test_skips_vcs_and_dependency_directories(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()

        snapshot = _build_repo_snapshot(str(tmp_path))

        assert "node_modules" not in snapshot
        assert "__pycache__" not in snapshot

    def test_empty_repo_yields_empty_snapshot(self, tmp_path):
        assert _build_repo_snapshot(str(tmp_path)).strip() == ""

    def test_nonexistent_path_does_not_raise(self, tmp_path):
        snapshot = _build_repo_snapshot(str(tmp_path / "does-not-exist"))
        assert snapshot.strip() == ""


class TestInferStackWithAi:
    @pytest.mark.asyncio
    async def test_returns_none_for_empty_repo_without_calling_ai(self, tmp_path):
        calls = []

        async def generate_text(prompt):
            calls.append(prompt)
            return "{}"

        result = await infer_stack_with_ai(str(tmp_path), generate_text)

        assert result is None
        assert calls == []  # nothing to analyze — never even asked the AI

    @pytest.mark.asyncio
    async def test_parses_a_valid_json_response_into_a_stack(self, tmp_path):
        (tmp_path / "main.py").write_text("import bottle")
        response = json.dumps(
            {
                "language": "python",
                "framework": "Bottle",
                "install_cmd": "pip install bottle",
                "dev_cmd": "python main.py",
                "use_hot_reload": False,
                "apk_packages": ["python3", "py3-pip"],
            }
        )

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm(response))

        assert result is not None
        assert result.language == "python"
        assert result.framework == "Bottle"
        assert result.install_cmd == "pip install bottle"
        assert result.dev_cmd == "python main.py"
        assert result.extra_apt == ["python3", "py3-pip"]

    @pytest.mark.asyncio
    async def test_handles_response_wrapped_in_markdown_fences(self, tmp_path):
        (tmp_path / "main.py").write_text("import bottle")
        response = (
            "```json\n"
            + json.dumps(
                {
                    "language": "python",
                    "framework": "",
                    "install_cmd": "",
                    "dev_cmd": "python main.py",
                    "use_hot_reload": False,
                    "apk_packages": [],
                }
            )
            + "\n```"
        )

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm(response))

        assert result is not None
        assert result.dev_cmd == "python main.py"

    @pytest.mark.asyncio
    async def test_declared_unable_to_determine_yields_none(self, tmp_path):
        """The prompt explicitly instructs the AI to answer
        {"language": ""} when it can't confidently tell — must not be
        treated as a static-site stack."""
        (tmp_path / "mystery.bin").write_text("???")
        response = json.dumps({"language": ""})

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm(response))

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_dev_cmd_yields_none(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        response = json.dumps({"language": "python", "dev_cmd": ""})

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm(response))

        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_response_yields_none_not_a_crash(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm("not json at all"))

        assert result is None

    @pytest.mark.asyncio
    async def test_ai_call_raising_yields_none_not_a_crash(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")

        async def broken_generate_text(prompt):
            raise RuntimeError("provider unavailable")

        result = await infer_stack_with_ai(str(tmp_path), broken_generate_text)

        assert result is None

    @pytest.mark.asyncio
    async def test_non_list_apk_packages_degrades_to_empty_list(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        response = json.dumps(
            {
                "language": "python",
                "dev_cmd": "python main.py",
                "apk_packages": "python3",  # wrong type, not a list
            }
        )

        result = await infer_stack_with_ai(str(tmp_path), _fake_llm(response))

        assert result is not None
        assert result.extra_apt == []
